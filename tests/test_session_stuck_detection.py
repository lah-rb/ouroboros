"""Stuck detection must mean NO PROGRESS, not merely repeated input.

The hy3 post-prologue collapse (2026-08-07). Once the self-playing demo was
removed and testers walked the map themselves, every session died at "step
2 of 10": the route legitimately requires "go north" several times in a
row, and the old bare duplicate-input check closed the session as stuck on
the SECOND consecutive move. Thirty-plus guided retests failed identically,
immune to every prompt-side fix, because the persona never chose to stop —
the detector did.

Stuck requires the same input to have already run _STUCK_IDENTICAL_RUNS
(operator-set: 4) times with byte-identical outputs — the FIFTH identical
send trips it ("at that point it really looks like circling, not
productive terminal time"). Repeated input whose output changes is
navigation, not a loop.
"""

from __future__ import annotations

import asyncio

from agent.actions.interactive_actions import action_send_interaction
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _run(history, text):
    return asyncio.run(
        action_send_interaction(
            StepInput(
                context={
                    "mcp_connection_id": "c1",
                    "mcp_session_id": "s1",
                    "session_history": history,
                    "planned_action": "send_input",
                    "planned_action_arg": text,
                },
                params={},
                meta=FlowMeta(flow_name="run_session", step_id="execute_interaction"),
                effects=MockEffects(),
            )
        )
    )


def _hx(*pairs):
    return [{"input": i, "output": o} for i, o in pairs]


def test_consecutive_moves_with_changing_output_are_not_stuck():
    """The exact hy3 route: the second consecutive 'go north' moved to a
    NEW room — it must send, not close."""
    out = _run(
        _hx(("go north", "You move to the Antechamber.")),
        "go north",
    )
    assert out.result.get("stuck_detected") is not True


def test_three_moves_in_a_row_fine_while_rooms_change():
    out = _run(
        _hx(
            ("go north", "You move to the Antechamber."),
            ("go north", "You move to Sage's Nook."),
        ),
        "go north",
    )
    assert out.result.get("stuck_detected") is not True


def test_four_identical_exchanges_still_send():
    """One shy of the bar: 4 identical no-progress runs — the 5th send is
    still allowed through (it becomes the trip only on the NEXT try)."""
    out = _run(
        _hx(*[("go south", "No way to go.")] * 3),
        "go south",
    )
    assert out.result.get("stuck_detected") is not True


def test_true_circling_trips_on_the_fifth_identical_send():
    """Same input already ran 4 times with byte-identical output — the
    fifth send is genuine circling and closes."""
    out = _run(
        _hx(*[("go south", "No way to go.")] * 4),
        "go south",
    )
    assert out.result.get("stuck_detected") is True
    assert out.result.get("command_sent") is False
