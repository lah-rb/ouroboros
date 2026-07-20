"""Phase derivation through the flow-set registry.

action_check_pipeline_phase used to hardcode the code pipeline's phase
order; it now evaluates the declarative spec registered in
agent/flow_sets.py, selected by mission.config.flow_set. These tests pin
that the refactor is behavior-preserving — every phase, ordering rule,
count semantic, and observation string of the original implementation —
plus the new selection edges (missing config attr, unknown set name).
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import action_check_pipeline_phase
from agent.flow_sets import DEFAULT_FLOW_SET, FLOW_SETS, get_flow_set
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(goals=None, *, arch=True, env_verified=False, tests_verified=True):
    config = MissionConfig(working_directory="/tmp/x")
    m = MissionState(
        objective="t",
        status="active",
        goals=goals or [],
        config=config,
        architecture=(
            ArchitectureState(modules=[ModuleSpec(file="m.py")]) if arch else None
        ),
    )
    m.environment_verified = env_verified
    # Default tests_verified=True so the test-suite gate (which sits between
    # functional completion and quality) doesn't intercept the quality-phase
    # assertions; the gate's own routing is pinned in test_test_suite_gate.
    m.tests_verified = tests_verified
    return m


def _goal(gtype, status="incomplete"):
    return GoalRecord(description=f"{gtype} goal", type=gtype, status=status)


def _si(mission) -> StepInput:
    return StepInput(
        context={"mission": mission},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="check_phase"),
    )


async def _phase(mission):
    out = await action_check_pipeline_phase(_si(mission))
    return out.result["phase"], out.observations


# ── plan preconditions (exact observations pinned) ────────────────────


@pytest.mark.asyncio
async def test_no_mission_is_plan():
    assert await _phase(None) == ("plan", "No mission — needs planning")


@pytest.mark.asyncio
async def test_no_architecture_is_plan():
    assert await _phase(_mission(arch=False)) == (
        "plan",
        "No architecture — needs planning",
    )


@pytest.mark.asyncio
async def test_no_goals_is_plan():
    assert await _phase(_mission([])) == ("plan", "No goals — needs planning")


# ── phase order + count semantics ─────────────────────────────────────


@pytest.mark.asyncio
async def test_structural_wins_over_functional_with_type_scoped_counts():
    m = _mission(
        [
            _goal("structural"),
            _goal("structural", "complete"),
            _goal("functional"),
        ]
    )
    # {total} counts goals OF THAT TYPE (2 structural), not all goals (3).
    assert await _phase(m) == ("structural", "Structural phase: 1/2 incomplete")


@pytest.mark.asyncio
async def test_environment_gate_after_structural():
    m = _mission([_goal("structural", "complete"), _goal("functional")])
    assert await _phase(m) == (
        "environment",
        "All structural goals complete — environment needs verification",
    )


@pytest.mark.asyncio
async def test_functional_after_environment_verified():
    m = _mission(
        [_goal("structural", "complete"), _goal("functional")], env_verified=True
    )
    assert await _phase(m) == ("functional", "Functional phase: 1/1 incomplete")


@pytest.mark.asyncio
async def test_quality_fix_for_harvested_quality_goals():
    m = _mission(
        [
            _goal("structural", "complete"),
            _goal("functional", "complete"),
            _goal("quality"),
        ],
        env_verified=True,
    )
    assert await _phase(m) == ("quality_fix", "Quality-fix phase: 1/1 incomplete")


@pytest.mark.asyncio
async def test_all_complete_is_quality_gate():
    m = _mission(
        [_goal("structural", "complete"), _goal("functional", "complete")],
        env_verified=True,
    )
    assert await _phase(m) == (
        "quality",
        "All goals complete — ready for quality gate",
    )


@pytest.mark.asyncio
async def test_test_suite_gate_between_functional_and_quality():
    # Functional complete but tests not yet verified → the test-suite gate
    # fires before the quality gate (Phase B.5).
    m = _mission(
        [_goal("structural", "complete"), _goal("functional", "complete")],
        env_verified=True,
        tests_verified=False,
    )
    assert await _phase(m) == (
        "test_suite",
        "Functional complete — running the repo's test suite",
    )


@pytest.mark.asyncio
async def test_incomplete_functional_precedes_test_gate():
    # A harvested fix goal (incomplete functional) is worked BEFORE the gate
    # re-fires — the functional rule sits ahead of test_suite.
    m = _mission(
        [_goal("structural", "complete"), _goal("functional", "incomplete")],
        env_verified=True,
        tests_verified=False,
    )
    assert (await _phase(m))[0] == "functional"


# ── brownfield replan (pending_directive) ─────────────────────────────


@pytest.mark.asyncio
async def test_pending_directive_routes_to_replan():
    # A reopened-and-complete mission would otherwise go straight to quality;
    # a pending directive must intercept first.
    m = _mission(
        [_goal("structural", "complete"), _goal("functional", "complete")],
        env_verified=True,
    )
    m.pending_directive = "Add a boss room behind a puzzle"
    assert await _phase(m) == (
        "replan",
        "Pending directive — decomposing into goals (brownfield replan)",
    )


@pytest.mark.asyncio
async def test_empty_pending_directive_unaffected():
    # Polarity guard: the common re-gate case (no directive) is unchanged.
    m = _mission(
        [_goal("structural", "complete"), _goal("functional", "complete")],
        env_verified=True,
    )
    assert m.pending_directive == ""
    assert (await _phase(m))[0] == "quality"


@pytest.mark.asyncio
async def test_replan_wins_over_incomplete_structural():
    m = _mission([_goal("structural", "incomplete")])
    m.pending_directive = "extend it"
    assert (await _phase(m))[0] == "replan"  # replan rule is first


def test_replan_rule_is_first_and_keyed_on_pending_directive():
    from agent.flow_sets import CODE_CORE_PHASES

    first = CODE_CORE_PHASES[0]
    assert first.kind == "attr_truthy"
    assert first.phase == "replan"
    assert first.flag == "pending_directive"


# ── flow-set selection edges ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_config_without_flow_set_attr_defaults():
    class _Cfg:  # old persisted missions / test stubs without the field
        pass

    class _Mission:
        config = _Cfg()
        architecture = object()
        goals = [_goal("structural")]
        environment_verified = False

    out = await action_check_pipeline_phase(_si(_Mission()))
    assert out.result["phase"] == "structural"


@pytest.mark.asyncio
async def test_unknown_flow_set_falls_back_to_code_core():
    class _Cfg:
        flow_set = "not-a-real-set"

    class _Mission:
        config = _Cfg()
        architecture = object()
        goals = [_goal("structural")]
        environment_verified = False

    out = await action_check_pipeline_phase(_si(_Mission()))
    assert out.result["phase"] == "structural"


def test_registry_contract():
    assert DEFAULT_FLOW_SET in FLOW_SETS
    assert get_flow_set("code_core").entry_flow == "mission_control"
    # Every spec must end in a terminal rule.
    for spec in FLOW_SETS.values():
        assert spec.phases[-1].kind == "terminal"


def _grounded_complete_goal():
    return GoalRecord(
        description="done",
        type="functional",
        status="complete",
        acceptance_checks=[{"command": "echo ok", "name": "c", "required": True}],
    )


@pytest.mark.asyncio
async def test_regression_pending_phase():
    # Grounded completed goal + an edit since the last sweep -> regression fires.
    g = _grounded_complete_goal()
    m = _mission(goals=[g], env_verified=True)
    m.last_edit_cycle, m.last_regression_cycle = 5, 3
    phase, _ = await _phase(m)
    assert phase == "regression"

    # No edit since the last sweep -> not regression (all complete -> quality).
    m.last_regression_cycle = 5
    phase, _ = await _phase(m)
    assert phase != "regression"

    # Edit since sweep but NO grounded check -> not regression (nothing to protect).
    g.acceptance_checks = []
    m.last_regression_cycle = 3
    phase, _ = await _phase(m)
    assert phase != "regression"


@pytest.mark.asyncio
async def test_regression_preempts_functional_but_not_replan():
    g = _grounded_complete_goal()
    open_fn = _goal("functional", status="incomplete")
    m = _mission(goals=[g, open_fn], env_verified=True)
    m.last_edit_cycle, m.last_regression_cycle = 5, 3
    # An incomplete functional goal exists, but the armed regression preempts it.
    phase, _ = await _phase(m)
    assert phase == "regression"
    # ...yet a pending directive (replan) still preempts regression.
    m.pending_directive = "add feature X"
    phase, _ = await _phase(m)
    assert phase == "replan"
