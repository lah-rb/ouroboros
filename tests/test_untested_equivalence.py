"""A coverage finding must not become a goal for work already verified.

THE FALL-THROUGH. `_quality_finding_signature` anchors on code identifiers
when the text names any, and falls back to normalized prose when it does not.
A coverage finding names no code, so it always keys on prose; a design goal
names its module, so it always keys on anchors. Measured on the live mission:

    untested: the drop command was not exercised…  ->  'the drop command was
                                                        not exercised by the
                                                        ux session'
    design goal covering drop                      ->  'engine.py'

Two key spaces. They can never collide, so a completed goal has never been
able to suppress one of these, however the prose is tuned. Only a semantic
comparison bridges it — one question, thinking off, one word back.
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import (
    _quality_finding_signature,
    _untested_already_covered,
)
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


class _Effects:
    """Answers with a scripted verdict; records what it was asked."""

    def __init__(self, answer: str = "COVERED", boom: bool = False):
        self.answer, self.boom, self.prompts, self.configs = answer, boom, [], []

    async def run_inference(self, prompt, config_overrides=None, **kw):
        if self.boom:
            raise RuntimeError("server gone")
        self.prompts.append(prompt)
        self.configs.append(config_overrides or {})
        return type("R", (), {"text": self.answer})()


def _mission(goals) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=goals,
    )


def _verified(desc: str) -> GoalRecord:
    return GoalRecord(
        description=desc, type="functional", status="complete", origin="design"
    )


def test_the_signatures_genuinely_cannot_collide():
    """The premise of the whole change — pinned so a future signature tweak
    that DOES bridge them shows up here rather than silently duplicating."""
    coverage = _quality_finding_signature(
        {
            "description": "untested: the drop command was not exercised by the UX session"
        }
    )
    design = _quality_finding_signature(
        {"description": "engine.py — turns raw player input into a Command(verb, arg)"}
    )
    assert coverage != design
    assert "engine" not in coverage


@pytest.mark.asyncio
async def test_a_covered_behaviour_is_suppressed():
    fx = _Effects("COVERED")
    m = _mission([_verified("The player can drop an item they are carrying")])

    covered = await _untested_already_covered(
        fx, m, "untested: the drop command was not exercised by the UX session"
    )

    assert covered is True
    # the marker is framing, not part of the question
    assert "untested:" not in fx.prompts[0]
    assert "drop command was not exercised" in fx.prompts[0]
    assert "The player can drop an item" in fx.prompts[0]


@pytest.mark.asyncio
async def test_thinking_is_off_for_the_comparison():
    """It is a comparison, not a deliberation, and it runs once per candidate
    inside a gate that is already expensive."""
    fx = _Effects("COVERED")
    m = _mission([_verified("player can drop items")])

    await _untested_already_covered(fx, m, "untested: drop was not exercised")

    assert fx.configs[0].get("reasoning") == "none"


@pytest.mark.asyncio
async def test_a_genuinely_new_gap_still_files():
    fx = _Effects("NEW")
    m = _mission([_verified("The player can move between rooms")])

    covered = await _untested_already_covered(
        fx, m, "untested: saving and loading was not exercised"
    )

    assert covered is False


@pytest.mark.asyncio
async def test_it_fails_open_when_inference_dies():
    """A real gap dropped is invisible; a duplicate that survives is merely
    expensive. So an error keeps today's behaviour — file the goal."""
    fx = _Effects(boom=True)
    m = _mission([_verified("player can drop items")])

    assert await _untested_already_covered(fx, m, "untested: drop") is False


@pytest.mark.asyncio
async def test_an_ambiguous_answer_files_the_goal():
    fx = _Effects("Possibly COVERED, though it could be NEW")
    m = _mission([_verified("player can drop items")])

    assert await _untested_already_covered(fx, m, "untested: drop") is False


@pytest.mark.asyncio
async def test_nothing_verified_yet_means_nothing_to_compare():
    """No completed goals: skip the call entirely rather than ask the model to
    compare against an empty list."""
    fx = _Effects("COVERED")
    m = _mission(
        [GoalRecord(description="wip", type="functional", status="incomplete")]
    )

    assert await _untested_already_covered(fx, m, "untested: drop") is False
    assert fx.prompts == []


@pytest.mark.asyncio
async def test_incomplete_goals_do_not_count_as_coverage():
    """A goal that has not completed has established nothing."""
    fx = _Effects("COVERED")
    m = _mission(
        [
            GoalRecord(
                description="player can drop items",
                type="functional",
                status="incomplete",
            )
        ]
    )

    assert await _untested_already_covered(fx, m, "untested: drop") is False
    assert fx.prompts == []
