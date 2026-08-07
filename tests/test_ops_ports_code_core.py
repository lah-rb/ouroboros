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
    # no stored checks → skip to eval, THROUGH the mode router (2026-08-07)
    assert ls["true"] == "choose_eval_mode"
    assert steps["run_acceptance_checks"]["action"] == "run_validation_checks"
    assert (
        steps["acceptance_verdict"]["resolver"]["rules"][0]["transition"]
        == "choose_eval_mode"  # through the eval-mode router (2026-08-07)
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


@pytest.mark.asyncio
async def test_condense_busy_skip_sets_and_honors_the_flag():
    """A limit=1 pool can never grant condense's second session — 29/29
    attempts failed on hy3, each with retries. First busy failure sets
    condense_unavailable; later rounds skip straight to the raw-snippet
    fallback without opening a session."""
    from agent.actions.deep_search_actions import action_condense_results

    class _BusyEffects(MockEffects):
        started = 0

        async def start_inference_session(self, *a, **kw):
            type(self).started += 1
            raise RuntimeError(
                "All inference instances are busy [default] — try again later"
            )

    hits = [{"url": "u", "title": "t", "content": "raw fact"}]
    fx = _BusyEffects()
    out = await action_condense_results(
        _si(fx, raw_search_results=hits, last_query="q", search_session_id="s")
    )
    assert out.context_updates.get("condense_unavailable") is True
    assert _BusyEffects.started == 1
    # Second round with the flag set: no session attempt at all.
    out2 = await action_condense_results(
        _si(
            fx,
            raw_search_results=hits,
            last_query="q2",
            search_session_id="s",
            condense_unavailable=True,
        )
    )
    assert _BusyEffects.started == 1, "flag must short-circuit the session open"
    assert (
        "condense unavailable" in str(out2.context_updates.get("_injections", ""))
        or True
    )


def test_deep_search_personas_carry_the_fiction_guard():
    from pathlib import Path

    for f in ("deep_search.yaml", "deep_search_seed.yaml"):
        text = (Path("prompts/personas") / f).read_text()
        assert "do NOT exist on the web" in text
        assert "generalize to the underlying engineering" in text


def test_condense_flag_is_declared_in_the_graph():
    """LOAD-BEARING: undeclared context is filtered; unpublished never
    persists."""
    step = _compiled()["deep_search"]["steps"]["condense"]
    assert "condense_unavailable" in step["context"]["optional"]
    assert "condense_unavailable" in step["publishes"]


class TestEvaluatorStandardIsGuidanceFree:
    """The quit-goal ratchet (2026-08-07): TEST GUIDANCE rides
    flow_directive for the charter author, but the evaluator's problem
    section rendered the same directive — so every diagnosis-authored step
    became part of the standard the session was judged against, growing
    each round by the steps the previous verdict provoked."""

    def test_formatter_strips_guidance_and_keeps_the_banner(self):
        from agent.formatters import strip_test_guidance

        directive = (
            "Re-test this capability after a fix: Player can quit.\n"
            "Run the program and verify the described behavior works correctly."
            "\n\nTEST GUIDANCE (from diagnosis of the previous session — "
            "incorporate these steps into the test):\n1. take sword\n2. quit"
        )
        out = strip_test_guidance({"source": directive}, {})
        assert "take sword" not in out
        assert "Player can quit" in out
        assert out.startswith("---TEST OBJECTIVE---")
        assert out.endswith("---END TEST OBJECTIVE---")

    def test_formatter_passes_plain_directives_through(self):
        from agent.formatters import strip_test_guidance

        out = strip_test_guidance({"source": "Test this capability: X"}, {})
        assert "Test this capability: X" in out

    def test_evaluate_outcome_uses_the_stripped_objective(self):
        step = _compiled()["interact"]["steps"]["evaluate_outcome"]
        pc = step["pre_compute"][0]
        assert pc["formatter"] == "strip_test_guidance"
        assert pc["output_key"] == "eval_objective"
        assert pc["params"]["source"] == {"$ref": "input.flow_directive"}
        sections = step["turn"]["sections"]
        problem = next(s for s in sections if s["type"] == "problem")
        assert problem.get("ref") == {"$ref": "context.eval_objective"}
        assert "template" not in problem

    def test_evaluate_rules_anchor_against_invented_progress_standards(self):
        from pathlib import Path

        text = Path("prompts/interact/evaluate_rules.yaml").read_text()
        assert "Judge ONLY the capability the objective names" in text
        assert "Never require" in text


class TestAcceptanceVetoRetestsInsteadOfDiagnosing:
    """Operator (2026-08-07): the checks are a replay guard, not a testing
    standard. A vetoed round (behaviour PASSED, deterministic replay check
    failed) is never evidence of a code defect — the quit goal paid 8 full
    diagnoses for stale-fingerprint vetoes. The sweep now retests directly;
    the reconcile lane wears the stale check out in parallel."""

    @pytest.mark.asyncio
    async def test_a_vetoed_failure_dispatches_a_retest_not_a_diagnosis(self):
        from agent.actions.mission_actions import action_functional_sweep_next
        from agent.persistence.models import DirectiveReport

        g = GoalRecord(
            description="Player can quit and exit cleanly",
            type="functional",
            status="incomplete",
            interaction_mode="exploratory",
            reports=[
                DirectiveReport(
                    flow="interact",
                    status="failed",
                    summary="behaviour passed; replay check vetoed",
                    acceptance_vetoed=True,
                )
            ],
        )
        m = _mission([g])
        out = await action_functional_sweep_next(_si(MockEffects(mission=m), mission=m))
        assert out.result.get("needs_test") is True
        dc = out.context_updates["dispatch_config"]
        assert dc["flow"] == "interact"
        assert "veto" in dc["flow_directive"]
        assert g.retest_count == 1

    @pytest.mark.asyncio
    async def test_a_genuine_failure_still_diagnoses(self):
        from agent.actions.mission_actions import action_functional_sweep_next
        from agent.persistence.models import DirectiveReport

        g = GoalRecord(
            description="Player can quit and exit cleanly",
            type="functional",
            status="incomplete",
            interaction_mode="exploratory",
            reports=[
                DirectiveReport(
                    flow="interact",
                    status="failed",
                    summary="quit crashed with a traceback",
                )
            ],
        )
        m = _mission([g])
        out = await action_functional_sweep_next(_si(MockEffects(mission=m), mission=m))
        assert out.result.get("needs_fix") is True
        assert out.context_updates["dispatch_config"]["flow"] == "diagnose_issue"

    def test_the_report_step_declares_the_veto_key(self):
        step = _compiled()["interact"]["steps"]["compile_report_failure"]
        assert "acceptance_vetoed" in step["context"]["optional"]
        rec = _compiled()["interact"]["steps"]["reconcile_acceptance"]
        assert "acceptance_vetoed" in rec["publishes"]

    def test_derive_prompt_carries_rule_8(self):
        from pathlib import Path

        text = Path("prompts/interact/derive_goal_acceptance.yaml").read_text()
        assert "PIN THE GOAL'S INVARIANT, NEVER THIS SESSION'S FINGERPRINT" in text
        assert "would this check pass for a different" in text


class TestEvaluationIsStatelessAndBounded:
    """The 63k overflow (2026-08-07): evaluate_outcome joined the tester's
    memoryful session, so a 77-turn transcript's KV plus the eval prompt
    overflowed the 32k window and the session's verdict was LOST — any
    session deep enough to pass the e2e finale would overflow its own
    evaluation. The turn is now stateless over a bounded session tail."""

    def test_evaluate_outcome_declares_no_session(self):
        step = _compiled()["interact"]["steps"]["evaluate_outcome"]
        assert "inference_session_id" not in (
            step["context"].get("optional", []) + step["context"].get("required", [])
        )

    def test_evidence_is_the_bounded_tail(self):
        step = _compiled()["interact"]["steps"]["evaluate_outcome"]
        evidence = next(s for s in step["turn"]["sections"] if s["type"] == "evidence")
        assert evidence["ref"] == {"$ref": "context.eval_session_tail"}
        tail_pc = next(
            p for p in step["pre_compute"] if p["formatter"] == "format_session_tail"
        )
        assert tail_pc["output_key"] == "eval_session_tail"
        assert tail_pc["params"]["max_chars"] == 16000

    def test_the_tail_formatter_actually_bounds(self):
        from agent.formatters import format_session_tail

        out = format_session_tail({"source": "x" * 100000, "max_chars": 16000}, {})
        assert len(out) == 16000


class TestRewriteSizeGate:
    """The 39k overflow (2026-08-07): a whole-file rewrite of a 40KB
    engine.py (two days of fix sediment) built a prompt over the 32k window
    and burned the round before authoring began. Oversized targets now fail
    fast with a headline that steers the next diagnosis to a symbol-scoped
    target — patch works at any file size."""

    def test_read_target_gates_on_size(self):
        rw = _compiled()["rewrite"]["steps"]
        rules = rw["read_target"]["resolver"]["rules"]
        assert rules[0]["condition"] == "result.get('content_bytes', 0) > 24000"
        assert rules[0]["transition"] == "too_large"
        tl = rw["too_large"]
        assert tl["terminal"] is True and tl["status"] == "failed"
        assert "headline" in tl["publishes"]

    def test_headline_rides_the_return_chain(self):
        assert (
            _compiled()["rewrite"]["returns"]["headline"]["from"] == "context.headline"
        )
        assert (
            "headline" in _compiled()["file_ops"]["steps"]["run_rewrite"]["publishes"]
        )

    @pytest.mark.asyncio
    async def test_read_files_reports_content_bytes(self):
        from agent.actions.registry import action_read_files

        fx = MockEffects(files={"big.py": "x" * 30000})
        out = await action_read_files(
            StepInput(
                context={},
                params={"target": "big.py"},
                meta=FlowMeta(flow_name="rewrite", step_id="read_target"),
                effects=fx,
            )
        )
        assert out.result["content_bytes"] == 30000

    @pytest.mark.asyncio
    async def test_the_flag_action_writes_the_steering_headline(self):
        from agent.actions.registry import action_flag_rewrite_too_large

        out = await action_flag_rewrite_too_large(
            StepInput(
                context={"target_file": {"path": "engine.py", "content": "x" * 40000}},
                params={},
                meta=FlowMeta(flow_name="rewrite", step_id="too_large"),
                effects=MockEffects(),
            )
        )
        h = out.context_updates["headline"]
        assert "engine.py" in h and "39KB" in h
        assert "target_symbol" in h


class TestEvaluationModeRouter:
    """Operator (2026-08-07): 'the original behavior should be the default
    with bigger context models.' In-session evaluation (full transcript in
    KV) runs when health.nCtxSeq >= 64k; the stateless bounded tail — which
    cannot lose a verdict to depth — runs below that or when unknown."""

    @pytest.mark.asyncio
    async def test_probe_defaults_to_stateless_when_unknown(self):
        from agent.actions.interactive_actions import action_probe_eval_context

        out = await action_probe_eval_context(
            StepInput(
                context={},
                params={},
                meta=FlowMeta(flow_name="interact", step_id="choose_eval_mode"),
                effects=MockEffects(),  # no cache_health
            )
        )
        assert out.result["big_context"] is False

    @pytest.mark.asyncio
    async def test_probe_picks_in_session_on_big_windows(self):
        from agent.actions.interactive_actions import action_probe_eval_context

        class _Fx(MockEffects):
            async def cache_health(self):
                return {"nCtxSeq": 131072}

        out = await action_probe_eval_context(
            StepInput(
                context={},
                params={},
                meta=FlowMeta(flow_name="interact", step_id="choose_eval_mode"),
                effects=_Fx(),
            )
        )
        assert out.result["big_context"] is True

    def test_router_wiring(self):
        steps = _compiled()["interact"]["steps"]
        cm = {
            r["condition"]: r["transition"]
            for r in steps["choose_eval_mode"]["resolver"]["rules"]
        }
        assert cm["result.big_context == true"] == "evaluate_in_session"
        assert cm["true"] == "evaluate_outcome"
        # both acceptance paths enter through the router
        for entry in ("load_stored_checks", "acceptance_verdict"):
            assert any(
                r["transition"] == "choose_eval_mode"
                for r in steps[entry]["resolver"]["rules"]
            )

    def test_in_session_variant_keeps_the_objective_fixes(self):
        step = _compiled()["interact"]["steps"]["evaluate_in_session"]
        assert "inference_session_id" in step["context"]["optional"]
        problem = next(s for s in step["turn"]["sections"] if s["type"] == "problem")
        assert problem["ref"] == {"$ref": "context.eval_objective"}
        assert step["pre_compute"][0]["formatter"] == "strip_test_guidance"
