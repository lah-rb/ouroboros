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
    m.regression_dirty = True  # a prior edit armed the rule
    fx = MockEffects(commands={_wrap("echo ok"): _cmd(0)})

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 0
    assert beta.status == "complete"
    assert m.regression_dirty is False  # trigger cleared (restart-proof flag)
    assert (
        m.last_regression_cycle == 0
    )  # telemetry still advances (cycle=0 in unit ctx)


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


@pytest.mark.asyncio
async def test_a_disarmed_check_is_replaced_not_just_removed():
    """The deferred "component 3", built 2026-08-06 after the examine goal
    completed with ZERO checks: grounding is one-shot (needs_derive = not
    grounded), so the disarm's completion round skipped re-derivation and the
    goal dropped out of the regression sweep entirely — future edits could
    break it silently, forever. A disarm now resets grounding AND tells THIS
    round's arm_acceptance to derive a fresh check from the pass that just
    happened, so the stale assumption is replaced by one grounded in the
    current world. Derive-after-pass stays inviolate — now_ok IS a pass."""
    cmd = "test -f stale-layout"
    g = _goal("g", [_check(cmd)])
    g.acceptance_grounded = True
    m = _mission([g])
    fx = MockEffects()
    fail_row = check_result("m", ["/bin/sh", "-c", cmd], False, required=True)

    for _ in range(_ACCEPTANCE_DISARM_K - 1):
        out = await action_reconcile_acceptance(_rc_si(m, [fail_row], "g", fx))
        assert g.acceptance_grounded is True, "no reset before the disarm"
        assert "acceptance_needs_derive" not in (out.context_updates or {})

    out = await action_reconcile_acceptance(_rc_si(m, [fail_row], "g", fx))
    assert out.result["now_ok"] is True
    assert g.acceptance_grounded is False, "grounding must reset on disarm"
    assert out.context_updates.get("acceptance_needs_derive") is True, (
        "the completion round must derive the replacement — grounding alone "
        "cannot re-arm a goal that completes this same round"
    )


def test_the_flow_routes_the_replacement_derive():
    """reconcile now_ok -> end_eval_session_success -> arm_acceptance, whose
    resolver reads acceptance_needs_derive — the key reconcile now publishes.
    Pinned off the compiled graph so the route survives refactors."""
    import json as _json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    interact = _json.loads((root / "flows" / "compiled.json").read_text())["interact"]
    steps = interact["steps"]

    assert "acceptance_needs_derive" in steps["reconcile_acceptance"]["publishes"]
    ok = [
        r
        for r in steps["reconcile_acceptance"]["resolver"]["rules"]
        if "now_ok" in r["condition"]
    ]
    assert ok[0]["transition"] == "end_eval_session_success"
    assert steps["end_eval_session_success"]["resolver"]["rules"][0]["transition"] == (
        "arm_acceptance"
    )
    derive = [
        r
        for r in steps["arm_acceptance"]["resolver"]["rules"]
        if "acceptance_needs_derive" in r["condition"]
    ]
    assert derive and derive[0]["transition"] == "derive_acceptance"


# ── the one-way-ratchet fix (2026-08-21) ──────────────────────────────


@pytest.mark.asyncio
async def test_check_driven_reopen_marks_the_check_as_discriminating():
    # Reopening on a FAILED check records the in-episode proof that the check
    # can go red. Without this the same check can destroy verified state and
    # never restore it.
    g = _goal("g", [_check("broke")], status="complete")
    m = _mission([g])
    fx = MockEffects(commands={_wrap("broke"): _cmd(1)})

    await action_regression_sweep(_si(m, fx))

    assert g.status == "incomplete"
    assert g.regression_check_failed is True


@pytest.mark.asyncio
async def test_ungrounded_check_can_reclose_what_it_reopened():
    # THE RATCHET. acceptance_grounded is reset whenever a check is disarmed,
    # so a goal can carry a real, working check with grounded False. Before
    # this fix such a goal was excluded from the auto-complete direction
    # entirely: its check could reopen it and never re-close it, leaving an
    # LLM play-test as the only exit (qwen3.8 completion run — two boss goals
    # went interact -> tester error -> diagnose while their checks were green).
    g = _reopened("g", [_check("nowpasses")])
    g.acceptance_grounded = False
    g.regression_check_failed = True  # this check went red on the regression
    m = _mission([g])
    fx = MockEffects(commands={_wrap("nowpasses"): _cmd(0)})

    out = await action_regression_sweep(_si(m, fx))

    assert g.status == "complete"
    assert out.result["autocompleted"] == 1
    assert g.regression_check_failed is False  # episode closed


@pytest.mark.asyncio
async def test_ungrounded_check_that_never_failed_still_cannot_reclose():
    # The gate still holds for the PRE-EMPTIVE reopen path (a structural goal
    # reopened ahead of a fix edit, not because a check failed). There the
    # check has proved nothing, so a bare pass must not certify it — the
    # vacuous-verification guard stays armed.
    g = _reopened("g", [_check("passes")])
    g.acceptance_grounded = False
    g.regression_check_failed = False
    m = _mission([g])
    fx = MockEffects(commands={_wrap("passes"): _cmd(0)})

    out = await action_regression_sweep(_si(m, fx))

    assert g.status == "incomplete"
    assert out.result["autocompleted"] == 0


@pytest.mark.asyncio
async def test_functional_sweep_recertifies_without_llm_dispatch():
    # The verify-only rung: a check-driven reopened goal whose checks pass
    # completes with NO dispatch_config (no interact, no diagnose). Pins the
    # qwen3.8 waste path — the regression sweep only runs when the mission is
    # regression_dirty, but this loop runs every cycle and gets there first.
    from agent.actions.mission_actions import action_functional_sweep_next

    g = _reopened("g", [_check("nowpasses")])
    g.acceptance_grounded = False
    g.regression_check_failed = True
    g.reports = []
    m = _mission([g])
    fx = MockEffects(commands={_wrap("nowpasses"): _cmd(0)})

    out = await action_functional_sweep_next(_si(m, fx))

    assert g.status == "complete"
    assert not (out.context_updates or {}).get("dispatch_config")


@pytest.mark.asyncio
async def test_functional_sweep_rung_does_not_fire_without_the_proof_flag():
    # A goal that was never reopened by a failing check keeps the normal path,
    # so the rung cannot vacuously certify never-verified work.
    from agent.actions.mission_actions import action_functional_sweep_next

    g = _reopened("g", [_check("passes")])
    g.acceptance_grounded = False
    g.regression_check_failed = False
    g.reports = []
    m = _mission([g])
    fx = MockEffects(commands={_wrap("passes"): _cmd(0)})

    await action_functional_sweep_next(_si(m, fx))

    assert g.status == "incomplete"


# ── gate verdict is recoverable (2026-08-21) ──────────────────────────


def test_step_end_carries_a_bounded_observations_preview():
    """The quality gate's PASSING conclusion was unrecoverable after the fact:
    no note (fail-only), no trace field, and a server log truncated per boot.
    StepEnd now carries the observations, capped so it stays a review aid."""
    from agent.trace import StepEnd
    from agent.runtime import _OBSERVATIONS_PREVIEW_CAP

    e = StepEnd(step="summarize", observations_preview="x" * 5000)
    assert hasattr(e, "observations_preview")
    assert _OBSERVATIONS_PREVIEW_CAP <= 2000


# ── confirm-before-reopen (fixture collisions) ────────────────────────


class _FlakyEffects(MockEffects):
    """Fails a command the first N times, then passes it.

    Models the live defect: the sweep runs its checks concurrently against ONE
    shared workspace, so a check that writes its own fixture can have it
    clobbered by a neighbour — red in the wave, green when run alone.
    """

    def __init__(self, flaky_cmd: str, fail_times: int = 1, **kw):
        super().__init__(**kw)
        self._flaky = flaky_cmd
        self._left = fail_times
        self.run_counts: dict[str, int] = {}

    async def run_command(self, command, working_dir=None, timeout=30):
        cmd_str = " ".join(command)
        self.run_counts[cmd_str] = self.run_counts.get(cmd_str, 0) + 1
        if cmd_str == _wrap(self._flaky):
            if self._left > 0:
                self._left -= 1
                return CommandResult(
                    return_code=1, stdout="", stderr="clobbered", command=cmd_str
                )
            return CommandResult(return_code=0, stdout="", stderr="", command=cmd_str)
        return await super().run_command(command, working_dir, timeout)


@pytest.mark.asyncio
async def test_collision_red_does_not_reopen():
    # The check fails in the wave and passes on the quiet re-run: a collision,
    # not a regression. The goal must survive untouched — this is the exact
    # loop that reopened one qwen3.8 goal 5x while it passed 6/6 in isolation.
    beta = _goal("beta", [_check("play_and_assert")])
    m = _mission([beta])
    fx = _FlakyEffects("play_and_assert", fail_times=1)

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 0
    assert beta.status == "complete"
    assert beta.regression_check_failed is False  # never marked red
    assert fx.run_counts[_wrap("play_and_assert")] == 2  # wave + confirmation
    assert "1 collision(s) held back" in out.observations


@pytest.mark.asyncio
async def test_real_regression_survives_the_confirmation():
    # The whole point of the guard is that it must not blunt real detection:
    # a red that survives isolation still reopens, with its provenance intact.
    beta = _goal("beta", [_check("really_broken")])
    m = _mission([beta])
    fx = _FlakyEffects("really_broken", fail_times=99)

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 1
    assert beta.status == "incomplete"
    assert beta.regression_check_failed is True
    assert beta.regression_reopened is True
    assert fx.run_counts[_wrap("really_broken")] == 2  # confirmed, then believed
    assert "collision" not in out.observations


@pytest.mark.asyncio
async def test_confirmation_probes_from_a_cold_workspace(monkeypatch):
    # Control #2 of the authored-test arm, reused: the re-run must start from
    # the same flushed floor the acceptance rung will later use, or a fixture
    # left by the wave decides the verdict.
    import agent.actions.interactive_actions as ia

    order: list[str] = []

    async def _spy_flush(effects, mission):
        order.append("flush")
        return ["save.json"]

    monkeypatch.setattr(ia, "flush_known_transients", _spy_flush)

    class _OrderedEffects(_FlakyEffects):
        async def run_command(self, command, working_dir=None, timeout=30):
            order.append(" ".join(command))
            return await super().run_command(command, working_dir, timeout)

    beta = _goal("beta", [_check("writes_save_then_loads")])
    m = _mission([beta])
    fx = _OrderedEffects("writes_save_then_loads", fail_times=1)

    await action_regression_sweep(_si(m, fx))

    # wave run, THEN flush, THEN the confirmation run
    assert order == [
        _wrap("writes_save_then_loads"),
        "flush",
        _wrap("writes_save_then_loads"),
    ]


@pytest.mark.asyncio
async def test_recomplete_direction_is_not_confirmed():
    # A spurious red here only leaves a goal open for the next wave, so it buys
    # nothing and would put a serial re-run on the common path.
    beta = _reopened("beta", [_check("still_red")])
    m = _mission([beta])
    fx = _FlakyEffects("still_red", fail_times=1)

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["autocompleted"] == 0
    assert beta.status == "incomplete"
    assert fx.run_counts[_wrap("still_red")] == 1  # no confirmation spent


@pytest.mark.asyncio
async def test_flush_failure_never_aborts_the_sweep(monkeypatch):
    # The flush is best-effort: a workspace that refuses an unlink must not
    # cost us the confirmation, or the guard evaporates exactly when the tree
    # is in the odd state that most needs it.
    import agent.actions.interactive_actions as ia

    async def _boom(effects, mission):
        raise OSError("read-only workspace")

    monkeypatch.setattr(ia, "flush_known_transients", _boom)

    beta = _goal("beta", [_check("play_and_assert")])
    m = _mission([beta])
    fx = _FlakyEffects("play_and_assert", fail_times=1)

    out = await action_regression_sweep(_si(m, fx))

    assert out.result["reopened"] == 0  # probed anyway, and it passed
    assert beta.status == "complete"
