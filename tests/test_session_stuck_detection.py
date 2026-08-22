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


def test_alternating_orbit_below_the_bar_is_NOT_force_closed():
    """History, both directions. The 58eb83c screen detector (removed by
    operator verdict 2026-08-17) closed on `≤`3 distinct screens over a
    12-turn window — loose enough to kill a combat orbit the model might
    walk out of. The 2026-08-21 verdict reinstated cycle detection but at
    the byte-identity bar: every position of the cycle repeated
    _STUCK_IDENTICAL_RUNS times, input AND output exact. This pin keeps
    the 08-17 protection at the boundary: one repetition short of the
    bar must NOT close."""
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hx(2 * _STUCK_IDENTICAL_RUNS - 1)
    out = _run(hx, "north")
    assert out.result.get("stuck_detected") is not True


def test_alternating_orbit_at_the_bar_IS_closed():
    """And at the bar, it closes (operator, 2026-08-21): a byte-identical
    A-B orbit repeated _STUCK_IDENTICAL_RUNS times is circling — the live
    qwen3-next-coder case ran this shape 324 turns."""
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hx(2 * _STUCK_IDENTICAL_RUNS)
    out = _run(hx, "north")
    assert out.result.get("stuck_detected") is True


def test_walking_back_through_known_rooms_toward_something_new_keeps_going():
    hx = _cycle_hx(24)
    hx.append({"input": "east", "output": "Sunken Chapel — a NEW room\n> "})
    hx += [
        {"input": "north", "output": "Foggy Shore\n> "},
        {"input": "south", "output": "Salt Marsh\n> "},
    ]
    out = _run(hx, "north")
    assert out.result.get("stuck_detected") is not True


def test_a_long_but_varied_playthrough_is_not_stuck():
    """40 turns, every screen distinct — exploration, not orbiting."""
    hx = [{"input": f"go {i}", "output": f"Room {i}\n> "} for i in range(40)]
    out = _run(hx, "go 40")
    assert out.result.get("stuck_detected") is not True


def test_no_turn_count_ever_closes_a_session():
    """There is NO session turn budget, deliberately (operator, 2026-08-21).
    A turn count cannot tell an orbit from a large world — the 90-turn cap
    that used to live here truncated a ~92-turn game two moves from its
    ending. The wall is the only honest bound, and it is enforced by the
    tier runner's time-up kill, not from in here."""
    hx = [{"input": f"go {i}", "output": f"Room {i}\n> "} for i in range(400)]
    out = _run(hx, "go on")
    assert out.result.get("stuck_detected") is not True


def test_a_varying_orbit_slips_the_in_session_guards():
    """Documents WHY the bound has to be the wall. A session that varies its
    output each turn is invisible to both in-session guards: the degeneration
    guard sees only inside one generation, and the stuck detector needs
    byte-identical outputs. qwen3-next-coder orbited one session to 335 turns
    this way. Nothing here can catch it — the runner must."""
    hx = [
        {"input": f"look {i}", "output": f"Varying output {i}\n> "} for i in range(300)
    ]
    out = _run(hx, "look again")
    assert out.result.get("stuck_detected") is not True


def test_tier_runner_kills_an_arm_that_never_parks():
    """The wall is handed to the AGENT, which parks itself at a cycle
    boundary — so a wedged cycle outlives it. The runner enforces the wall
    independently after a grace period; without this the 08-21 arm sat 40
    minutes past its wall and had to be abandoned by hand."""
    from pathlib import Path

    from agent.tier.runner import WALL_GRACE_S

    assert 300 <= WALL_GRACE_S <= 1800  # generous enough for an honest cycle
    src = (
        Path(__file__).resolve().parents[1] / "agent" / "tier" / "runner.py"
    ).read_text()
    assert "TIME-UP KILL" in src
    assert "wall_killed" in src  # the record says the wall did not stop it


def test_run_session_gets_a_step_ceiling_above_any_real_session():
    """The turn cap only existed because runtime capped sub-flows at 200
    steps (~95 turns at 2 steps/turn), so a long session died as
    MaxStepsExceeded with no evaluate turn. Removing the cap without lifting
    that ceiling would trade a graceful close for a hard crash five turns
    later."""
    from agent.runtime import _subflow_max_steps

    STEPS_PER_TURN = 2
    assert _subflow_max_steps("run_session") / STEPS_PER_TURN > 500
    assert _subflow_max_steps("diagnose_issue") == 200


# ── short-cycle orbits (2026-08-21, the qwen3-next-coder 324-turn loop) ──


def _cycle_hist(pairs, reps):
    hx = []
    for _ in range(reps):
        for inp, out in pairs:
            hx.append({"input": inp, "output": out})
    return hx


def test_ab_orbit_with_identical_outputs_is_caught():
    """The live case: relaunch/look alternating against a byte-identical
    broken boot. Period-1 is structurally blind to it (never 4 consecutive
    identical entries); the cycle check trips on the send that would
    continue the orbit."""
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hist(
        [
            ("python main.py", "You find yourself in an empty void.\n> "),
            ("look", "You find yourself in an empty void.\n> "),
        ],
        _STUCK_IDENTICAL_RUNS,
    )
    out = _run(hx, "python main.py")
    assert out.result.get("stuck_detected") is True
    assert "period-2" in out.observations


def test_abc_orbit_is_caught():
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hist(
        [("north", "Hall.\n> "), ("south", "Cave.\n> "), ("look", "Cave.\n> ")],
        _STUCK_IDENTICAL_RUNS,
    )
    out = _run(hx, "north")
    assert out.result.get("stuck_detected") is True
    assert "period-3" in out.observations


def test_orbit_with_varying_output_is_progress_not_stuck():
    """The combat case that got the 58eb83c screen detector removed: the
    same inputs cycling while output CHANGES (HP counters) is a fight, not
    an orbit — byte-identity per position is the bar."""
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = []
    for i in range(_STUCK_IDENTICAL_RUNS):
        hx.append({"input": "attack", "output": f"You hit. Wolf HP {20 - i}.\n> "})
        hx.append({"input": "look", "output": f"A wolf. HP {20 - i}.\n> "})
    out = _run(hx, "attack")
    assert out.result.get("stuck_detected") is not True


def test_a_send_that_breaks_the_cycle_is_not_stuck():
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hist(
        [("python main.py", "void\n> "), ("look", "void\n> ")],
        _STUCK_IDENTICAL_RUNS,
    )
    out = _run(hx, "help")  # a NEW action — the model broke out on its own
    assert out.result.get("stuck_detected") is not True


def test_one_mismatched_repetition_resets_the_orbit_claim():
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hist(
        [("python main.py", "void\n> "), ("look", "void\n> ")],
        _STUCK_IDENTICAL_RUNS,
    )
    hx[3]["output"] = "void, but a door creaks open.\n> "  # state changed once
    out = _run(hx, "python main.py")
    assert out.result.get("stuck_detected") is not True


def test_period_twelve_replay_orbit_is_caught():
    """The 2026-08-22 live case: relaunch, replay an 11-move walkthrough
    route byte-for-byte against a deterministic fresh boot, hit the same
    wall, relaunch. Period 12 — far beyond the original 2/3 — caught by
    the generalized N-period detector at the same byte-identity bar."""
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    route = [
        ("python main.py", "SHADOW KINGDOM\n> "),
        ("1", "New game.\n> "),
        ("north", "Great Hall.\n> "),
        ("north", "You can't go that way.\n> "),
        ("look", "Great Hall.\n> "),
        ("west", "Armory.\n> "),
        ("east", "Great Hall.\n> "),
        ("look", "Great Hall.\n> "),
        ("south", "Entrance.\n> "),
        ("north", "Great Hall.\n> "),
        ("look", "Great Hall.\n> "),
        ("attack goblin", "There is no goblin here.\n> "),
    ]
    hx = _cycle_hist(route, _STUCK_IDENTICAL_RUNS)
    out = _run(hx, "python main.py")
    assert out.result.get("stuck_detected") is True
    assert "period-12" in out.observations


def test_long_period_orbit_one_rep_short_does_not_fire():
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    route = [(f"step{i}", f"Screen {i}\n> ") for i in range(12)]
    hx = _cycle_hist(route, _STUCK_IDENTICAL_RUNS)[:-1]  # one turn short
    out = _run(hx, "step0")
    assert out.result.get("stuck_detected") is not True


def test_smallest_period_wins_the_report():
    """A period-2 orbit is also periodic at 4/6/8 — it must be reported as
    period-2, so the observation names the true cycle."""
    from agent.actions.interactive_actions import _STUCK_IDENTICAL_RUNS

    hx = _cycle_hist(
        [("python main.py", "void\n> "), ("look", "void\n> ")],
        4 * _STUCK_IDENTICAL_RUNS,
    )
    out = _run(hx, "python main.py")
    assert out.result.get("stuck_detected") is True
    assert "period-2" in out.observations
