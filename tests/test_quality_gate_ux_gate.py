"""The quality gate's behavioral/UX phase gate must actually open.

For the entire life of the quality gate, the UX-verification phase
(plan_ux_charter -> run_ux_verification -> evaluate_ux_session) was dead:
``run_startup_check`` gated it on ``result.status == 'success' and
result.all_passed == true``, but ``all_passed`` arrives from the
``run_commands`` sub-flow's ``returns`` block, which the runtime stores under
``result["_returns"]`` — and ``_DotDict`` refuses to dot-access underscore
keys, so ``result.all_passed`` was ALWAYS ``None`` and the gate ALWAYS fell
through to ``summarize``. The first model to ever complete the challenge
(qwen3.5-122b) sailed through with a shipped dialogue-repeat bug and an
unreachable room precisely because this phase never ran.

The fix publishes ``all_passed`` into context and gates on
``context.all_passed``. These tests evaluate the COMPILED resolver so they'd
fail against the old ``result.all_passed`` wiring and stay honest about what
the runtime actually executes.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.resolvers.rule import resolve_rule


def _startup_resolver() -> dict:
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    return compiled["quality_gate"]["steps"]["run_startup_check"]["resolver"]


class _Out:
    def __init__(self, result: dict) -> None:
        self.result = result


def test_successful_startup_routes_through_boot_liveness_floor():
    """The startup sub-flow completing (status success) hits the deterministic
    boot-liveness floor before the explorer — an exit-0 boot that printed a
    traceback must not reach the UX session (ops sanity-rung port)."""
    target = resolve_rule(
        _startup_resolver(),
        step_output=_Out({"status": "success"}),
        context={},
        meta={},
    )
    assert target == "check_boot_liveness"


def test_boot_liveness_routes_clean_to_ux_dirty_to_summarize():
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    resolver = compiled["quality_gate"]["steps"]["check_boot_liveness"]["resolver"]
    assert (
        resolve_rule(resolver, step_output=_Out({"boot_clean": True}), context={}, meta={})
        == "plan_ux_charter"
    )
    assert (
        resolve_rule(resolver, step_output=_Out({"boot_clean": False}), context={}, meta={})
        == "summarize"
    )


def test_failed_terminal_skips_ux_phase():
    """If the terminal couldn't even start (status != success), skip to summarize —
    no point exploring something that never launched."""
    target = resolve_rule(
        _startup_resolver(),
        step_output=_Out({"status": "failed"}),
        context={},
        meta={},
    )
    assert target == "summarize"


def test_gate_uses_status_not_the_unreadable_returns_path():
    """Guard against regressing to result.all_passed / result._returns.* — those
    are unreachable (buried under _returns; _DotDict won't dot-access underscore
    keys), which is exactly what kept the whole UX phase dead. The gate must key
    off result.status, the proven action:flow propagation path."""
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    cond = compiled["quality_gate"]["steps"]["run_startup_check"]["resolver"]["rules"][
        0
    ]["condition"]
    assert "result.status == 'success'" in cond
    assert "all_passed" not in cond
    assert "_returns" not in cond


# ── Quality-fix re-entry routing (mission_control.check_phase) ───────────
#
# The old convergence guard (phase=='quality' + last_status=='quality_failed'
# -> completed) was a stopgap that finalized findings unaddressed. It is
# REPLACED by the quality-fix loop: gate failure routes to quality_fix_next,
# and check_phase routes mid-loop re-entries (mission.quality_fixing True) back
# to the orchestrator. These tests lock in the new routing and assert the old
# guard is gone.


class _Mission:
    def __init__(self, quality_fixing: bool = False) -> None:
        self.quality_fixing = quality_fixing


def _check_phase_resolver() -> dict:
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    return compiled["mission_control"]["steps"]["check_phase"]["resolver"]


def test_quality_fix_phase_routes_to_sweep():
    """Incomplete quality goals -> phase 'quality_fix' -> the quality sweep."""
    target = resolve_rule(
        _check_phase_resolver(),
        step_output=_Out({"phase": "quality_fix"}),
        context={"mission": _Mission()},
        meta={},
    )
    assert target == "quality_sweep_next"


def test_all_complete_dispatches_gate():
    """phase 'quality' (all goals complete) -> run the gate."""
    target = resolve_rule(
        _check_phase_resolver(),
        step_output=_Out({"phase": "quality"}),
        context={"mission": _Mission()},
        meta={},
    )
    assert target == "dispatch_quality_gate"


def test_check_phase_quality_rule_is_null_safe():
    """The re-entry condition must not throw when mission is absent."""
    target = resolve_rule(
        _check_phase_resolver(),
        step_output=_Out({"phase": "quality"}),
        context={},
        meta={},
    )
    assert target == "dispatch_quality_gate"


def test_old_convergence_guard_is_gone():
    """No check_phase rule may finalize on last_status=='quality_failed' — that
    stopgap is superseded by the diagnose-driven fix loop."""
    rules = _check_phase_resolver()["rules"]
    assert not any(
        "quality_failed" in r["condition"] and r["transition"] == "completed"
        for r in rules
    )


def test_regression_routes_to_functional_sweep():
    """A real regression flips a goal incomplete -> phase 'functional' -> the
    functional sweep, regardless of any quality-fix flag (phase-gated)."""
    target = resolve_rule(
        _check_phase_resolver(),
        step_output=_Out({"phase": "functional"}),
        context={"mission": _Mission(quality_fixing=True)},
        meta={},
    )
    assert target == "functional_sweep_next"
