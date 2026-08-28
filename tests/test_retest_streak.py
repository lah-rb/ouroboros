"""Retest-streak escalation — the operator rule from the tmp_cleaner incident.

Honest retest verdicts never append to failed_attempts, so the stuck-goal
escalation gate was structurally blind to a retest loop: one goal ran 15
rounds of diagnose-certifies-correct → interact-passes → sweep-reopens
without the gate ever firing, and the fleet's only self-heal path was
quarantining legitimate authored tests (≈225 cycles for 15 goals). The
streak counts consecutive honored retests with no intervening fix attempt;
past a quarantine's worth (3), the gate escalates with an environment-
suspicion brief instead of letting diagnosis re-certify the code again.
"""

from __future__ import annotations

import pytest

from agent.actions.diagnosis_session_actions import (
    _RETEST_STREAK_ESCALATE,
    action_goal_search_gate,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    GoalRecord,
    MissionConfig,
    MissionState,
    NoteRecord,
)


def _goal(streak: int = 0) -> GoalRecord:
    g = GoalRecord(
        id="g1",
        description="the drop command works from any room",
        type="functional",
        status="incomplete",
        acceptance_checks=[
            {"command": "python -m pytest -q tests/test_drop.py", "required": True}
        ],
    )
    g.retest_streak = streak
    return g


def _mission(goal: GoalRecord) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=[goal],
    )


def _si(fx, goal_id="g1") -> StepInput:
    return StepInput(
        context={},
        inputs={"goal_id": goal_id},
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="search_gate"),
        effects=fx,
    )


def test_threshold_exceeds_a_quarantine():
    """Operator: 'larger than what a single quarantine requires' — the
    strongest existing wear-out bound is 3 contradictions."""
    from agent.actions.pipeline_actions import _AUTHORED_QUARANTINE_K

    assert _RETEST_STREAK_ESCALATE == _AUTHORED_QUARANTINE_K + 1


@pytest.mark.asyncio
async def test_streak_at_threshold_escalates_with_env_suspicion():
    goal = _goal(streak=_RETEST_STREAK_ESCALATE)
    m = _mission(goal)
    m.notes.append(
        NoteRecord(
            content=(
                "regression: goal 'the drop command works' reopened — an "
                "acceptance check failed after an edit (triggering "
                "goal_id=n/a). check=python -m pytest -q tests/test_drop.py "
                "| rc=1 stderr=No module named pytest"
            ),
            category="failure_analysis",
            tags=["regression", goal.id],
            source_flow="regression_sweep_next",
        )
    )
    fx = MockEffects(mission=m)

    out = await action_goal_search_gate(_si(fx))

    assert out.result["should_search"] is True
    brief = out.context_updates["search_brief"]
    assert "ENVIRONMENT" in brief
    assert "python -m pytest -q tests/test_drop.py" in brief  # the check
    assert "No module named pytest" in brief  # the sweep note's stderr
    assert "environment fix, never a code edit" in brief
    assert goal.escalation_count == 1
    assert goal.retest_streak == 0, "the escalation consumes the signal"


@pytest.mark.asyncio
async def test_streak_below_threshold_does_not_fire():
    goal = _goal(streak=_RETEST_STREAK_ESCALATE - 1)
    fx = MockEffects(mission=_mission(goal))

    out = await action_goal_search_gate(_si(fx))

    assert out.result["should_search"] is False
    assert goal.escalation_count == 0


@pytest.mark.asyncio
async def test_streak_escalations_still_count_toward_boss_consult():
    """The third escalation forces the boss regardless of which trigger
    produced the first two."""
    goal = _goal()
    fx = MockEffects(mission=_mission(goal))
    for expected_force in (False, False, True):
        goal.retest_streak = _RETEST_STREAK_ESCALATE
        out = await action_goal_search_gate(_si(fx))
        assert out.result["should_search"] is True
        assert out.context_updates["force_consult"] is expected_force
    assert goal.escalation_count == 3


# ── increments and resets (the sweep side) ────────────────────────────


def test_streak_survives_completion_but_not_a_passing_check():
    """The incident loop COMPLETES and reopens each round — a reset on
    completion would keep the streak at zero forever. Only a fix attempt or
    a passing check ends it, and interact-success completion touches the
    regression flags but not the streak."""
    import inspect

    from agent.actions import mission_actions as ma

    src = inspect.getsource(ma)
    # completion block clears the three regression flags; assert it does NOT
    # reset the streak there (source-level pin: the block that sets
    # regression_autocompleted = False on interact success has no streak
    # write between status flip and flag clears)
    block = src.split('if report_flow == "interact" and report_status == "success":')[1]
    head = block.split("logger.info")[0]
    assert "retest_streak" not in head

    # and the two check-passed sites DO reset it
    assert (
        src.count("goal.retest_streak = 0") >= 4
    )  # file_ops, project_ops, recert, recomplete


@pytest.mark.asyncio
async def test_veto_route_increments_streak_too():
    """Both retest_count sites carry the streak — the acceptance-veto route
    included, so a veto loop that somehow evades the quarantine still
    accumulates toward the gate."""
    import inspect

    from agent.actions import mission_actions as ma

    src = inspect.getsource(ma)
    # every retest_count increment is paired with a streak increment
    diagnose_route = src.split('if recommended_flow == "retest":')[1][:900]
    assert "retest_streak" in diagnose_route
    veto_route = src.split('if getattr(last_report, "acceptance_vetoed", False):')[1][
        :400
    ]
    assert "retest_streak" in veto_route


# ── landing escalation findings where diagnosis can see them ──────────


@pytest.mark.asyncio
async def test_escalation_summary_lands_on_every_affected_goal():
    """Notes are stripped from diagnose seeds; search_findings is the one
    per-goal field the seed renders. Every affected goal gets the summary,
    so whichever one the sweep dispatches next starts informed."""
    from agent.actions.mission_actions import action_store_env_escalation_findings

    g1, g2 = _goal(), _goal()
    g2.id = "g2"
    m = _mission(g1)
    m.goals.append(g2)
    fx = MockEffects(mission=m)

    out = await action_store_env_escalation_findings(
        StepInput(
            context={
                "mission": m,
                "escalation_summary": "the venv lost pyvenv.cfg; pytest is not importable",
                "env_affected_goal_ids": ["g1", "g2"],
            },
            inputs={},
            params={},
            meta=FlowMeta(flow_name="mission_control", step_id="store_env_escalation"),
            effects=fx,
        )
    )

    assert out.result["stored"] == 2
    assert "pyvenv.cfg" in g1.search_findings and "pyvenv.cfg" in g2.search_findings
    assert g1.search_findings.startswith("[common-cause escalation]")
    assert any(n.source_flow == "regression_sweep_env" for n in m.notes)


@pytest.mark.asyncio
async def test_empty_summary_does_not_clobber_prior_findings():
    from agent.actions.mission_actions import action_store_env_escalation_findings

    g = _goal()
    g.search_findings = "prior escalation context worth keeping"
    m = _mission(g)

    out = await action_store_env_escalation_findings(
        StepInput(
            context={
                "mission": m,
                "escalation_summary": "",
                "env_affected_goal_ids": ["g1"],
            },
            inputs={},
            params={},
            meta=FlowMeta(flow_name="mission_control", step_id="store_env_escalation"),
            effects=MockEffects(mission=m),
        )
    )

    assert out.result["stored"] == 0
    assert g.search_findings == "prior escalation context worth keeping"
