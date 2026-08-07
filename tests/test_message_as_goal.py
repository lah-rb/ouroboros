"""`mission message --as-goal` — live-mission goal injection via the event
pipeline.

Before this round the messaging system could only deliver advisory notes,
and notes are addressed to file-scoped readers — a scaffolding-removal
directive would surface to nobody (the flush-tripwire addressing problem
all over again). The reopen path can append goals but refuses active
missions. So the event pipeline gains a goal-bearing message: the CLI
stamps ``as_goal`` on the ``user_message`` event, and ``handle_events``
creates the goal INSIDE the running process at the cycle boundary — the
only write that survives against the process's in-memory mission state
(the CLI's direct mission.json save loses to the process's next save).
"""

from __future__ import annotations

import asyncio

from agent.actions.mission_actions import action_handle_events
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import Event, MissionConfig, MissionState


def _mission() -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
    )


def _handle(mission, events):
    return asyncio.run(
        action_handle_events(
            StepInput(
                context={"mission": mission, "events": events},
                params={},
                meta=FlowMeta(flow_name="mission_control", step_id="handle_events"),
                effects=MockEffects(mission=mission),
            )
        )
    )


DIRECTIVE = "Game starts interactive-first: remove the scripted demo from main()"


def test_a_goal_message_appends_a_directive_goal_and_a_note():
    m = _mission()
    _handle(
        m,
        [Event(type="user_message", payload={"message": DIRECTIVE, "as_goal": True})],
    )
    assert len(m.goals) == 1
    g = m.goals[0]
    assert g.description == DIRECTIVE
    assert g.origin == "directive"
    assert g.type == "functional"
    assert g.status == "incomplete"
    assert g.interaction_mode == "exploratory"
    assert any(n.content == DIRECTIVE for n in m.notes)


def test_a_plain_message_stays_a_note_only():
    m = _mission()
    _handle(m, [Event(type="user_message", payload={"message": "just fyi"})])
    assert m.goals == []
    assert any(n.content == "just fyi" for n in m.notes)


def test_a_duplicate_goal_message_records_only_the_note():
    m = _mission()
    ev = Event(type="user_message", payload={"message": DIRECTIVE, "as_goal": True})
    _handle(m, [ev])
    _handle(
        m,
        [Event(type="user_message", payload={"message": DIRECTIVE, "as_goal": True})],
    )
    assert len(m.goals) == 1, "re-sending the directive must not fork the goal"


def test_goal_messages_coexist_with_pause_handling():
    """The goal append must not disturb the existing abort/pause contract."""
    m = _mission()
    out = _handle(
        m,
        [
            Event(type="pause", payload={}),
            Event(
                type="user_message",
                payload={"message": DIRECTIVE, "as_goal": True},
            ),
        ],
    )
    assert out.result["pause_requested"] is True
    assert m.status == "paused"
    assert len(m.goals) == 1
