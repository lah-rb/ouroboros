"""Phase 2 of the whole-if-it-fits rule (agent/context_fit.py): the places
that used to cut evidence with a fixed number now size it at the point of
use, and the fallback is always a way back to the whole.

- a step-level ``fit`` map sizes a value a TEMPLATE interpolates (no section
  can declare ``fit`` on it) — rewrite's ``{input.validation_errors}``;
- a section's ``fit: "index"`` saves what will not fit and shows its line
  index instead of a cut;
- the diagnosis seed's transcript is whole when it fits beside the rest of
  the seed, else saved and indexed, and a trace can read ``file:A-B`` back;
- the seed shows every failing test and every prior attempt;
- the terminal view's bounds follow the serving window.
"""

from __future__ import annotations

import copy
import json
import os
from types import SimpleNamespace

import pytest

from agent import context_fit as cf
from agent.actions.diagnosis_session_actions import (
    _failing_test_block,
    action_execute_symbol_trace,
    action_start_diagnosis_session,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput, TurnDefinition
from agent.runtime import _fit_step_keys, _fit_tail_sections
from agent.session_injections import _KEY as INJECTION_KEY

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Window(MockEffects):
    """A server that reports its window and counts ~4 chars per token."""

    def __init__(self, n_ctx: int, **kw):
        super().__init__(**kw)
        self._n = n_ctx

    async def cache_health(self):
        return {"nCtxSeq": self._n}

    async def token_count(self, texts, model=""):
        return [len(t) // 4 for t in texts]


def _lines(n: int, width: int = 60) -> str:
    return "\n".join(f"line {i} " + "x" * (width - 10) for i in range(1, n + 1))


# ── the step-level fit map (template steps, turn problem templates) ─────


@pytest.mark.asyncio
async def test_a_template_value_is_sized_against_the_rendered_prompt():
    big = _lines(6000)  # ~360k chars, ~90k tokens
    inputs = {"validation_errors": big}
    ns = {
        "input": inputs,
        "context": {},
        "meta": {"flow_name": "rewrite", "step_id": "generate_rewrite"},
    }
    step = SimpleNamespace(fit={"input.validation_errors": "tail"})
    await _fit_step_keys(
        step,
        ns,
        _Window(32768),
        {"max_tokens": 2048},
        "generate_rewrite",
        lambda n: "Fix these errors:\n" + n["input"]["validation_errors"],
    )
    fitted = ns["input"]["validation_errors"]
    marker, _, kept = fitted.partition("\n")
    assert marker.startswith("[… the first ") and "32,768-token window" in marker
    assert big.endswith(kept) and "line 6000" in kept  # the most recent part
    # Copy-on-write: the flow's own input dict is never mutated.
    assert inputs["validation_errors"] is big and ns["input"] is not inputs


@pytest.mark.asyncio
async def test_a_template_value_that_fits_is_left_alone():
    inputs = {"validation_errors": "E  assert 1 == 2\ntests/test_a.py:3"}
    ns = {"input": inputs, "context": {}, "meta": {}}
    step = SimpleNamespace(fit={"input.validation_errors": "tail"})
    await _fit_step_keys(
        step, ns, _Window(32768), {}, "s", lambda n: n["input"]["validation_errors"]
    )
    assert ns["input"] is inputs  # untouched, same object


# ── fit: "index" on a section ───────────────────────────────────────────


def _evaluator_turn(mode: str) -> TurnDefinition:
    with open(os.path.join(_ROOT, "flows", "compiled.json")) as f:
        step = copy.deepcopy(json.load(f)["interact"]["steps"]["evaluate_outcome"])
    turn = TurnDefinition.model_validate(step["turn"])
    for i, s in enumerate(turn.sections):
        if s.ref is not None and s.ref.ref == "context.eval_session_tail":
            turn.sections[i] = s.model_copy(update={"fit": mode})
    return turn


def _eval_namespaces(text: str) -> dict:
    return {
        "input": {},
        "context": {
            "eval_session_tail": text,
            "eval_objective": "---TEST OBJECTIVE---\nLook works.\n---END TEST OBJECTIVE---",
        },
        "meta": {"flow_name": "interact", "step_id": "evaluate_outcome"},
    }


@pytest.mark.asyncio
async def test_an_index_section_saves_the_whole_and_shows_its_ranges():
    text = _lines(5000)
    ns = _eval_namespaces(text)
    eff = _Window(16384)
    await _fit_tail_sections(_evaluator_turn("index"), ns, eff, {}, "evaluate_outcome")
    fitted = ns["context"]["eval_session_tail"]
    assert fitted.startswith("(eval_session_tail is too large to show whole here")
    saved = [
        p
        for p in eff.written_files
        if p.startswith(".agent/outputs/interact-evaluate_outcome-eval_session_tail-")
    ]
    assert len(saved) == 1 and eff.written_files[saved[0]] == text  # nothing lost
    assert f"read_file `{saved[0]}:<first>-<last>`" in fitted
    assert "1-200  starts" in fitted and "4801-5000  starts" in fitted  # spans it


@pytest.mark.asyncio
async def test_an_index_section_that_fits_is_whole_and_saves_nothing():
    text = _lines(40)
    ns = _eval_namespaces(text)
    eff = _Window(262144)
    await _fit_tail_sections(_evaluator_turn("index"), ns, eff, {}, "evaluate_outcome")
    assert ns["context"]["eval_session_tail"] == text
    assert not [p for p in eff.written_files if p.startswith(".agent/outputs/")]


# ── the diagnosis seed ──────────────────────────────────────────────────


def _seed_input(effects, **context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="start_session", attempt=1),
        effects=effects,
    )


async def _seed(effects, **context) -> str:
    out = await action_start_diagnosis_session(_seed_input(effects, **context))
    assert out.result["session_started"] is True
    return "\n".join(out.context_updates[INJECTION_KEY])


_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "engine.py", line 12, in move\n'
    "    room = world[dest]\n"
    "KeyError: 'attic'\n"
)


@pytest.mark.asyncio
async def test_the_seed_transcript_is_whole_when_it_fits():
    eff = _Window(32768)
    seed = await _seed(eff, goal_description="Move north", error_output=_TRACEBACK)
    assert "## Transcript" in seed and _TRACEBACK.strip() in seed
    assert ".agent/outputs" not in seed
    assert not [p for p in eff.written_files if p.startswith(".agent/outputs/")]


@pytest.mark.asyncio
async def test_a_transcript_too_large_for_the_session_is_saved_and_indexed():
    log = _lines(1500) + "\n" + _TRACEBACK  # ~90k chars: over a 16k window's share
    eff = _Window(16384)
    seed = await _seed(eff, goal_description="Move north", error_output=log)
    assert "the transcript is too large to show whole here" in seed
    saved = [p for p in eff.written_files if p.startswith(".agent/outputs/diagnose-")]
    assert len(saved) == 1 and saved[0].endswith("-transcript.txt")
    assert eff.written_files[saved[0]] == log  # the whole is on disk
    # The way back is the diagnosis's own verb, not a tool it does not have.
    assert f"read a range with trace `{saved[0]}:<first>-<last>`" in seed
    assert "read_file" not in seed
    # The traceback still reaches the model whole, in its own section.
    assert "## What crashed" in seed and "KeyError: 'attic'" in seed
    assert "starts: line 1401" in seed  # the index reaches the end


@pytest.mark.asyncio
async def test_the_seed_shows_every_prior_attempt():
    attempts = [
        {"target_file": f"f{i}.py", "target_symbol": "g", "pre_headline": f"h{i}"}
        for i in range(9)
    ]
    seed = await _seed(
        _Window(32768), goal_description="x", failed_attempts_context=attempts
    )
    assert "9 previous fix attempts" in seed
    assert "Attempt 1: patched f0.py:g" in seed  # the last-6 cap dropped these
    assert "Attempt 3: patched f2.py:g" in seed
    assert "Attempt 9: patched f8.py:g" in seed


_TESTS_SRC = (
    "def test_a():\n    assert f(1) == 2\n\n\n"
    "def test_b():\n"
    '    """' + ("A long docstring. " * 120) + '"""\n'
    "    assert g(2) == 3\n    assert LAST_LINE_OF_B\n\n\n"
    "def test_c():\n    assert h() is None\n"
)
_PYTEST_OUT = (
    "FAILED tests/test_x.py::test_a - AssertionError\n"
    "FAILED tests/test_x.py::test_b - AssertionError\n"
    "FAILED tests/test_x.py::test_c - AssertionError\n"
)


@pytest.mark.asyncio
async def test_every_failing_test_is_seeded_whole():
    eff = MockEffects(files={"tests/test_x.py": _TESTS_SRC})
    block = await _failing_test_block(eff, _PYTEST_OUT)
    assert block.count("# tests/test_x.py::test_") == 3  # not "up to 2"
    assert "LAST_LINE_OF_B" in block  # a body past 1,500 chars is not cut


# ── a trace reads a line range of any file ──────────────────────────────


def _trace_input(effects, **context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="execute_trace", attempt=1),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_a_line_range_traces_that_part_whole():
    eff = _Window(32768, files={"notes.txt": _lines(300)})
    out = await action_execute_symbol_trace(
        _trace_input(
            eff,
            diagnosis_session_id="s1",
            investigation_choice_arg="notes.txt:10-12",
            investigation_turn=2,
            traced_symbols=[],
        )
    )
    assert out.result["trace_ok"] is True
    view = out.context_updates[INJECTION_KEY][-1]
    assert view.startswith("=== notes.txt:10-12 ===\n")
    assert "line 10 " in view and "line 12 " in view and "line 13 " not in view
    assert out.context_updates["investigation_turn"] == 3
    assert out.context_updates["traced_symbols"] == ["notes.txt:10-12"]


@pytest.mark.asyncio
async def test_a_range_past_the_end_is_corrected_with_the_index():
    eff = _Window(32768, files={"notes.txt": _lines(300)})
    out = await action_execute_symbol_trace(
        _trace_input(
            eff,
            diagnosis_session_id="s1",
            investigation_choice_arg="notes.txt:900-950",
            investigation_turn=2,
        )
    )
    assert out.result["trace_ok"] is False
    msg = out.context_updates[INJECTION_KEY][-1]
    assert "no lines 900-950 in notes.txt: it has 300 lines" in msg
    assert "1-200  starts" in msg  # a wrong guess is also a map


@pytest.mark.asyncio
async def test_a_range_too_large_for_the_session_is_asked_to_narrow():
    eff = _Window(16384, files={"notes.txt": _lines(4000)})
    out = await action_execute_symbol_trace(
        _trace_input(
            eff,
            diagnosis_session_id="s1",
            investigation_choice_arg="notes.txt:1-4000",
            investigation_turn=0,
        )
    )
    assert out.result["trace_ok"] is True  # a real read, just not whole
    view = out.context_updates[INJECTION_KEY][-1]
    assert "too large to show whole here" in view
    assert "`notes.txt:1-2000` then `notes.txt:2001-4000`" in view


# ── the terminal view follows the serving window ────────────────────────


@pytest.mark.asyncio
async def test_terminal_bounds_follow_the_last_reported_window(monkeypatch):
    from agent.formatters import _RECENT_TURNS_FULL, _history_turn_max, _last_turn_max

    monkeypatch.setattr(cf, "_last_window", None)
    assert cf.known_window() == cf.UNKNOWN_WINDOW
    small = _last_turn_max()
    await cf.serving_window(_Window(262144))
    assert cf.known_window() == 262144
    assert _last_turn_max() == int(cf.SHARE * 262144 * 40 / 13) > small
    assert _history_turn_max() == _last_turn_max() // _RECENT_TURNS_FULL
