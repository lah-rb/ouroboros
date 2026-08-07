"""Ops → code_core ports: ledger, boot floor, asym-probe, stuck search, DoD.

Pins the five incorporations from the ops flow set into code_core:
  C1 workspace_ledger — project_ops report path records durable provisions;
     the setup projection renders the "already done" block.
  C2 boot-liveness floor — an exit-0 startup that printed an error trace
     appends a REQUIRED fail and skips the UX session.
  C3 asym-probe in quality_gate — solver-shaped objectives with passing
     checks get a property test; checkpoint mode suppresses it.
  C4 per-goal acceptance checks — derived once grounded per goal,
     tighten-only, deterministic veto on the evaluator's goal_met, never
     vacuous.
  C5 stuck-goal web search — one-shot exa gate at >= 2 failed attempts;
     hits stored on the goal and surfaced by the diagnose seed.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.actions.diagnosis_session_actions import (
    action_goal_search_gate,
    action_store_goal_search_findings,
)
from agent.actions.operations_actions import action_detect_solver_task
from agent.actions.oracle_actions import action_check_boot_liveness
from agent.actions.pipeline_actions import (
    action_apply_acceptance_verdict,
    action_gate_goal_acceptance,
    action_store_goal_acceptance,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    FailedAttempt,
    GoalRecord,
    MissionConfig,
    MissionState,
)


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="build a text adventure",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=goals or [],
    )


def _si(effects=None, inputs=None, **ctx) -> StepInput:
    return StepInput(
        context=ctx,
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="x", step_id="x"),
        effects=effects if effects is not None else MockEffects(),
    )


def _compiled():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "flows", "compiled.json")) as f:
        return json.load(f)


# ── C1: workspace ledger ──────────────────────────────────────────────


def test_add_ledger_entry_dedupes_and_caps():
    m = _mission()
    assert m.add_ledger_entry(
        cycle=0, kind="provision", description="pip install x", status="success"
    )
    # Re-reporting the same successful provision: deduped.
    assert not m.add_ledger_entry(
        cycle=1, kind="provision", description="pip install x", status="success"
    )
    # A FAILED retry of a known entry still records.
    assert m.add_ledger_entry(
        cycle=2, kind="provision", description="pip install x", status="failed"
    )
    # dedupe=False (per-cycle narrative) always appends.
    assert m.add_ledger_entry(
        cycle=3, kind="session", description="n", status="attempt", dedupe=False
    )
    assert m.add_ledger_entry(
        cycle=4, kind="session", description="n", status="attempt", dedupe=False
    )
    # Cap at 60, most recent kept.
    for i in range(70):
        m.add_ledger_entry(
            cycle=i, kind="provision", description=f"e{i}", status="success"
        )
    assert len(m.workspace_ledger) == 60


@pytest.mark.asyncio
async def test_project_ops_report_records_ledger_entry():
    from agent.actions.reporting_actions import action_attach_directive_report

    goal = GoalRecord(description="env", status="incomplete", type="structural")
    m = _mission([goal])
    fx = MockEffects(mission=m)
    si = StepInput(
        context={
            "mission": m,
            "last_goal_id": goal.id,
            "last_status": "success",
            "last_result": {
                "directive_report": {
                    "flow": "project_ops",
                    "status": "success",
                    "summary": "installed pytest and created pyproject.toml",
                }
            },
        },
        inputs={},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="apply_last_result"),
        effects=fx,
    )
    await action_attach_directive_report(si)
    assert m.environment_verified is True
    provisions = [e for e in m.workspace_ledger if e.kind == "provision"]
    assert len(provisions) == 1
    assert "installed pytest" in provisions[0].description


def test_setup_projection_renders_ledger_block():
    from agent.projections import MATERIALIZER_REGISTRY

    m = _mission()
    m.add_ledger_entry(
        cycle=0, kind="provision", description="pip install rich", status="success"
    )
    ctx = MATERIALIZER_REGISTRY["project_setup_context"](m, {})
    assert "pip install rich" in ctx["workspace_ledger_block"]
    assert "ALREADY DONE THIS MISSION" in ctx["workspace_ledger_block"]
    # Empty ledger → empty block (section omits cleanly).
    assert (
        MATERIALIZER_REGISTRY["project_setup_context"](_mission(), {})[
            "workspace_ledger_block"
        ]
        == ""
    )


# ── C2: boot-liveness floor ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_boot_liveness_flags_exit0_traceback():
    out = await action_check_boot_liveness(
        _si(
            terminal_output="starting...\nTraceback (most recent call last):\n  ...\nKeyError: 'x'"
        )
    )
    assert out.result["boot_clean"] is False
    vr = out.context_updates["validation_results"]
    assert (
        vr[0]["name"] == "boot_liveness" and vr[0]["required"] and not vr[0]["passed"]
    )


@pytest.mark.asyncio
async def test_boot_liveness_clean_and_empty_are_safe():
    clean = await action_check_boot_liveness(
        _si(terminal_output="app started on :8080")
    )
    assert clean.result["boot_clean"] is True
    assert clean.context_updates == {}
    empty = await action_check_boot_liveness(_si())
    assert empty.result["boot_clean"] is True


def test_quality_gate_wiring_boot_and_probe():
    steps = _compiled()["quality_gate"]["steps"]
    # startup success → boot floor → UX; dirty boot → summarize.
    assert (
        steps["run_startup_check"]["resolver"]["rules"][0]["transition"]
        == "check_boot_liveness"
    )
    bl = {
        r["condition"]: r["transition"]
        for r in steps["check_boot_liveness"]["resolver"]["rules"]
    }
    assert bl["result.boot_clean == true"] == "plan_ux_charter"
    assert bl["true"] == "summarize"
    # profile oracle → asym-probe gate → dep coverage.
    assert steps["profile_oracle"]["resolver"]["rules"][0]["transition"] == "probe_gate"
    pg = {
        r["condition"]: r["transition"]
        for r in steps["probe_gate"]["resolver"]["rules"]
    }
    assert pg["result.run_probe == true"] == "probe_generate"
    assert pg["true"] == "gather_dep_info"
    assert steps["probe_run"]["resolver"]["rules"][0]["transition"] == "gather_dep_info"


# ── C3: asym-probe gate for mission-less callers ──────────────────────


@pytest.mark.asyncio
async def test_detect_solver_falls_back_to_input_objective():
    si = StepInput(
        context={"validation_results": [{"passed": True, "required": True}]},
        inputs={
            "mission_objective": "Implement the function transform(grid) — examples: input [[1]] -> output [[1]]",
            "mode": "completion",
        },
        params={},
        meta=FlowMeta(flow_name="quality_gate", step_id="probe_gate"),
        effects=MockEffects(),
    )
    out = await action_detect_solver_task(si)
    assert out.result["run_probe"] is True
    assert "transform(grid)" in out.context_updates["task_spec"]


@pytest.mark.asyncio
async def test_detect_solver_suppressed_in_checkpoint_mode():
    si = StepInput(
        context={"validation_results": [{"passed": True, "required": True}]},
        inputs={
            "mission_objective": "Implement the function transform(grid) — examples: input [[1]] -> output [[1]]",
            "mode": "checkpoint",
        },
        params={},
        meta=FlowMeta(flow_name="quality_gate", step_id="probe_gate"),
        effects=MockEffects(),
    )
    out = await action_detect_solver_task(si)
    assert out.result["run_probe"] is False


# ── C4: per-goal acceptance checks ────────────────────────────────────


def _functional_goal(**kw) -> GoalRecord:
    return GoalRecord(description="saving works", type="functional", **kw)


@pytest.mark.asyncio
async def test_gate_acceptance_loads_and_flags_derive_need():
    # An ungrounded eligible goal: no stored checks yet, but flagged to
    # derive AFTER a pass (has_checks False, acceptance_needs_derive True).
    goal = _functional_goal()
    fx = MockEffects(mission=_mission([goal]))
    out = await action_gate_goal_acceptance(_si(fx, inputs={"goal_id": goal.id}))
    assert out.result["needs_derive"] is True
    assert out.result["has_checks"] is False
    assert out.context_updates["acceptance_needs_derive"] is True
    # Once grounded with a stored check: has_checks True, no more derive.
    goal.acceptance_grounded = True
    goal.acceptance_checks = [
        {"command": "test -s save.json", "name": "s", "required": True}
    ]
    out2 = await action_gate_goal_acceptance(_si(fx, inputs={"goal_id": goal.id}))
    assert out2.result["needs_derive"] is False
    assert out2.result["has_checks"] is True
    assert out2.context_updates["acceptance_needs_derive"] is False
    assert out2.context_updates["goal_acceptance_checks"] == goal.acceptance_checks


@pytest.mark.asyncio
async def test_gate_acceptance_skips_structural_and_missing_goal():
    goal = GoalRecord(description="module", type="structural")
    fx = MockEffects(mission=_mission([goal]))
    out = await action_gate_goal_acceptance(_si(fx, inputs={"goal_id": goal.id}))
    assert out.result["needs_derive"] is False
    assert out.result["has_checks"] is False
    assert out.context_updates["acceptance_needs_derive"] is False
    out2 = await action_gate_goal_acceptance(_si(fx, inputs={"goal_id": "nope"}))
    assert out2.result["needs_derive"] is False
    assert out2.result["has_checks"] is False


@pytest.mark.asyncio
async def test_store_acceptance_merges_tighten_only_and_one_shots():
    goal = _functional_goal()
    goal.acceptance_checks = [{"command": "test -f a", "name": "a", "required": True}]
    m = _mission([goal])
    resp = (
        '```json\n{"checks": ['
        '{"command": "test -s save.json", "description": "save exists"}, '
        '{"command": "test -f a", "description": "dup"}]}\n```'
    )
    # All probes pass (validate-on-create runs each candidate as /bin/sh -c …;
    # the command[0] "/bin/sh" fallback makes every wrapped check exit 0).
    out = await action_store_goal_acceptance(
        _si(
            MockEffects(commands={"/bin/sh": CommandResult(0, "", "", "/bin/sh")}),
            inputs={"goal_id": goal.id},
            mission=m,
            inference_response=resp,
        )
    )
    cmds = [c["command"] for c in goal.acceptance_checks]
    assert cmds == ["test -f a", "test -s save.json"]  # prior kept, dup dropped
    assert goal.acceptance_grounded is True
    assert out.result["criteria_count"] == 2
    # One-shot even on an empty parse (optional tightener, unlike ops' DoD).
    g2 = _functional_goal()
    await action_store_goal_acceptance(
        _si(
            MockEffects(),
            inputs={"goal_id": g2.id},
            mission=_mission([g2]),
            inference_response="junk",
        )
    )
    assert g2.acceptance_grounded is True and g2.acceptance_checks == []


@pytest.mark.asyncio
async def test_store_acceptance_validates_and_drops_broken():
    # Validate-on-create: the goal just passed, so a correct check exits 0
    # against the current state. A candidate that fails the probe now — a
    # broken command or a mis-grounded assertion — is dropped, not stored.
    goal = _functional_goal()
    m = _mission([goal])
    resp = (
        '```json\n{"checks": ['
        '{"command": "test -s good.json", "description": "produced file"}, '
        '{"command": "test -s bad.json", "description": "mis-grounded"}]}\n```'
    )
    fx = MockEffects(
        commands={
            "/bin/sh -c test -s good.json": CommandResult(0, "", "", "good"),
            "/bin/sh -c test -s bad.json": CommandResult(1, "", "", "bad"),
        }
    )
    out = await action_store_goal_acceptance(
        _si(fx, inputs={"goal_id": goal.id}, mission=m, inference_response=resp)
    )
    cmds = [c["command"] for c in goal.acceptance_checks]
    assert cmds == ["test -s good.json"]  # only the passing check armed
    assert goal.acceptance_grounded is True
    assert out.result["criteria_count"] == 1


@pytest.mark.asyncio
async def test_store_acceptance_all_broken_empty_but_grounded():
    # Every candidate fails the probe (unconfigured MockEffects → rc 127):
    # nothing is armed, but the goal is still grounded one-shot so the
    # evaluator judges alone thereafter (never re-derives, never vacuous).
    goal = _functional_goal()
    resp = (
        '```json\n{"checks": ['
        '{"command": "python -c \\"broken(\\"", "description": "syntaxerror"}, '
        '{"command": "test -s nope.json", "description": "absent"}]}\n```'
    )
    out = await action_store_goal_acceptance(
        _si(
            MockEffects(),
            inputs={"goal_id": goal.id},
            mission=_mission([goal]),
            inference_response=resp,
        )
    )
    assert goal.acceptance_checks == []
    assert goal.acceptance_grounded is True
    assert out.result["criteria_count"] == 0


@pytest.mark.asyncio
async def test_acceptance_verdict_never_vacuous_and_vetoes():
    # Zero checks: acceptance_ok True but summary EMPTY (no vacuous evidence).
    none = await action_apply_acceptance_verdict(_si())
    assert none.result["acceptance_ok"] is True
    assert none.context_updates["acceptance_summary"] == ""
    # A required failure → veto.
    fail = await action_apply_acceptance_verdict(
        _si(validation_results=[{"name": "s", "passed": False, "required": True}])
    )
    assert fail.result["acceptance_ok"] is False
    assert fail.context_updates["acceptance_summary"]


def test_interact_wiring_acceptance_rung():
    # Post-2026-07-18 restructure: derivation is a REGRESSION GUARD armed on
    # the SUCCESS branch after a genuine pass — never pre-pass. On entry the
    # step only LOADS stored checks; derive/store live after
    # end_eval_session_success.
    steps = _compiled()["interact"]["steps"]
    assert "gate_acceptance" not in steps  # renamed → load_stored_checks
    assert (
        steps["run_session"]["resolver"]["rules"][0]["transition"]
        == "load_stored_checks"
    )
    ls = {
        r["condition"]: r["transition"]
        for r in steps["load_stored_checks"]["resolver"]["rules"]
    }
    assert ls["result.has_checks == true"] == "run_acceptance_checks"
    assert ls["true"] == "evaluate_outcome"  # no stored checks → skip to eval
    assert steps["run_acceptance_checks"]["action"] == "run_validation_checks"
    assert (
        steps["acceptance_verdict"]["resolver"]["rules"][0]["transition"]
        == "evaluate_outcome"
    )
    # The deterministic veto: goal_met AND acceptance_ok.
    pe = steps["parse_evaluation"]["resolver"]["rules"]
    cond = pe[0]["condition"]
    assert "acceptance_ok" in cond and "goal_met" in cond
    assert pe[0]["transition"] == "end_eval_session_success"
    # Regression backstop middle rule: behavior passed (goal_met) but a required
    # acceptance check failed → reconcile (disarm-on-refute), NOT outright fail.
    assert pe[1]["transition"] == "reconcile_acceptance"
    assert "goal_met" in pe[1]["condition"] and "== false" in pe[1]["condition"]
    assert pe[2]["transition"] == "end_eval_session_failure"
    # Success branch arms the check only when the goal wasn't yet grounded.
    assert (
        steps["end_eval_session_success"]["resolver"]["rules"][0]["transition"]
        == "arm_acceptance"
    )
    aa = {
        r["condition"]: r["transition"]
        for r in steps["arm_acceptance"]["resolver"]["rules"]
    }
    assert aa["context.get('acceptance_needs_derive') == true"] == "derive_acceptance"
    assert aa["true"] == "flush_transient_success"
    da = {
        r["condition"]: r["transition"]
        for r in steps["derive_acceptance"]["resolver"]["rules"]
    }
    assert da["result.tokens_generated > 0"] == "store_acceptance"
    assert da["true"] == "flush_transient_success"
    assert (
        steps["store_acceptance"]["resolver"]["rules"][0]["transition"]
        == "flush_transient_success"
    )


# ── C5: stuck-goal escalation (2026-08-07: full escalate flow replaced
# the deep_search web hop; boss consult forced on the 3rd escalation) ──


def _stuck_goal(n_attempts=2) -> GoalRecord:
    g = _functional_goal()
    for i in range(n_attempts):
        g.failed_attempts.append(
            FailedAttempt(
                target_file="a.py",
                flow="file_ops",
                reason=f"r{i}",
                diagnosis_summary="d",
            )
        )
    return g


@pytest.mark.asyncio
async def test_goal_escalation_gate_fires_and_repeats_every_two_attempts():
    goal = _stuck_goal(2)
    fx = MockEffects(mission=_mission([goal]))
    out = await action_goal_search_gate(_si(fx, inputs={"goal_id": goal.id}))
    assert out.result["should_search"] is True
    assert goal.description[:40] in out.context_updates["search_brief"]
    assert out.context_updates["force_consult"] is False
    assert goal.escalation_count == 1
    # Not one-shot anymore: re-fires only after 2 MORE failed attempts.
    out2 = await action_goal_search_gate(_si(fx, inputs={"goal_id": goal.id}))
    assert out2.result["should_search"] is False
    goal.failed_attempts.extend(_stuck_goal(2).failed_attempts)
    out3 = await action_goal_search_gate(_si(fx, inputs={"goal_id": goal.id}))
    assert out3.result["should_search"] is True
    assert goal.escalation_count == 2


@pytest.mark.asyncio
async def test_third_escalation_forces_the_boss_consult():
    """Operator (2026-08-07): 'make sure the boss is consulted on the 3rd
    escalation' — two self-recovery loops without resolution mean the agent
    needs direction, not more tooling."""
    goal = _stuck_goal(2)
    fx = MockEffects(mission=_mission([goal]))
    for expected_force in (False, False, True):
        out = await action_goal_search_gate(_si(fx, inputs={"goal_id": goal.id}))
        assert out.result["should_search"] is True
        assert out.context_updates["force_consult"] is expected_force
        goal.failed_attempts.extend(_stuck_goal(2).failed_attempts)
    assert goal.escalation_count == 3


@pytest.mark.asyncio
async def test_goal_search_gate_skips_fresh_goal():
    goal = _stuck_goal(1)
    fx = MockEffects(mission=_mission([goal]))
    out = await action_goal_search_gate(_si(fx, inputs={"goal_id": goal.id}))
    assert out.result["should_search"] is False


@pytest.mark.asyncio
async def test_hermetic_runs_still_escalate_without_web():
    """Escalation is repo-local, so hermetic runs (web_research=False) DO
    escalate — the escalate seed announces web_search is unavailable and
    deep_search self-gates (the contamination guard lives there). The old
    gate skipped entirely because its only tool WAS the web."""
    goal = _stuck_goal(2)
    m = _mission([goal])
    m.config.web_research = False
    fx = MockEffects(mission=m)
    out = await action_goal_search_gate(_si(fx, inputs={"goal_id": goal.id}))
    assert out.result["should_search"] is True


@pytest.mark.asyncio
async def test_store_goal_search_findings_and_sentinel():
    goal = _stuck_goal(2)
    m = _mission([goal])
    out = await action_store_goal_search_findings(
        _si(
            MockEffects(),
            inputs={"goal_id": goal.id},
            mission=m,
            research_summary="use pty not pipes for interactive apps (source: docs.python.org/pty)",
        )
    )
    assert out.result["stored"] is True
    assert "pty not pipes" in goal.search_findings
    # escalation_summary is the primary source now (research_summary legacy).
    g2 = _stuck_goal(2)
    await action_store_goal_search_findings(
        _si(
            MockEffects(),
            inputs={"goal_id": g2.id},
            mission=_mission([g2]),
            escalation_summary="boss: the demo script is the blocker",
        )
    )
    assert "demo script" in g2.search_findings
    g3 = _stuck_goal(2)
    await action_store_goal_search_findings(
        _si(
            MockEffects(),
            inputs={"goal_id": g3.id},
            mission=_mission([g3]),
            research_summary="",
        )
    )
    assert g3.search_findings.startswith("(escalation produced no summary)")


def test_diagnose_wiring_search_arm():
    flow = _compiled()["diagnose_issue"]
    assert flow["entry"] == "search_gate"
    steps = flow["steps"]
    sg = {
        r["condition"]: r["transition"]
        for r in steps["search_gate"]["resolver"]["rules"]
    }
    assert sg["result.should_search == true"] == "do_escalate"
    assert sg["true"] == "start_session"
    # The stuck-goal arm runs the FULL escalate flow (operator, 2026-08-07).
    ds = steps["do_escalate"]
    assert ds["flow"] == "escalate"
    assert ds["input_map"]["failure_evidence"] == {"$ref": "context.search_brief"}
    assert ds["input_map"]["force_consult"]["$ref"] == "context.force_consult"
    assert ds["resolver"]["rules"][0]["transition"] == "store_search_findings"
    assert (
        steps["store_search_findings"]["resolver"]["rules"][0]["transition"]
        == "start_session"
    )


@pytest.mark.asyncio
async def test_diagnose_seed_surfaces_ledger_and_findings():
    from agent.actions.diagnosis_session_actions import action_start_diagnosis_session

    goal = _stuck_goal(2)
    goal.search_findings = "- http://x\n  use pty not pipes for interactive apps"
    m = _mission([goal])
    m.add_ledger_entry(
        cycle=0, kind="provision", description="pip install rich", status="success"
    )
    fx = MockEffects(mission=m)
    out = await action_start_diagnosis_session(
        _si(
            fx,
            inputs={"mission_id": m.id, "goal_id": goal.id},
            goal_description="saving works",
        )
    )
    assert out.result["session_started"] is True
    # The seed is queued as a session injection in context_updates.
    injected = "\n".join(
        str(v) for v in out.context_updates.values() if isinstance(v, (str, list))
    )
    assert "ALREADY DONE THIS MISSION" in injected
    assert "pty not pipes" in injected


def test_escalate_forced_consult_entry_wiring():
    """Compiled-graph pin: force_consult routes start_session straight to
    the boss consult before any tool action (3rd-escalation contract)."""
    steps = _compiled()["escalate"]["steps"]
    rules = {
        r["condition"]: r["transition"]
        for r in steps["start_session"]["resolver"]["rules"]
    }
    assert (
        rules["result.session_started == true and result.force_consult == true"]
        == "do_consult"
    )
    assert rules["result.session_started == true"] == "work"
    assert "escalation_choice_arg" in steps["start_session"]["publishes"]
    assert (
        steps["start_session"]["params"]["force_consult"]["$ref"]
        == "input.force_consult"
    )


def test_goal_escalation_fields_roundtrip():
    g = _functional_goal()
    g.escalation_count = 3
    g.last_escalation_attempts = 6
    g2 = GoalRecord.model_validate(g.model_dump())
    assert g2.escalation_count == 3
    assert g2.last_escalation_attempts == 6
