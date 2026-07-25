"""Brownfield directive planner — action_derive_directive_goals.

The replan flow decomposes mission.pending_directive into goals against the
EXISTING architecture. The contract this pins: APPEND-ONLY (never wipe a
complete goal — the design_and_plan derive does, this must not), signature
dedup (re-running a directive is a no-op), structural goals stay
greenfield-simple while capabilities become functional capability_absent
goals, and pending_directive is cleared so the replan phase fires once.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.mission_actions import action_derive_directive_goals
from agent.effects.mock import MockEffects
from tests.conftest import compiled_flows
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(directive="Add a boss room behind a puzzle"):
    return MissionState(
        objective="Build a text adventure",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            modules=[ModuleSpec(file="engine.py"), ModuleSpec(file="world.yaml")],
            creation_order=["engine.py", "world.yaml"],
        ),
        goals=[
            GoalRecord(description="engine.py", type="structural", status="complete"),
            GoalRecord(
                description="player can move", type="functional", status="complete"
            ),
        ],
        pending_directive=directive,
    )


def _si(mission, response, effects=None) -> StepInput:
    return StepInput(
        context={"mission": mission, "inference_response": response},
        params={},
        meta=FlowMeta(flow_name="replan", step_id="derive_directive_goals"),
        effects=effects or MockEffects(),
    )


_RESPONSE = json.dumps(
    {
        "new_files": [
            {
                "file": "boss.py",
                "responsibility": "Boss encounter + combat",
                "defines": ["BossRoom"],
                "imports_from": {"models": ["Room"]},
            }
        ],
        "capabilities": [
            {
                "description": "Player enters the boss room from the guard room",
                "placement": "east exit on guard room in world.yaml; wire BossRoom",
            },
            "Player must solve a puzzle before the boss",
        ],
    }
)


@pytest.mark.asyncio
async def test_appends_without_wiping_existing_goals():
    m = _mission()
    out = await action_derive_directive_goals(_si(m, _RESPONSE))
    assert out.result == {
        "goals_derived": True,
        "structural_count": 1,
        "functional_count": 2,
    }
    # Original 2 complete goals untouched; +3 new = 5.
    assert len(m.goals) == 5
    assert sum(1 for g in m.goals if g.status == "complete") == 2
    # New file appended to the architecture (+ creation_order).
    assert m.architecture.has_file("boss.py")
    assert "boss.py" in m.architecture.creation_order
    assert len(m.architecture.modules) == 3


@pytest.mark.asyncio
async def test_structural_vs_functional_split():
    m = _mission()
    await action_derive_directive_goals(_si(m, _RESPONSE))
    new = [g for g in m.goals if g.origin == "directive"]
    struct = [g for g in new if g.type == "structural"]
    func = [g for g in new if g.type == "functional"]
    assert len(struct) == 1 and len(func) == 2
    # Structural stays greenfield-simple (file-bound, NOT capability_absent).
    assert struct[0].associated_files == ["boss.py"]
    assert struct[0].capability_absent is False
    # Functional goals are absent-capabilities to build, exploratory mode.
    assert all(g.capability_absent is True for g in func)
    assert all(g.interaction_mode == "exploratory" for g in func)
    # Placement hint folded into the description.
    assert any("Placement:" in g.description for g in func)


@pytest.mark.asyncio
async def test_clears_pending_directive():
    m = _mission()
    await action_derive_directive_goals(_si(m, _RESPONSE))
    assert m.pending_directive == ""


@pytest.mark.asyncio
async def test_dedup_on_rerun_is_noop():
    m = _mission()
    await action_derive_directive_goals(_si(m, _RESPONSE))
    n_after_first = len(m.goals)
    n_modules = len(m.architecture.modules)
    # Re-set the directive and run the SAME response again.
    m.pending_directive = "Add a boss room behind a puzzle"
    out = await action_derive_directive_goals(_si(m, _RESPONSE))
    assert out.result["structural_count"] == 0
    assert out.result["functional_count"] == 0
    assert len(m.goals) == n_after_first  # nothing re-appended
    assert len(m.architecture.modules) == n_modules  # no duplicate ModuleSpec


@pytest.mark.asyncio
async def test_existing_architecture_file_not_replanned():
    m = _mission()
    resp = json.dumps(
        {
            "new_files": [{"file": "engine.py", "responsibility": "already exists"}],
            "capabilities": [],
        }
    )
    out = await action_derive_directive_goals(_si(m, resp))
    # engine.py is already in the architecture — skipped.
    assert out.result["structural_count"] == 0
    assert len(m.architecture.modules) == 2


@pytest.mark.asyncio
async def test_empty_directive_is_noop():
    m = _mission(directive="")
    out = await action_derive_directive_goals(_si(m, _RESPONSE))
    assert out.result["goals_derived"] is False
    assert len(m.goals) == 2  # unchanged


@pytest.mark.asyncio
async def test_undecomposable_directive_still_clears():
    # 0 goals derived must still clear the directive (else it loops in replan).
    m = _mission()
    out = await action_derive_directive_goals(
        _si(m, json.dumps({"new_files": [], "capabilities": []}))
    )
    assert out.result["goals_derived"] is False
    assert m.pending_directive == ""


def test_compiled_replan_wiring():
    compiled = compiled_flows()
    # mission_control routes the replan phase to dispatch_replan -> replan flow.
    rules = compiled["mission_control"]["steps"]["check_phase"]["resolver"]["rules"]
    transitions = {r["condition"]: r["transition"] for r in rules}
    assert transitions["result.phase == 'replan'"] == "dispatch_replan"
    assert (
        compiled["mission_control"]["steps"]["dispatch_replan"]["tail_call"]["flow"]
        == "replan"
    )
    # The replan flow decomposes then derives goals.
    steps = compiled["replan"]["steps"]
    assert steps["decompose_directive"]["action"] == "inference"
    assert steps["derive_directive_goals"]["action"] == "derive_directive_goals"
