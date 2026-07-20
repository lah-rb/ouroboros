"""Cross-goal regression suite + disarm-on-behavior-refute backstop.

The per-goal acceptance checks accumulate into an emergent regression suite.
`action_regression_sweep` runs EVERY completed goal's required checks in
parallel after an edit lands and reopens any goal whose check now fails —
catching an edit for one goal breaking another (live: a boss-combat parser
refactor broke movement, which the movement goal's check would have caught).

`action_reconcile_acceptance` is the safety valve: a check refuted by behavior
(goal_met=true while the check fails — a brittle/stateful/intentionally-edited
check) is disarmed at K, since grounded checks never re-derive. A real
regression keeps behavior failing → never routes to disarm (safe by
construction). Domain-neutral fixtures — no game/parser semantics.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.check_result import check_result
from agent.actions.mission_actions import action_regression_sweep
from agent.actions.pipeline_actions import (
    _ACCEPTANCE_DISARM_K,
    action_reconcile_acceptance,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _wrap(cmd: str) -> str:
    # MockEffects.run_command keys on " ".join(command); the sweep runs
    # ["/bin/sh","-c",cmd] → this is the lookup key.
    return "/bin/sh -c " + cmd


def _cmd(rc: int) -> CommandResult:
    return CommandResult(return_code=rc, stdout="", stderr="", command="")


def _check(cmd: str, required: bool = True) -> dict:
    return {"command": cmd, "name": cmd[:24], "required": required}


def _goal(gid: str, checks: list[dict], status: str = "complete") -> GoalRecord:
    return GoalRecord(
        id=gid,
        description=f"{gid} goal",
        type="functional",
        status=status,
        acceptance_checks=checks,
        acceptance_grounded=True,
    )


def _mission(goals: list[GoalRecord]) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=goals,
    )


def _si(mission, effects, last_goal_id: str = "") -> StepInput:
    return StepInput(
        context={"mission": mission, "last_goal_id": last_goal_id},
        inputs={"last_goal_id": last_goal_id},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="regression_sweep_next"),
        effects=effects,
    )


def _reopened(gid: str, checks: list[dict]) -> GoalRecord:
    # An incomplete goal the sweep itself reopened (auto-complete-eligible).
    g = _goal(gid, checks, status="incomplete")
    g.regression_reopened = True
    return g


# ── the sweep ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_catches_cross_goal_regression():
    beta_cmd = "test -f marker_beta"
    alpha = _goal("alpha", [_check("echo ok")])
    beta = _goal("beta", [_check(beta_cmd)])  # its check now FAILS
    m = _mission([alpha, beta])
    fx = MockEffects(commands={_wrap("echo ok"): _cmd(0), _wrap(beta_cmd): _cmd(1)})

    out = await action_regression_sweep(_si(m, fx, last_goal_id="alpha"))

    assert out.result["reopened"] == 1
    assert beta.status == "incomplete"  # victim reopened
    assert alpha.status == "complete"  # completer untouched
    note = next(n for n in m.notes if "regression" in (n.tags or []))
    assert "beta goal" in note.content and "alpha" in note.content
    assert len(fx.calls_to("save_mission")) == 1


@pytest.mark.asyncio
async def test_sweep_all_pass_reopens_nothing_but_clears_trigger():
    beta = _goal("beta", [_check("echo ok")])
    m = _mission([beta])
    m.last_edit_cycle = 4  # a prior edit armed the rule
    fx = MockEffects(commands={_wrap("echo ok"): _cmd(0)})

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 0
    assert beta.status == "complete"
    assert m.last_regression_cycle == 0  # trigger cleared (cycle=0 in unit ctx)


@pytest.mark.asyncio
async def test_sweep_runs_all_checks_no_short_circuit():
    # FIRST goal's check fails — a short-circuit (the run_validation_checks
    # break-on-first-required-fail) would miss the third goal. Both failing
    # owners must reopen; the passing one stays complete.
    a = _goal("a", [_check("check_a")])
    b = _goal("b", [_check("check_b")])
    c = _goal("c", [_check("check_c")])
    m = _mission([a, b, c])
    fx = MockEffects(
        commands={
            _wrap("check_a"): _cmd(1),
            _wrap("check_b"): _cmd(0),
            _wrap("check_c"): _cmd(1),
        }
    )

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 2
    assert a.status == "incomplete" and c.status == "incomplete"
    assert b.status == "complete"


@pytest.mark.asyncio
async def test_sweep_skips_just_completed_goal():
    alpha = _goal("alpha", [_check("would_fail")])
    m = _mission([alpha])
    fx = MockEffects(commands={_wrap("would_fail"): _cmd(1)})

    out = await action_regression_sweep(_si(m, fx, last_goal_id="alpha"))

    assert alpha.status == "complete"  # skipped, not reopened
    assert out.result["reopened"] == 0


@pytest.mark.asyncio
async def test_sweep_note_carries_original_piped_command():
    cmd = "grep -qF TOKEN out.txt | wc -l"  # pipe: must survive /bin/sh -c wrap
    beta = _goal("beta", [_check(cmd)])
    m = _mission([beta])
    fx = MockEffects(commands={_wrap(cmd): _cmd(1)})

    await action_regression_sweep(_si(m, fx))

    assert beta.status == "incomplete"
    assert any(cmd[:40] in n.content for n in m.notes)


@pytest.mark.asyncio
async def test_sweep_only_required_checks():
    # A non-required (lint-style) check that fails must NOT reopen the goal.
    beta = _goal("beta", [_check("soft", required=False)])
    m = _mission([beta])
    fx = MockEffects(commands={_wrap("soft"): _cmd(1)})

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 0
    assert beta.status == "complete"


# ── the disarm-on-behavior-refute backstop ────────────────────────────


def _rc_si(mission, results, goal_id, effects) -> StepInput:
    return StepInput(
        context={"mission": mission, "validation_results": results},
        inputs={"goal_id": goal_id},
        params={},
        meta=FlowMeta(flow_name="interact", step_id="reconcile_acceptance"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_reconcile_disarms_within_k():
    cmd = "test -f brittle"
    g = _goal("g", [_check(cmd)])
    m = _mission([g])
    fx = MockEffects()
    # row command is the list form (as run_acceptance_checks produces).
    fail_row = check_result("m", ["/bin/sh", "-c", cmd], False, required=True)

    for _ in range(_ACCEPTANCE_DISARM_K - 1):
        out = await action_reconcile_acceptance(_rc_si(m, [fail_row], "g", fx))
        assert out.result["now_ok"] is False  # still vetoing before K
        assert any(c["command"] == cmd for c in g.acceptance_checks)  # still armed

    out = await action_reconcile_acceptance(_rc_si(m, [fail_row], "g", fx))
    assert out.result["now_ok"] is True  # disarmed → goal may complete
    assert not any(c["command"] == cmd for c in g.acceptance_checks)  # removed
    assert cmd not in g.acceptance_conflicts  # counter dropped on disarm
    assert any("DISARMED" in n.content for n in m.notes)


@pytest.mark.asyncio
async def test_reconcile_multi_check_only_failing_disarmed():
    ok_cmd, bad_cmd = "echo ok", "test -f brittle"
    g = _goal("g", [_check(ok_cmd), _check(bad_cmd)])
    m = _mission([g])
    fx = MockEffects()
    results = [
        check_result("ok", ["/bin/sh", "-c", ok_cmd], True, required=True),
        check_result("bad", ["/bin/sh", "-c", bad_cmd], False, required=True),
    ]
    for _ in range(_ACCEPTANCE_DISARM_K):
        out = await action_reconcile_acceptance(_rc_si(m, results, "g", fx))

    assert any(c["command"] == ok_cmd for c in g.acceptance_checks)  # passing retained
    assert not any(c["command"] == bad_cmd for c in g.acceptance_checks)  # bad disarmed
    assert out.result["now_ok"] is True


def test_parse_evaluation_routes_reconcile_only_on_behavior_pass():
    # Safe-by-construction: reconcile (disarm) is reachable ONLY when the
    # behavior passed (goal_met=true) while a check failed. A real regression
    # (goal_met=false) falls through to terminal failure and never disarms.
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    rules = compiled["interact"]["steps"]["parse_evaluation"]["resolver"]["rules"]
    recon = [r for r in rules if r.get("transition") == "reconcile_acceptance"]
    assert len(recon) == 1
    cond = recon[0]["condition"]
    assert "goal_met') == true" in cond
    assert "acceptance_ok', true) == false" in cond
    assert rules[-1]["transition"] == "end_eval_session_failure"


def test_regression_step_wired_in_both_controllers():
    compiled = json.loads(
        (Path(__file__).resolve().parent.parent / "flows" / "compiled.json").read_text()
    )
    for ctrl in ("mission_control", "mission_control_swarm"):
        steps = compiled[ctrl]["steps"]
        assert "regression_sweep_next" in steps, f"{ctrl} missing the step"
        routes = [
            r["transition"]
            for r in steps["check_phase"]["resolver"]["rules"]
            if r.get("transition") == "regression_sweep_next"
        ]
        assert routes, f"{ctrl} check_phase does not route 'regression'"


# ── auto-complete direction (bidirectional sweep) ─────────────────────


@pytest.mark.asyncio
async def test_sweep_autocompletes_reopened_goal_on_pass():
    g = _reopened("g", [_check("recheck")])
    m = _mission([g])
    fx = MockEffects(commands={_wrap("recheck"): _cmd(0)})

    out = await action_regression_sweep(_si(m, fx))

    assert g.status == "complete"
    assert g.regression_reopened is False
    assert g.regression_autocompleted is True
    assert g.acceptance_checks == [_check("recheck")]  # NOT cleared
    assert out.result["autocompleted"] == 1
    assert len(fx.calls_to("save_mission")) == 1
    assert any("auto_complete" in (n.tags or []) for n in m.notes)


@pytest.mark.asyncio
async def test_wave_reclears_blast_radius_after_root_fix():
    gs = [_reopened(f"g{i}", [_check(f"c{i}")]) for i in range(3)]
    m = _mission(gs)
    fx = MockEffects(commands={_wrap(f"c{i}"): _cmd(0) for i in range(3)})

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["autocompleted"] == 3  # whole blast radius in one sweep
    assert all(g.status == "complete" for g in gs)


@pytest.mark.asyncio
async def test_only_sweep_reopened_goals_autocomplete():
    # Incomplete grounded goal that was NOT reopened by the sweep (harvester/
    # design reopen) — a passing check must NOT auto-complete it.
    g = _goal("g", [_check("passes")], status="incomplete")  # regression_reopened=False
    m = _mission([g])
    fx = MockEffects(commands={_wrap("passes"): _cmd(0)})

    out = await action_regression_sweep(_si(m, fx))

    assert g.status == "incomplete"
    assert out.result["autocompleted"] == 0


@pytest.mark.asyncio
async def test_reopened_goal_failing_check_stays_reopened():
    g = _reopened("g", [_check("still_fails")])
    m = _mission([g])
    fx = MockEffects(commands={_wrap("still_fails"): _cmd(1)})

    out = await action_regression_sweep(_si(m, fx))

    assert g.status == "incomplete"
    assert g.regression_reopened is True
    assert out.result["autocompleted"] == 0


@pytest.mark.asyncio
async def test_flipflop_guard_forces_interact_after_first_autocomplete():
    # A goal already auto-completed once, now breaking again, must reopen with
    # regression_reopened=False (forced down the interact/disarm path); a fresh
    # goal reopens eligible (True).
    g = _goal("g", [_check("now_fails")], status="complete")
    g.regression_autocompleted = True
    fresh = _goal("f", [_check("also_fails")], status="complete")
    m = _mission([g, fresh])
    fx = MockEffects(
        commands={_wrap("now_fails"): _cmd(1), _wrap("also_fails"): _cmd(1)}
    )

    await action_regression_sweep(_si(m, fx))

    assert g.status == "incomplete" and g.regression_reopened is False
    assert fresh.status == "incomplete" and fresh.regression_reopened is True


@pytest.mark.asyncio
async def test_multi_check_goal_needs_all_pass_to_autocomplete():
    # Two required checks, only one passes → must NOT auto-complete (pins the
    # per-goal aggregation; a per-check loop would wrongly complete it).
    g = _reopened("g", [_check("passA"), _check("failB")])
    m = _mission([g])
    fx = MockEffects(commands={_wrap("passA"): _cmd(0), _wrap("failB"): _cmd(1)})

    out = await action_regression_sweep(_si(m, fx))

    assert g.status == "incomplete"
    assert out.result["autocompleted"] == 0


@pytest.mark.asyncio
async def test_bidirectional_same_batch():
    reopened_g = _reopened("r", [_check("fixed")])
    complete_g = _goal("c", [_check("broke")], status="complete")
    m = _mission([reopened_g, complete_g])
    fx = MockEffects(commands={_wrap("fixed"): _cmd(0), _wrap("broke"): _cmd(1)})

    out = await action_regression_sweep(_si(m, fx))

    assert reopened_g.status == "complete"
    assert complete_g.status == "incomplete"
    assert out.result == {"reopened": 1, "autocompleted": 1}
    assert len(fx.calls_to("save_mission")) == 1
