"""Greenfield functional-goal derivation — action_derive_project_goals.

The contract pinned here is COVERAGE: the objective's own scope decides how
many functional goals exist, and the framework does not put a thumb on that
scale — not in the prompt, and not by capping what comes back.

Incident (2026-07-25). The derivation prompt asked for "4-7 functional
goals". Across ten greenfield runs on seven model families the counts came
back 8/8/8/8/9/9/9/10/10 — models sat at or above the stated ceiling, and
the band compressed goal counts into a near-constant unrelated to any
objective's real scope. It was the one confirmed place the framework was
dictating mission shape: the architecture prompt was investigated and
CLEARED on all three plausible charges (filenames, granularity, and
downstream reconcile — dev/anchor_probe.py, dev/granularity_probe.py,
dev/gate_normalization_probe.py), which left this inline f-string as the
real anchor.

These tests pin the CLASS, not the wording: any numeric goal-count band
reintroduced into the prompt fails the first test, whatever phrasing carries
it, and any downstream cap fails the second.
"""

from __future__ import annotations

import json
import re

import pytest

from agent.actions.mission_actions import action_derive_project_goals
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)

# "4-7 goals", "produce 5 to 9 functional goals", "3–6 goals" ...
COUNT_BAND = re.compile(
    r"\b\d+\s*(?:-|–|to)\s*\d+\s+(?:functional\s+)?goals\b", re.IGNORECASE
)
# "produce 6 functional goals", "return 5 goals"
FIXED_COUNT = re.compile(
    r"\b(?:produce|return|give|write|list)\s+\d+\s+(?:functional\s+)?goals\b",
    re.IGNORECASE,
)


class _CapturingEffects(MockEffects):
    """MockEffects that keeps the FIRST prompt it is handed.

    Pass 2 (functional goals) is the first inference this action fires; the
    content-brief pass only runs when the architecture declares data_shapes,
    which these missions deliberately do not.
    """

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.first_prompt: str | None = None

    async def run_inference(self, prompt, config_overrides=None):
        if self.first_prompt is None:
            self.first_prompt = prompt
        return await super().run_inference(prompt, config_overrides)


def _mission(objective: str = "Build a thing that does several things.") -> MissionState:
    return MissionState(
        objective=objective,
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        architecture=ArchitectureState(
            run_command="python main.py",
            creation_order=["models.py", "main.py"],
            modules=[
                ModuleSpec(file="models.py", responsibility="data"),
                ModuleSpec(file="main.py", responsibility="entry"),
            ],
        ),
    )


def _si(mission: MissionState, effects) -> StepInput:
    return StepInput(
        context={"mission": mission, "architecture": mission.architecture},
        params={},
        effects=effects,
        meta=FlowMeta(flow_name="design_and_plan", step_id="derive_goals"),
    )


@pytest.mark.asyncio
async def test_derivation_prompt_does_not_dictate_a_goal_count():
    mission = _mission()
    fx = _CapturingEffects(mission=mission)
    await action_derive_project_goals(_si(mission, fx))

    prompt = fx.first_prompt or ""
    assert prompt, "expected the functional-goal derivation to fire an inference"

    band = COUNT_BAND.search(prompt)
    assert band is None, (
        f"derivation prompt reintroduced a goal-count band: {band.group(0)!r}. "
        "The objective's scope decides the count — see this module's docstring."
    )
    fixed = FIXED_COUNT.search(prompt)
    assert fixed is None, (
        f"derivation prompt reintroduced a fixed goal count: {fixed.group(0)!r}."
    )


@pytest.mark.asyncio
async def test_every_derived_goal_is_recorded_however_many_come_back():
    """A wide objective must not be silently trimmed.

    The prompt is only half the contract — a cap applied to the parsed list
    would re-impose a shape ceiling without any prompt text to show for it.
    """
    descriptions = [f"User can perform capability {i}" for i in range(12)]
    mission = _mission()
    fx = _CapturingEffects(
        mission=mission,
        inference_responses=["```json\n" + json.dumps(descriptions) + "\n```"],
    )

    await action_derive_project_goals(_si(mission, fx))

    functional = [g.description for g in mission.goals if g.type == "functional"]
    for desc in descriptions:
        assert desc in functional, f"derived goal dropped: {desc!r}"
