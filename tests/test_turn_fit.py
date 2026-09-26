"""`fit: "tail"` — a section sized to the serving model's real window.

The stateless evaluator used to cut its transcript to a fixed 16,000
characters: sized for the 32k-window arms (hy3, muse), it threw evidence
away on a 262k window and still guessed at the rest of the prompt. The
runtime now measures the prompt without the section (the serving model's
own tokenizer, or the chars x 13/40 estimate when the server won't count),
reserves the output (the turn's max_tokens, else 8k), keeps the most recent
part of the section that fits — from a line boundary — and says what it
left out.
"""

from __future__ import annotations

import copy
import json
import logging
import os

import pytest

from agent.effects.mock import MockEffects
from agent.models import Section, TurnDefinition
from agent.context_fit import UNKNOWN_WINDOW as _FIT_UNKNOWN_WINDOW
from agent.runtime import (
    _FIT_OUTPUT_RESERVE,
    _fit_tail_sections,
    _get_turn_renderer,
)
from agent.scheduler.capacity_model import TOKENIZE_MARGIN
from agent.turn_renderer import TurnRenderError

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _compiled_step(name: str) -> dict:
    with open(os.path.join(_ROOT, "flows", "compiled.json")) as f:
        return copy.deepcopy(json.load(f)["interact"]["steps"][name])


def _evaluator_turn() -> TurnDefinition:
    return TurnDefinition.model_validate(_compiled_step("evaluate_outcome")["turn"])


def _transcript(turns: int) -> str:
    return "".join(
        f"[Turn {i}] (send_input)\n  > look\nRoom {i}: a long corridor of stone.\n>\n"
        for i in range(turns)
    )


def _namespaces(transcript: str) -> dict:
    return {
        "input": {},
        "context": {
            "eval_session_tail": transcript,
            "eval_objective": "---TEST OBJECTIVE---\nLook works.\n---END TEST OBJECTIVE---",
        },
        "meta": {"flow_name": "interact", "step_id": "evaluate_outcome"},
    }


def _tok(text: str) -> int:
    return len(text) // 4


class _Server(MockEffects):
    """A server that reports its window and counts ~4 chars per token."""

    def __init__(self, n_ctx: int | None) -> None:
        super().__init__()
        self._n_ctx = n_ctx

    async def cache_health(self) -> dict:
        return {"nCtxSeq": self._n_ctx} if self._n_ctx else {}

    async def token_count(self, texts: list[str], model: str = "") -> list[int]:
        return [_tok(t) for t in texts]


def _fits(turn: TurnDefinition, ns: dict, window: int, reserve: int) -> bool:
    prompt = _get_turn_renderer().render(turn, ns)
    return int(_tok(prompt) * TOKENIZE_MARGIN) + reserve <= window


@pytest.mark.asyncio
async def test_a_transcript_that_fits_is_left_whole():
    turn, text = _evaluator_turn(), _transcript(400)
    ns = _namespaces(text)
    await _fit_tail_sections(turn, ns, _Server(262144), {}, "evaluate_outcome")
    assert ns["context"]["eval_session_tail"] == text


@pytest.mark.asyncio
async def test_a_small_window_keeps_the_latest_lines_and_says_so():
    turn, text = _evaluator_turn(), _transcript(4000)
    ns = _namespaces(text)
    await _fit_tail_sections(turn, ns, _Server(16384), {}, "evaluate_outcome")
    fitted = ns["context"]["eval_session_tail"]
    marker, _, kept = fitted.partition("\n")
    assert marker.startswith("[… the first ") and "16,384-token window" in marker
    omitted = int(marker.split("the first ")[1].split(" ")[0].replace(",", ""))
    assert omitted == len(text) - len(kept)
    assert text[len(text) - len(kept) - 1] == "\n"  # a line boundary, never mid-line
    assert text.endswith(kept)  # the most recent stretch
    assert "[Turn 3999]" in kept and "[Turn 0]" not in kept
    assert _fits(turn, ns, 16384, _FIT_OUTPUT_RESERVE)


@pytest.mark.asyncio
async def test_the_turns_own_max_tokens_is_the_reserve():
    turn, text = _evaluator_turn(), _transcript(4000)
    default_ns, small_ns = _namespaces(text), _namespaces(text)
    await _fit_tail_sections(turn, default_ns, _Server(16384), {}, "e")
    await _fit_tail_sections(turn, small_ns, _Server(16384), {"max_tokens": 2000}, "e")
    assert len(small_ns["context"]["eval_session_tail"]) > len(
        default_ns["context"]["eval_session_tail"]
    )
    assert _fits(turn, small_ns, 16384, 2000)


@pytest.mark.asyncio
async def test_an_unreported_window_assumes_the_smallest_served(caplog):
    turn, text = _evaluator_turn(), _transcript(20000)
    ns = _namespaces(text)
    with caplog.at_level(logging.INFO, logger="agent.context_fit"):
        await _fit_tail_sections(turn, ns, _Server(None), {}, "evaluate_outcome")
    assert f"{_FIT_UNKNOWN_WINDOW:,}-token window" in ns["context"]["eval_session_tail"]
    assert "assumed" in caplog.text
    assert _fits(turn, ns, _FIT_UNKNOWN_WINDOW, _FIT_OUTPUT_RESERVE)


@pytest.mark.asyncio
async def test_without_token_counts_it_sizes_by_the_estimate(caplog):
    class _NoCount(_Server):
        async def token_count(self, texts, model=""):
            return []

    turn, text = _evaluator_turn(), _transcript(4000)
    ns = _namespaces(text)
    with caplog.at_level(logging.INFO, logger="agent.context_fit"):
        await _fit_tail_sections(turn, ns, _NoCount(16384), {}, "evaluate_outcome")
    assert "estimated counts" in caplog.text
    prompt = _get_turn_renderer().render(turn, ns)
    assert (len(prompt) * 13) // 40 + _FIT_OUTPUT_RESERVE <= 16384


@pytest.mark.asyncio
async def test_a_fit_needs_a_top_level_ref():
    turn = TurnDefinition.model_validate(
        {
            "response_shape": "json_document",
            "sections": [
                {"type": "evidence", "ref": {"$ref": "context.a.b"}, "fit": "tail"},
                {"type": "envelope"},
            ],
            "response": {"schema_id": "evaluation"},
            "transitions": {"default": "x", "no_answer": "x"},
        }
    )
    with pytest.raises(TurnRenderError, match="top-level"):
        await _fit_tail_sections(
            turn, {"context": {"a": {"b": "t"}}}, _Server(16384), {}, "s"
        )


def test_an_unfitted_section_has_no_fit():
    assert Section(type="evidence", ref={"$ref": "context.x"}).fit is None


@pytest.mark.asyncio
async def test_the_runtime_fits_before_it_sends():
    """End to end through execute_flow: the prompt the stateless evaluation
    SENDS carries the marker and the latest turns, not the first ones."""
    from agent.actions.registry import build_action_registry
    from agent.models import FlowDefinition
    from agent.runtime import execute_flow

    steps = {
        "evaluate_outcome": _compiled_step("evaluate_outcome"),
        "stop": {
            "action": "noop",
            "description": "end",
            "terminal": True,
            "status": "success",
        },
    }
    steps["evaluate_outcome"]["turn"]["transitions"] = {
        "default": "stop",
        "no_answer": "stop",
    }
    sent: list[str] = []

    class _Recording(_Server):
        async def run_inference(self, prompt, config_overrides=None, **kw):
            sent.append(prompt)
            return await super().run_inference(prompt, config_overrides, **kw)

    fx = _Recording(16384)
    fx._inference_responses = [
        '```json\n{"goal_met": true, "headline": "h", "summary": "s"}\n```'
    ]
    await execute_flow(
        flow_def=FlowDefinition.model_validate(
            {"flow": "interact", "entry": "evaluate_outcome", "steps": steps}
        ),
        inputs={
            "terminal_output": _transcript(4000),
            "flow_directive": "Test this capability: Look works.",
        },
        action_registry=build_action_registry(),
        effects=fx,
    )
    (prompt,) = sent
    assert "[… the first " in prompt
    assert "[Turn 3999]" in prompt and "[Turn 0] " not in prompt
