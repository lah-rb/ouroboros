"""Escalation layer v1 — the shared mid-flow recovery primitive.

Pins: the session opens with a fact-only seed; tool executors queue
observations and spend the turn budget while corrections (missing file,
guard-rejected write) spend only the corrections counter; writes ride
guarded_write_file (anti-gut + scaffold parse floor apply to escalations);
conclude is fail-safe (unparseable → deferred); and the compiled wiring —
escalate's loop + file_ops' self_correct handing to escalate with
resolved→lookup_env / deferred→check_diagnose_budget.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.actions.escalation_actions import (
    MAX_ESCALATION_CORRECTIONS,
    MAX_ESCALATION_TURNS,
    action_conclude_escalation,
    action_escalation_fold_search,
    action_escalation_read,
    action_escalation_run,
    action_escalation_propose,
    action_escalation_write,
    action_open_escalation_session,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput

VALID_TOML = '[project]\nname = "x"\nversion = "0.1"\n'


def _si(fx=None, inputs=None, **ctx) -> StepInput:
    return StepInput(
        context=ctx,
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="escalate", step_id="x"),
        effects=fx if fx is not None else MockEffects(),
    )


def _queued(out) -> str:
    """The session-injection queue an action left in context_updates."""
    return "\n\n".join(out.context_updates.get("session_injections", []) or [])


# ── open session ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_session_seeds_facts():
    out = await action_open_escalation_session(
        _si(
            inputs={
                "invoking_flow": "file_ops",
                "failure_evidence": "[FAIL] syntax: x.py — SyntaxError line 3",
                "expected_outcome": "The validation checks pass.",
                "target_file_path": "x.py",
            }
        )
    )
    assert out.result["session_started"] is True
    assert out.context_updates["escalation_turn"] == 0
    assert out.context_updates["inference_session_id"]
    seed = _queued(out)
    assert "file_ops" in seed and "SyntaxError line 3" in seed
    assert "Expected outcome" in seed and "x.py" in seed


# ── tool executors: observations spend turns, corrections don't ───────


@pytest.mark.asyncio
async def test_read_observes_and_spends_turn():
    fx = MockEffects(files={"x.py": "def f():\n    return 1\n"})
    out = await action_escalation_read(
        _si(fx, escalation_choice_arg="x.py", escalation_turn=0)
    )
    assert out.result["action_ok"] is True
    assert out.context_updates["escalation_turn"] == 1
    assert "def f()" in _queued(out)


@pytest.mark.asyncio
async def test_read_missing_file_is_correction_not_turn():
    out = await action_escalation_read(
        _si(MockEffects(), escalation_choice_arg="nope.py", escalation_turn=2)
    )
    assert out.result["action_ok"] is False
    assert out.context_updates["escalation_corrections"] == 1
    assert "escalation_turn" not in out.context_updates  # budget untouched
    assert "does not exist" in _queued(out)


@pytest.mark.asyncio
async def test_run_observes_exit_and_output():
    fx = MockEffects(
        commands={
            "/bin/sh": CommandResult(
                return_code=1, stdout="1 failed", stderr="boom", command="pytest"
            )
        }
    )
    out = await action_escalation_run(
        _si(fx, escalation_choice_arg="pytest -q", escalation_turn=0)
    )
    assert out.result["action_ok"] is True
    q = _queued(out)
    assert "$ pytest -q" in q and "[exit 1]" in q and "boom" in q


@pytest.mark.asyncio
async def test_corrections_cap_signals_exhausted():
    out = await action_escalation_read(
        _si(
            MockEffects(),
            escalation_choice_arg="nope.py",
            escalation_corrections=MAX_ESCALATION_CORRECTIONS - 1,
        )
    )
    assert out.result["exhausted"] is True


# ── write: fenced body through the guarded path ───────────────────────


# ══════════════════════════════════════════════════════════════════════
# ESCALATION IS READ-ONLY (operator ruling, 2026-08-11)
#
# These replace the write_file tests. Escalation used to write via
# guarded_write_file, and that is how a working 17-method engine.py became a
# 2-method stub: an escalation write emitted a body ending "(rest of file
# unchanged)" and the guard accepted it at 24.2% retention — four points above
# its anti-gut floor, because that floor is a PER-WRITE ratio with no memory of
# the file's original shape. Later writes measured against the wreck and looked
# healthy at 59%. The threshold was not the defect: a recovery loop that can
# write is a second authoring path around the flows that own file edits, with
# none of their review. It now proposes; the owning flow decides.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_propose_records_an_advisory_and_writes_nothing():
    fx = MockEffects(files={})
    raw = (
        '{"choice": "propose_fix", "path": "src/x.py"}\n'
        "```python\n# === FILE: src/x.py ===\ndef f():\n    return 2\n```\n"
    )
    out = await action_escalation_propose(
        _si(
            fx,
            inference_response=raw,
            escalation_choice_arg="src/x.py",
            escalation_turn=0,
        )
    )
    props = out.context_updates["escalation_proposals"]
    assert [x["path"] for x in props] == ["src/x.py"]
    assert "def f()" in props[0]["content"]
    assert fx._files == {}, "escalation must not touch disk"


@pytest.mark.asyncio
async def test_propose_refuses_an_abbreviated_body():
    """The exact shape that destroyed a file — a body standing in for what it
    omits. Worse than no proposal: whoever applies it cannot tell."""
    fx = MockEffects(files={})
    raw = (
        '{"choice": "propose_fix", "path": "engine.py"}\n'
        "```python\n# === FILE: engine.py ===\nclass GameEngine:\n"
        "    def _handle_move(self, d):\n        ...\n\n"
        "        (rest of file unchanged)\n```\n"
    )
    out = await action_escalation_propose(
        _si(fx, inference_response=raw, escalation_choice_arg="engine.py")
    )
    q = _queued(out)
    assert "ABBREVIATED" in q and "rest of file unchanged" in q
    assert fx._files == {}


@pytest.mark.asyncio
async def test_propose_without_fence_is_correction():
    out = await action_escalation_propose(
        _si(
            MockEffects(),
            inference_response='{"choice": "propose_fix", "path": "x.py"}',
            escalation_choice_arg="",
        )
    )
    assert "fenced code block" in _queued(out)


@pytest.mark.asyncio
async def test_the_retired_write_action_refuses_instead_of_writing():
    """Registered still, so a stale route fails loudly rather than resolving
    to no action."""
    fx = MockEffects(files={"x.py": "original"})
    out = await action_escalation_write(
        _si(
            fx,
            inference_response=(
                '{"choice": "write_file", "path": "x.py"}\n'
                "```python\n# === FILE: x.py ===\nstub\n```\n"
            ),
            escalation_choice_arg="x.py",
        )
    )
    assert out.result.get("refused") is True
    assert fx._files["x.py"] == "original", "retired action must not write"


@pytest.mark.asyncio
async def test_conclude_resolved():
    fx = MockEffects(
        inference_responses=[
            '```json\n{"outcome": "resolved", "summary": "fixed the import", "key_change": "x.py"}\n```'
        ]
    )
    out = await action_conclude_escalation(
        _si(fx, escalation_session_id="s1", escalation_files=["x.py"])
    )
    assert out.result["outcome"] == "resolved"
    assert out.context_updates["files_changed"] == ["x.py"]
    assert "fixed the import" in out.context_updates["escalation_summary"]


@pytest.mark.asyncio
async def test_conclude_unparseable_defers():
    fx = MockEffects(inference_responses=["I think it's probably fine now?"])
    out = await action_conclude_escalation(_si(fx, escalation_session_id="s1"))
    assert out.result["outcome"] == "deferred"
    assert "fail-safe" in out.context_updates["escalation_summary"]


# ── compiled wiring ───────────────────────────────────────────────────


def _compiled():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "flows", "compiled.json")) as f:
        return json.load(f)


def test_escalate_flow_wiring():
    c = _compiled()["escalate"]
    steps = c["steps"]
    assert c["entry"] == "start_session"
    # menu options route to their executors; fallbacks conclude
    opts = steps["work"]["turn"]["transitions"]["options"]
    assert opts == {
        "read_file": "do_read",
        "run_command": "do_run",
        "propose_fix": "do_propose",
        "web_search": "do_web_search",
        "consult_boss": "do_consult",
        "conclude": "conclude",
    }
    assert steps["work"]["turn"]["transitions"]["no_answer"] == "conclude"
    # budget rule number agrees with the Python constant (no 8-vs-10 drift)
    budget_cond = steps["check_budget"]["resolver"]["rules"][0]["condition"]
    assert f">= {MAX_ESCALATION_TURNS}" in budget_cond
    # executors loop through the budget gate; exhausted corrections conclude
    for s in ("do_read", "do_run", "do_propose"):
        rules = {r["condition"]: r["transition"] for r in steps[s]["resolver"]["rules"]}
        assert rules["result.exhausted == true"] == "conclude"
        assert rules["true"] == "check_budget"
    # typed terminals
    assert steps["resolved"]["status"] == "resolved"
    assert steps["deferred"]["status"] == "deferred"
    cr = {
        r["condition"]: r["transition"] for r in steps["conclude"]["resolver"]["rules"]
    }
    assert cr["result.outcome == 'resolved'"] == "end_session_resolved"


@pytest.mark.asyncio
async def test_fold_search_injects_summary_and_spends_a_turn():
    out = await action_escalation_fold_search(
        _si(research_summary="X raises ValueError (https://docs/x).", escalation_turn=1)
    )
    assert out.result["action_ok"] is True
    assert out.context_updates["escalation_turn"] == 2  # one web_search = one turn
    assert "ValueError" in _queued(out)


@pytest.mark.asyncio
async def test_fold_search_empty_summary_still_spends_a_turn():
    out = await action_escalation_fold_search(
        _si(research_summary="", escalation_turn=0)
    )
    assert out.result["action_ok"] is True
    assert out.context_updates["escalation_turn"] == 1
    assert "no usable findings" in _queued(out)


def test_escalate_work_menu_has_web_search():
    steps = _compiled()["escalate"]["steps"]
    opts = steps["work"]["turn"]["transitions"]["options"]
    assert opts["web_search"] == "do_web_search"
    # web_search spends a normal escalation turn via fold_search → check_budget
    fold_targets = {
        r["condition"]: r["transition"]
        for r in steps["fold_search"]["resolver"]["rules"]
    }
    assert fold_targets["true"] == "check_budget"


def test_file_ops_self_correct_escalates():
    steps = _compiled()["file_ops"]["steps"]
    sc = steps["self_correct"]
    assert sc["flow"] == "escalate"
    assert sc["input_map"]["failure_evidence"]["$ref"] == "context.validation_output"
    rules = {r["condition"]: r["transition"] for r in sc["resolver"]["rules"]}
    assert rules["result.status == 'resolved'"] == "lookup_env"  # re-validate
    assert rules["true"] == "check_diagnose_budget"  # never a dead end
    # the retry budget + oversized-symbol diagnose routing are intact
    cr = {
        r["condition"]: r["transition"]
        for r in steps["check_retry"]["resolver"]["rules"]
    }
    assert cr["meta.attempt <= 2"] == "self_correct"
    rc = {
        r["condition"]: r["transition"]
        for r in steps["run_checks"]["resolver"]["rules"]
    }
    assert (
        rc["result.syntax_failed == true and result.oversized_symbol_fix == true"]
        == "check_diagnose_budget"
    )
