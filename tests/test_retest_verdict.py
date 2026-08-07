"""The retest verdict — diagnosis vocabulary for "the test is the problem".

Found live on the hy3 run (the Stone Guard case). The tester couldn't work
out HOW to defeat the guard (3 rooms away, 25 HP at code-side attack_power
10 — arithmetic the charter author can never do, since it sees data files
but not code). Diagnoses r13/r16/r19 correctly concluded the kill logic was
fine, but the only vocabulary was file_ops/project_ops, so the remedy got
distorted into file edits: r13/r16 prescribed nerfing the guard's health in
world.json, r19 invented an equip command. Nerfing the guardian has no
guarantee of being restored — nothing catches it until quality_gate, and a
test that passes against modified behavior proves nothing.

Now diagnosis can say `recommended_flow: "retest"` with `test_guidance` —
concrete charter steps computed from code + data + transcript, the three
things only the diagnostician holds at once. The sweep dispatches a guided
re-test instead of a fix; the guidance rides the goal and is appended to
every future retest directive. Guardrails against over-blaming the test
(the operator's explicit worry): retest without guidance demotes, guidance
without retest is zeroed, and _RETEST_MAX caps the loop before it becomes
the acceptance-veto shape again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.diagnosis_session_actions import action_conclude_diagnosis
from agent.actions.diagnostic_actions import action_compile_diagnosis
from agent.actions.mission_actions import (
    _functional_retest_directive,
    action_functional_sweep_next,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    DirectiveReport,
    GoalRecord,
    MissionConfig,
    MissionState,
)

ROOT = Path(__file__).resolve().parents[1]

GUIDANCE = (
    "1. go east  2. go east  3. go north  "
    "4. attack guard — repeat 3 times (25 HP at 10 damage)  "
    "5. check status to confirm the defeat is recorded"
)


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        goals=goals if goals is not None else [],
        config=MissionConfig(working_directory="/tmp/x"),
    )


def _conclude_input(effects: MockEffects, goal_id: str = "", **context) -> StepInput:
    return StepInput(
        context=dict(context),
        inputs={"goal_id": goal_id} if goal_id else {},
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="conclude", attempt=1),
        effects=effects,
    )


def _retest_json(guidance: str = GUIDANCE) -> str:
    return (
        "```json\n"
        + json.dumps(
            {
                "target_file": "engine.py",
                "target_symbol": "GameEngine._do_attack",
                "root_cause": "kill path is correct; session never landed 3 strikes",
                "change_spec": "",
                "kind": "fix",
                "confidence": "HIGH",
                "recommended_flow": "retest",
                "test_guidance": guidance,
            }
        )
        + "\n```"
    )


# ── Conclude layer ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conclude_publishes_retest_with_guidance():
    effects = MockEffects(inference_responses=[_retest_json()])
    out = await action_conclude_diagnosis(
        _conclude_input(effects, diagnosis_session_id="s", investigation_turn=1)
    )
    cu = out.context_updates
    assert cu["recommended_flow"] == "retest"
    assert cu["test_guidance"] == GUIDANCE


@pytest.mark.asyncio
async def test_retest_without_guidance_is_demoted():
    """A retest verdict with no steps is an unactionable promise — the sweep
    would dispatch a session no better charted than the one that failed."""
    effects = MockEffects(inference_responses=[_retest_json(guidance="")])
    out = await action_conclude_diagnosis(
        _conclude_input(effects, diagnosis_session_id="s", investigation_turn=1)
    )
    cu = out.context_updates
    assert cu.get("recommended_flow") != "retest"
    assert cu["test_guidance"] == ""


@pytest.mark.asyncio
async def test_guidance_on_a_fix_verdict_is_zeroed():
    """Guidance riding a fix verdict would leak a stale charter override onto
    the goal via the refresh-on-every-conclude persist."""
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "target_file": "engine.py",
                "target_symbol": "GameEngine._do_attack",
                "kind": "fix",
                "recommended_flow": "file_ops",
                "test_guidance": "go east and attack",
            }
        )
        + "\n```"
    )
    effects = MockEffects(inference_responses=[fenced])
    out = await action_conclude_diagnosis(
        _conclude_input(effects, diagnosis_session_id="s", investigation_turn=1)
    )
    assert out.context_updates["recommended_flow"] == "file_ops"
    assert out.context_updates["test_guidance"] == ""


@pytest.mark.asyncio
async def test_conclude_persists_guidance_onto_goal():
    goal = GoalRecord(description="defeat monsters marked", type="functional")
    mission = _mission([goal])
    effects = MockEffects(inference_responses=[_retest_json()], mission=mission)
    await action_conclude_diagnosis(
        _conclude_input(
            effects, goal.id, diagnosis_session_id="s", investigation_turn=1
        )
    )
    assert (await effects.load_mission()).goals[0].test_guidance == GUIDANCE


@pytest.mark.asyncio
async def test_a_later_fix_verdict_clears_stale_goal_guidance():
    """Refresh-on-every-conclude: the guidance described a session, not the
    goal — a re-diagnose that finds a real defect must clear it."""
    goal = GoalRecord(
        description="defeat monsters marked",
        type="functional",
        test_guidance="stale steps",
    )
    mission = _mission([goal])
    fenced = (
        '```json\n{"target_file": "x.py", "target_symbol": "f", "kind": "fix",'
        ' "recommended_flow": "file_ops"}\n```'
    )
    effects = MockEffects(inference_responses=[fenced], mission=mission)
    await action_conclude_diagnosis(
        _conclude_input(
            effects, goal.id, diagnosis_session_id="s", investigation_turn=1
        )
    )
    assert (await effects.load_mission()).goals[0].test_guidance == ""


# ── compile_diagnosis layer ───────────────────────────────────────────────


def _compile_input(**context) -> StepInput:
    return StepInput(
        context=dict(context),
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="compile_diagnosis"),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_compile_carries_retest_through_the_whitelist():
    """The compile step has its own recommended_flow coercion — before this
    round it squashed anything unknown to file_ops."""
    out = await action_compile_diagnosis(
        _compile_input(
            hypotheses="h",
            recommended_flow="retest",
            test_guidance=GUIDANCE,
            target_file="engine.py",
        )
    )
    diagnosis = out.context_updates["diagnosis"]
    assert diagnosis["recommended_flow"] == "retest"
    assert diagnosis["test_guidance"] == GUIDANCE


@pytest.mark.asyncio
async def test_compile_demotes_retest_without_guidance():
    out = await action_compile_diagnosis(
        _compile_input(hypotheses="h", recommended_flow="retest", test_guidance="")
    )
    diagnosis = out.context_updates["diagnosis"]
    assert diagnosis["recommended_flow"] == "file_ops"
    assert diagnosis["test_guidance"] == ""


# ── Sweep layer ───────────────────────────────────────────────────────────


def _sweep_goal(retest_count: int = 0, guidance: str = GUIDANCE) -> GoalRecord:
    return GoalRecord(
        description="Defeated monsters are marked defeated",
        type="functional",
        status="incomplete",
        origin="design",
        interaction_mode="exploratory",
        test_guidance=guidance,
        retest_count=retest_count,
        reports=[
            DirectiveReport(
                flow="interact",
                status="failed",
                summary="guard never observed defeated",
            ),
            DirectiveReport(
                flow="diagnose_issue",
                status="success",
                summary="kill path correct; session never landed 3 strikes",
                recommended_flow="retest",
                target_file="engine.py",
                target_symbol="GameEngine._do_attack",
                test_guidance=guidance,
            ),
        ],
    )


def _sweep_input(mission) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="x"),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_retest_verdict_dispatches_a_guided_retest_not_a_fix():
    g = _sweep_goal()
    out = await action_functional_sweep_next(_sweep_input(_mission([g])))
    assert out.result.get("needs_test") is True
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact"
    assert "TEST GUIDANCE" in dc["flow_directive"]
    assert GUIDANCE in dc["flow_directive"]
    assert g.retest_count == 1


@pytest.mark.asyncio
async def test_retest_is_uncapped():
    """The original _RETEST_MAX=2 cap was removed (operator, 2026-08-07):
    on hy3's Boss Nyx goal every post-cap diagnosis correctly certified the
    code and the forced fall-through edited that certified-correct code
    round after round. An honest verdict with guidance is honored every
    time; retest_count keeps counting as telemetry."""
    g = _sweep_goal(retest_count=7)
    out = await action_functional_sweep_next(_sweep_input(_mission([g])))
    assert out.result.get("needs_test") is True
    assert out.context_updates["dispatch_config"]["flow"] == "interact"
    assert g.retest_count == 8


@pytest.mark.asyncio
async def test_retest_without_goal_guidance_falls_through():
    """Belt-and-braces: if the persist never landed (older mission.json),
    an unguided retest dispatch would re-run the exact failed session."""
    g = _sweep_goal(guidance="")
    out = await action_functional_sweep_next(_sweep_input(_mission([g])))
    assert out.result.get("needs_test") is not True
    assert out.context_updates["dispatch_config"]["flow"] == "file_ops"


def test_every_retest_directive_carries_the_goal_guidance():
    """Once the route is known, EVERY future session uses it — including the
    after-fix retest and regression rechecks, which all build their directive
    here."""
    g = _sweep_goal()
    directive = _functional_retest_directive(g, after="fix")
    assert "TEST GUIDANCE" in directive
    assert GUIDANCE in directive
    # quality_gate-origin polarity keeps the guidance too
    g.origin = "quality_gate"
    assert GUIDANCE in _functional_retest_directive(g, after="fix")


def test_directives_without_guidance_are_unchanged():
    g = _sweep_goal(guidance="")
    assert "TEST GUIDANCE" not in _functional_retest_directive(g, after="fix")


# ── Contract pins ─────────────────────────────────────────────────────────


class TestTheFlowGraphCarriesTheVerdict:
    """Pinned off compiled.json — an undeclared context key is silently
    filtered by _build_step_input before the action ever sees it."""

    @pytest.fixture(scope="class")
    def diagnose_flow(self):
        return json.loads((ROOT / "flows" / "compiled.json").read_text())[
            "diagnose_issue"
        ]

    def test_conclude_publishes_test_guidance(self, diagnose_flow):
        assert "test_guidance" in diagnose_flow["steps"]["conclude"]["publishes"]

    def test_compile_declares_test_guidance(self, diagnose_flow):
        opt = diagnose_flow["steps"]["compile_diagnosis"]["context"]["optional"]
        assert "test_guidance" in opt

    def test_goal_record_defaults(self):
        g = GoalRecord(description="d", type="functional")
        assert g.test_guidance == ""
        assert g.retest_count == 0

    def test_directive_report_default(self):
        r = DirectiveReport(flow="diagnose_issue", status="success", summary="s")
        assert r.test_guidance == ""

    def test_conclude_prompt_documents_the_bar(self):
        """The operator's guardrail: careful prompting against over-blaming
        the test. The prompt must state the high bar and forbid
        nerf-to-make-testable edits."""
        text = (ROOT / "prompts" / "diagnose" / "conclude.yaml").read_text()
        assert "`retest`" in text
        assert "test_guidance" in text
        assert "NEVER recommend changing the program to make it easier to test" in text

    def test_charter_prompt_honors_guidance(self):
        text = (ROOT / "prompts" / "interact" / "charter_function.yaml").read_text()
        assert "TEST GUIDANCE" in text
        assert "OVERRIDES the step-count cap" in text
