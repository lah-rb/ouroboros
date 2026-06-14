"""Brownfield replan, end-to-end through the actions (no flow engine, no LLM).

Drives the full handoff a reopened-with-directive mission takes:
  pending_directive -> phase 'replan' -> directive planner appends goals +
  clears the directive -> phase 'structural' (new file) -> complete it ->
  phase 'functional' -> the capability_absent goal dispatches an
  explore-and-build interact session.
Pins that the three milestones compose.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.mission_actions import (
    action_check_pipeline_phase,
    action_derive_directive_goals,
    action_functional_sweep_next,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    GoalRecord,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _si(mission, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="x"),
        effects=MockEffects(),
    )


async def _phase(mission) -> str:
    out = await action_check_pipeline_phase(_si(mission))
    return out.result["phase"]


@pytest.mark.asyncio
async def test_replan_handoff_phase_to_planner_to_sweep():
    # A completed mission, reopened with a directive.
    m = MissionState(
        objective="Build a text adventure",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        environment_verified=True,
        architecture=ArchitectureState(
            modules=[ModuleSpec(file="engine.py")], creation_order=["engine.py"]
        ),
        goals=[
            GoalRecord(description="engine.py", type="structural", status="complete")
        ],
        pending_directive="Add a boss room behind a puzzle",
    )

    # 1) Pending directive intercepts -> replan.
    assert await _phase(m) == "replan"

    # 2) The planner decomposes (canned inference) -> appends goals, clears directive.
    response = json.dumps(
        {
            "new_files": [{"file": "boss.py", "responsibility": "Boss room + combat"}],
            "capabilities": [
                {
                    "description": "Enter the boss room from the guard room",
                    "placement": "east exit on guard room; wire BossRoom",
                }
            ],
        }
    )
    await action_derive_directive_goals(_si(m, inference_response=response))
    assert m.pending_directive == ""  # consumed -> replan won't re-fire
    assert m.architecture.has_file("boss.py")

    # 3) New structural goal (boss.py) is incomplete -> structural phase.
    assert await _phase(m) == "structural"

    # 4) Complete the structural goal -> functional phase (the capability).
    for g in m.goals:
        if g.type == "structural" and g.description == "Boss room + combat":
            g.status = "complete"
    assert await _phase(m) == "functional"

    # 5) The capability_absent goal dispatches an explore-and-build session.
    out = await action_functional_sweep_next(_si(m))
    dc = out.context_updates["dispatch_config"]
    assert dc["flow"] == "interact" and dc["charter_mode"] == "explore"
    assert "does not exist yet" in dc["flow_directive"]
