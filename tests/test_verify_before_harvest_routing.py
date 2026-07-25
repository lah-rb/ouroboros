"""Compiled routing for the quality gate's Phase-4 verification loop.

These tests evaluate the COMPILED resolvers (flows/compiled.json), so they
pin what the runtime actually executes: findings enter verification in
completion mode regardless of the model's verdict (the verdict/findings
coupling seam), checkpoint mode keeps the direct pass/fail routing (smoke
runs the gate in checkpoint), and every probe outcome loops or exits
through the flush steps.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.resolvers.rule import resolve_rule
from tests.conftest import StubStepOutput as _Out

_COMPILED = json.loads(
    (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
)


def _resolver(step: str) -> dict:
    return _COMPILED["quality_gate"]["steps"][step]["resolver"]


def _route(step: str, result: dict, context: dict | None = None) -> str:
    return resolve_rule(
        _resolver(step), step_output=_Out(result), context=context or {}, meta={}
    )


# ── evaluate_results: verification entry ─────────────────────────────


def test_findings_enter_verification_in_completion_mode():
    target = _route(
        "evaluate_results",
        {"all_passing": False, "has_findings": True},
        context={"mode": "completion"},
    )
    assert target == "prepare_finding_verification"


def test_model_pass_with_findings_still_enters_verification():
    # The a21a8c finale: verdict "pass" alongside non-empty blocking_issues.
    # Verification decouples that — claims get checked either way.
    target = _route(
        "evaluate_results",
        {"all_passing": True, "has_findings": True},
        context={"mode": "completion"},
    )
    assert target == "prepare_finding_verification"


def test_checkpoint_mode_keeps_direct_routing():
    assert (
        _route(
            "evaluate_results",
            {"all_passing": False, "has_findings": True},
            context={"mode": "checkpoint"},
        )
        == "gate_fail"
    )
    assert (
        _route(
            "evaluate_results",
            {"all_passing": True, "has_findings": False},
            context={"mode": "checkpoint"},
        )
        == "gate_pass"
    )


def test_no_findings_routes_directly():
    assert (
        _route(
            "evaluate_results",
            {"all_passing": True, "has_findings": False},
            context={"mode": "completion"},
        )
        == "gate_pass"
    )


# ── the probe loop ────────────────────────────────────────────────────


def test_prepare_routes_to_probe_or_apply():
    assert _route("prepare_finding_verification", {"has_next": True}) == "run_probe"
    assert (
        _route("prepare_finding_verification", {"has_next": False})
        == "apply_verification_results"
    )


def test_probe_success_judges_failure_records_error():
    assert _route("run_probe", {"status": "success"}) == "judge_finding"
    assert _route("run_probe", {"status": "failed"}) == "record_probe_error"


def test_judge_empty_response_records_error():
    assert _route("judge_finding", {"tokens_generated": 42}) == "record_and_advance"
    assert _route("judge_finding", {"tokens_generated": 0}) == "record_probe_error"


def test_record_loops_through_flush_or_exits():
    for step in ("record_and_advance", "record_probe_error"):
        assert _route(step, {"has_next": True}) == "flush_transient_probe"
        assert _route(step, {"has_next": False}) == "flush_transient_final"


def test_flush_steps_continue_unconditionally():
    assert _route("flush_transient_probe", {}) == "run_probe"
    assert _route("flush_transient_final", {}) == "apply_verification_results"


def test_apply_routes_on_derived_verdict():
    assert _route("apply_verification_results", {"all_passing": True}) == "gate_pass"
    assert _route("apply_verification_results", {"all_passing": False}) == "gate_fail"


# ── wiring sanity ─────────────────────────────────────────────────────


def test_probe_step_uses_run_commands_with_probe_keys():
    step = _COMPILED["quality_gate"]["steps"]["run_probe"]
    assert step["flow"] == "run_commands"
    assert step["input_map"]["commands"] == {"$ref": "context.probe_commands"}
    assert step["input_map"]["stop_on_error"] is False
    assert step["publishes"] == ["terminal_output"]


def test_record_probe_error_sets_probe_failed():
    step = _COMPILED["quality_gate"]["steps"]["record_probe_error"]
    assert step["params"]["probe_failed"] is True


def test_launch_command_declared_wherever_its_param_ref_lives():
    # Action params resolve against the FILTERED context
    # (runtime._build_step_input) — an undeclared key makes the
    # ux_launch_command $ref silently default and probes fall back to
    # the self-terminating startup command (observed live: every repro
    # line answered to bare bash).
    steps = _COMPILED["quality_gate"]["steps"]
    assert "launch_command" in steps["run_ux_verification"]["publishes"]
    for name in (
        "prepare_finding_verification",
        "record_and_advance",
        "record_probe_error",
    ):
        step = steps[name]
        assert (
            step["params"]["ux_launch_command"]["$ref"] == "context.launch_command"
        ), name
        assert "launch_command" in step["context"]["optional"], name
