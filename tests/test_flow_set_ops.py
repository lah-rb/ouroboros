"""Ops flow set — registration, phase routing, compiled wiring, e2e handoff.

Pins the contract: the task goal (type "task_exec") drives the phase machine
(task_exec while incomplete → complete when done), ops_task reuses run_session
verbatim, and the intake → phase → judge → complete handoff composes.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.actions.mission_actions import action_check_pipeline_phase
from agent.actions.operations_actions import (
    TASK_GOAL_SIGNATURE,
    action_derive_task_goal,
    action_judge_task_completion,
)
from agent.effects.mock import MockEffects
from agent.flow_sets import FLOW_SETS, get_flow_set
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState


def _mission() -> MissionState:
    return MissionState(
        objective="create a config.yaml that enables logging",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="ops"),
    )


def _si(mission, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="ops_control", step_id="x"),
        effects=MockEffects(),
    )


# ── registration + phases ─────────────────────────────────────────────


def test_ops_registered_with_entry_flow():
    assert "ops" in FLOW_SETS
    assert get_flow_set("ops").entry_flow == "ops_control"


@pytest.mark.asyncio
async def test_phase_task_exec_while_incomplete_then_complete():
    m = _mission()
    await action_derive_task_goal(_si(m))  # one incomplete task_exec goal
    out = await action_check_pipeline_phase(_si(m))
    assert out.result["phase"] == "task_exec"
    # Mark the task goal complete → phase flips to complete.
    next(g for g in m.goals if g.type == "task_exec").status = "complete"
    out2 = await action_check_pipeline_phase(_si(m))
    assert out2.result["phase"] == "complete"


# ── compiled wiring ───────────────────────────────────────────────────


def _compiled():
    with open(os.path.join("flows", "compiled.json")) as f:
        return json.load(f)


def test_compiled_ops_wiring():
    c = _compiled()
    rules = c["ops_control"]["steps"]["check_phase"]["resolver"]["rules"]
    transitions = {r["condition"]: r["transition"] for r in rules}
    assert transitions["result.phase == 'task_exec'"] == "dispatch_task"
    assert transitions["result.phase == 'complete'"] == "completed"
    # Empty definition-of-done derivation re-loops to re-derive (mandatory gate),
    # rather than storing 0 checks and running a session against no gate.
    derive_rules = c["ops_control"]["steps"]["derive_criteria"]["resolver"]["rules"]
    dt = {r["condition"]: r["transition"] for r in derive_rules}
    assert dt["result.tokens_generated > 0"] == "store_criteria"
    assert dt["true"] == "retry_setup"
    assert (
        c["ops_control"]["steps"]["retry_setup"]["tail_call"]["flow"] == "ops_control"
    )
    # After the definition-of-done, derive the OPTIONAL output-format spec, then
    # proceed. Unlike the mandatory criteria, a failed format derivation does NOT
    # re-loop — it proceeds with no format gate (the conservative default).
    ocs = c["ops_control"]["steps"]
    assert ocs["store_criteria"]["resolver"]["rules"][0]["transition"] == "derive_output_format"
    of = {r["condition"]: r["transition"] for r in ocs["derive_output_format"]["resolver"]["rules"]}
    assert of["result.tokens_generated > 0"] == "store_output_format"
    assert of["true"] == "check_phase"  # no retry — format gate is optional
    assert ocs["store_output_format"]["resolver"]["rules"][0]["transition"] == "check_phase"
    assert c["ops_control"]["steps"]["dispatch_task"]["tail_call"]["flow"] == "ops_task"
    # ops_task reuses run_session verbatim and judges completion.
    steps = c["ops_task"]["steps"]
    # Ground the cycle in the real working directory BEFORE planning, so the
    # charter plans against actual files (not blind). load_state →
    # gather_context (scan_project) → exa_probe_gate → plan_provision.
    assert (
        steps["load_state"]["resolver"]["rules"][0]["transition"] == "gather_context"
    )
    assert steps["gather_context"]["action"] == "scan_project"
    assert (
        steps["gather_context"]["resolver"]["rules"][0]["transition"] == "exa_probe_gate"
    )
    # Stuck-task external-search arm: gate (attempts>=2, once) → exa_search → store →
    # plan_provision; skips straight to plan_provision otherwise.
    eg = {r["condition"]: r["transition"] for r in steps["exa_probe_gate"]["resolver"]["rules"]}
    assert eg["result.should_search == true"] == "exa_search"
    assert eg["true"] == "plan_provision"
    assert steps["exa_search"]["action"] == "exa_search"
    assert steps["exa_search"]["resolver"]["rules"][0]["transition"] == "store_search_findings"
    assert steps["store_search_findings"]["resolver"]["rules"][0]["transition"] == "plan_provision"
    assert "search_findings_block" in steps["plan_charter"]["prompt_template"]["context_keys"]
    # The charter consumes the workspace manifest (grounded, not blind).
    assert (
        "workspace_context" in steps["plan_charter"]["prompt_template"]["context_keys"]
    )
    assert any(
        pc.get("formatter") == "format_project_listing"
        and pc.get("output_key") == "workspace_context"
        for pc in steps["plan_charter"]["pre_compute"]
    )
    # Oracle rungs: the sanity floor gates the judge; verify-before-harvest
    # re-probes the completion before "done" is harvested.
    assert steps["run_checks"]["resolver"]["rules"][0]["transition"] == "check_sanity"
    cs = {r["condition"]: r["transition"] for r in steps["check_sanity"]["resolver"]["rules"]}
    assert cs["result.check_plausibility == true"] == "sanity_plausibility"
    assert cs["true"] == "profile_oracle"  # sanity branch flows into the profile rung
    # Profile-gated rung (service/data/invertible) → grounded reground gate → output-format.
    assert steps["profile_oracle"]["action"] == "check_profile_oracle"
    assert steps["profile_oracle"]["resolver"]["rules"][0]["transition"] == "gate_reground"
    # Grounded output-format re-assessment (quality_gate port): gate fires the late
    # re-derivation only on an empty early spec (else straight to check_format), the
    # reground inference stores + marks grounded, then enforcement is check_format.
    assert steps["gate_reground"]["action"] == "gate_reground_output_format"
    gr = {r["condition"]: r["transition"] for r in steps["gate_reground"]["resolver"]["rules"]}
    assert gr["result.needs_reground == true"] == "reground_output_format"
    assert gr["true"] == "check_format"
    rof = {r["condition"]: r["transition"] for r in steps["reground_output_format"]["resolver"]["rules"]}
    assert rof["result.tokens_generated > 0"] == "store_reground_format"
    assert rof["true"] == "check_format"
    assert steps["store_reground_format"]["action"] == "store_reground_output_format"
    assert steps["store_reground_format"]["resolver"]["rules"][0]["transition"] == "check_format"
    # Output-format oracle: deterministic shape check vs the derived spec.
    assert steps["check_format"]["action"] == "check_output_format"
    assert steps["check_format"]["resolver"]["rules"][0]["transition"] == "probe_gate"
    assert steps["judge_step"]["resolver"]["rules"][0]["transition"] == "reprobe_completion"
    rp = {r["condition"]: r["transition"] for r in steps["reprobe_completion"]["resolver"]["rules"]}
    assert rp["result.do_verify == true"] == "verify_completion"
    assert rp["true"] == "decide"
    assert (
        steps["record_completion_verify"]["resolver"]["rules"][0]["transition"] == "decide"
    )
    # Provision the env BEFORE the work session (reuse project_ops's install
    # runner), so run_session stays observe-only and tb env-setup works.
    assert steps["run_provision"]["action"] == "execute_project_setup"
    pp = {r["condition"]: r["transition"] for r in steps["plan_provision"]["resolver"]["rules"]}
    assert pp["result.tokens_generated > 0"] == "run_provision"
    assert steps["run_provision"]["resolver"]["rules"][0]["transition"] == "plan_charter"
    assert steps["run_terminal"]["flow"] == "run_session"
    assert steps["run_checks"]["action"] == "run_validation_checks"
    assert steps["decide"]["action"] == "judge_task_completion"
    # Both decide branches release the memoryful inference session before
    # returning (else every ops cycle leaks an LLMVP pool instance).
    decide_t = {
        r["condition"]: r["transition"]
        for r in steps["decide"]["resolver"]["rules"]
    }
    assert decide_t["result.task_done == true"] == "end_session_success"
    assert decide_t["true"] == "end_session_loop"
    assert steps["end_session_success"]["action"] == "end_inference_session"
    assert steps["end_session_loop"]["action"] == "end_inference_session"


def test_completion_criteria_formatter_registered():
    from agent.formatters import PRE_COMPUTE_FORMATTERS

    assert "format_completion_criteria" in PRE_COMPUTE_FORMATTERS
    # It renders the {"checks": [...]} shape the reused check-runner consumes,
    # wrapping each command as ["/bin/sh", "-c", cmd] so shell syntax (quotes,
    # pipes, $(), [ ]) runs through a shell instead of being exec'd as argv.
    out = PRE_COMPUTE_FORMATTERS["format_completion_criteria"](
        {"source": [{"command": "grep -q 'x y' f"}]}, {}
    )
    assert json.loads(out) == {
        "checks": [{"command": ["/bin/sh", "-c", "grep -q 'x y' f"]}]
    }


# ── e2e handoff (pure-Python, no LLM/terminal) ────────────────────────


@pytest.mark.asyncio
async def test_ops_intake_to_complete_handoff():
    m = _mission()
    # Intake: one task goal + TaskState, definition-of-done pending.
    await action_derive_task_goal(_si(m))
    assert (await action_check_pipeline_phase(_si(m))).result["phase"] == "task_exec"

    # A work cycle finishes: checks pass + judge confirms → goal complete.
    out = await action_judge_task_completion(
        _si(
            m,
            validation_results=[{"passed": True, "required": True}],
            inference_response=json.dumps({"task_complete": True, "feedback": ""}),
        )
    )
    assert out.result["task_done"] is True
    assert (
        next(g for g in m.goals if g.finding_signature == TASK_GOAL_SIGNATURE).status
        == "complete"
    )
    # Phase now resolves to complete → ops_control would finalize.
    assert (await action_check_pipeline_phase(_si(m))).result["phase"] == "complete"
