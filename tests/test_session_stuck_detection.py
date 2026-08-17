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


# ── The two bounds the identical-run check cannot provide ────────────
#
# Live incident 2026-08-16 (muse win-screen charter): a playthrough hit
# turn 97 and kept climbing, oscillating north/south/north/south. The
# identical-run check requires the SAME input 4x consecutively, so an
# A-B-A-B cycle is structurally invisible to it — and the "turn budget
# bounds the rest" the module comment relied on did not exist anywhere in
# the repo. The session dropped its own context twice (56k tokens each),
# losing the first ~44 turns of its own playthrough. An unbounded session
# also makes `tier pause` unlandable, since pause drains at a cycle
# boundary and the session IS the cycle.


def _cycle_hx(n, screens=("Foggy Shore\n> ", "Salt Marsh\n> ")):
    """n exchanges alternating between `screens` — the observed shape."""
    moves = ("north", "south")
    return [
        {"input": moves[i % len(moves)], "output": screens[i % len(screens)]}
        for i in range(n)
    ]


def test_alternating_two_room_orbit_is_caught():
    """north/south/north/south — invisible to the identical-run check."""
    out = _run(_cycle_hx(30), "north")
    assert out.result.get("stuck_detected") is True
    assert "rbiting" in out.observations or "Cycling" in out.observations


def test_walking_back_through_known_rooms_toward_something_new_is_not_a_cycle():
    """The legitimate case the 'all previously seen' clause protects: a
    traversal that reveals a NEW screen must keep going."""
    hx = _cycle_hx(24)
    hx.append({"input": "east", "output": "Sunken Chapel — a NEW room\n> "})
    hx += [
        {"input": "north", "output": "Foggy Shore\n> "},
        {"input": "south", "output": "Salt Marsh\n> "},
    ]
    out = _run(hx, "north")
    assert out.result.get("stuck_detected") is not True


def test_a_long_but_varied_playthrough_is_not_a_cycle():
    """40 turns, every screen distinct — exploration, not orbiting."""
    hx = [{"input": f"go {i}", "output": f"Room {i}\n> "} for i in range(40)]
    out = _run(hx, "go 40")
    assert out.result.get("stuck_detected") is not True


def test_turn_budget_closes_a_session_that_will_never_close_itself():
    from agent.actions.interactive_actions import _SESSION_TURN_BUDGET

    hx = [
        {"input": f"go {i}", "output": f"Room {i}\n> "}
        for i in range(_SESSION_TURN_BUDGET)
    ]
    out = _run(hx, "go on")
    assert out.result.get("stuck_detected") is True
    assert "budget" in out.observations.lower()


def test_turn_budget_leaves_a_normal_length_playthrough_alone():
    """40 turns is a long real session; it must not be truncated."""
    hx = [{"input": f"go {i}", "output": f"Room {i}\n> "} for i in range(40)]
    out = _run(hx, "go 40")
    assert out.result.get("stuck_detected") is not True
