"""Resolver routing for the goal-driven quality findings flow (mission_control).

Evaluates the COMPILED flow so these track what the runtime executes:
  - dispatch_quality_gate: success -> mark_quality_verified, else -> harvest
    (a PASS stopped finalizing the mission on 2026-08-23, when the polish phase
    landed at rank 60 above the gate; the flag it sets is what lets the ladder
    exhaust to 'complete' one cycle later, or run polish when the ceiling allows)
  - harvest_quality_findings: done -> completed, else -> check_phase
  - check_phase: phase 'quality_fix' -> quality_sweep_next
  - quality_sweep_next: needs_fix -> dispatch_quality_fix, else -> check_phase
  - dispatch_quality_fix tail-calls the diagnose/file_ops flow (work flows ->
    consume the mission cycle budget) with goal_id bound.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.resolvers.rule import resolve_rule
from tests.conftest import StubStepOutput as _Out


def _steps() -> dict:
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    return compiled["mission_control"]["steps"]


def test_gate_failure_routes_to_harvest():
    r = _steps()["dispatch_quality_gate"]["resolver"]
    assert (
        resolve_rule(r, _Out({"status": "success"}), {}, {}) == "mark_quality_verified"
    )
    assert (
        resolve_rule(r, _Out({"status": "failure"}), {}, {})
        == "harvest_quality_findings"
    )


def test_a_gate_pass_still_reaches_completion():
    """The invariant the routing change must not break.

    A pass no longer transitions straight to `completed`, so this walks the
    hop it now takes instead: set the flag, return to the router. Completion
    itself is the ladder's job (tests/test_polish_phase.py) — what matters
    here is that the gate hands control back rather than dead-ending."""
    steps = _steps()
    nxt = resolve_rule(
        steps["dispatch_quality_gate"]["resolver"], _Out({"status": "success"}), {}, {}
    )
    assert nxt in steps, f"{nxt} is not a step in mission_control"
    assert (
        resolve_rule(steps[nxt]["resolver"], _Out({}), {}, {}) == "check_phase"
    ), "the flag setter must return to the router"


def test_harvest_routing():
    r = _steps()["harvest_quality_findings"]["resolver"]
    assert resolve_rule(r, _Out({"done": True}), {}, {}) == "completed"
    assert resolve_rule(r, _Out({"harvested": True}), {}, {}) == "check_phase"


def test_quality_fix_phase_routes_to_sweep():
    r = _steps()["check_phase"]["resolver"]
    assert (
        resolve_rule(r, _Out({"phase": "quality_fix"}), {"mission": None}, {})
        == "quality_sweep_next"
    )
    assert (
        resolve_rule(r, _Out({"phase": "quality"}), {"mission": None}, {})
        == "dispatch_quality_gate"
    )


def test_quality_sweep_routing():
    r = _steps()["quality_sweep_next"]["resolver"]
    assert resolve_rule(r, _Out({"needs_fix": True}), {}, {}) == "dispatch_quality_fix"
    assert resolve_rule(r, _Out({"sweep_complete": True}), {}, {}) == "check_phase"


def test_dispatch_quality_fix_tailcalls_with_goal_id():
    tc = _steps()["dispatch_quality_fix"]["tail_call"]
    assert tc["flow"] == {"$ref": "context.dispatch_config.flow"}
    im = tc["input_map"]
    for key in ("goal_id", "flow_directive", "target_file_path", "change_spec"):
        assert key in im


def test_steps_present_and_old_loop_retired():
    steps = _steps()
    assert "harvest_quality_findings" in steps
    assert "quality_sweep_next" in steps
    assert "dispatch_quality_fix" in steps
    assert "quality_fix_next" not in steps  # transient loop retired
