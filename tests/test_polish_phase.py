"""The polish phase (rank 60) — a consumer pass above the quality gate.

THE RISK THIS FILE GUARDS. Adding polish moves the terminal rule from rank 50
to rank 60 and turns the quality gate from TERMINAL into a flag_unset rule.
Every mission in the fleet completes through that path, so the first four tests
are about the DEFAULT (top_phase: quality) still behaving exactly as before —
the polish tests come after.
"""

from __future__ import annotations

from agent.flow_sets import CODE_CORE_PHASES, PHASE_RANKS, evaluate_phases
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _mission(**cfg) -> MissionState:
    base = dict(working_directory="/tmp/x", top_phase="quality")
    base.update(cfg)
    m = MissionState(objective="build a thing", config=MissionConfig(**base))
    m.architecture = {"run_command": "python main.py"}
    m.environment_verified = True
    m.tests_verified = True
    return m


def _done_goal(t: str = "functional") -> GoalRecord:
    return GoalRecord(description="d", type=t, status="complete")


# ── the default path, which must not move ────────────────────────────


def test_default_ceiling_reaches_the_quality_gate():
    m = _mission()
    m.goals = [_done_goal()]
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "quality"


def test_default_ceiling_completes_once_quality_is_verified():
    """The whole point: at top_phase=quality a verified gate still completes.

    The rank-60 rules are skipped by the ceiling and exhaustion returns
    'complete' — one extra cycle versus the old terminal, same end state.
    """
    m = _mission()
    m.goals = [_done_goal()]
    m.quality_verified = True
    phase, obs = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "complete", obs


def test_default_ceiling_never_enters_polish_even_with_entries_left():
    m = _mission(polish_max_entries=5)
    m.goals = [_done_goal()]
    m.quality_verified = True
    assert m.polish_entries == 0
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "complete", "polish must be unreachable below its ceiling"


def test_incomplete_functional_work_still_outranks_the_gate():
    m = _mission()
    m.goals = [GoalRecord(description="d", type="functional", status="incomplete")]
    m.quality_verified = True
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "functional"


# ── polish ───────────────────────────────────────────────────────────


def test_polish_ceiling_enters_polish_after_quality_verifies():
    m = _mission(top_phase="polish")
    m.goals = [_done_goal()]
    m.quality_verified = True
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "polish"


def test_polish_does_not_preempt_the_quality_gate():
    """Ordering matters: an unverified gate is worked before any consumer run."""
    m = _mission(top_phase="polish")
    m.goals = [_done_goal()]
    m.quality_verified = False
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "quality"


def test_spent_entries_fall_through_to_complete():
    m = _mission(top_phase="polish", polish_max_entries=1)
    m.goals = [_done_goal()]
    m.quality_verified = True
    m.polish_entries = 1
    phase, obs = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "complete", obs


def test_a_second_entry_is_allowed_when_configured():
    m = _mission(top_phase="polish", polish_max_entries=2)
    m.goals = [_done_goal()]
    m.quality_verified = True
    m.polish_entries = 1
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "polish"


def test_polish_findings_reopen_work_below_it():
    """A consumer finding lands as a functional goal and CLEARS quality_verified,
    so the ladder drops back to functional and must re-pass the gate on the way
    up. This is the loop the entry counter exists to bound."""
    m = _mission(top_phase="polish", polish_max_entries=2)
    m.goals = [
        _done_goal(),
        GoalRecord(
            description="the help text never mentions save",
            type="functional",
            status="incomplete",
            origin="polish_gate",
        ),
    ]
    m.quality_verified = False
    m.polish_entries = 1
    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "functional"


# ── the ladder itself ────────────────────────────────────────────────


def test_polish_outranks_quality():
    assert PHASE_RANKS["polish"] > PHASE_RANKS["quality"]


def test_the_terminal_rule_sits_at_the_top_of_the_ladder():
    terminal = [r for r in CODE_CORE_PHASES if r.kind == "terminal"]
    assert len(terminal) == 1, "exactly one terminal rule"
    assert terminal[0].rank == PHASE_RANKS["polish"]
    # It must resolve to completion, not to a gate name — a terminal that
    # named the polish phase re-dispatched the gate forever.
    assert terminal[0].phase == "complete"


# ── the consumer's session voice and entry point ─────────────────────


def test_the_session_persona_defaults_to_the_operator():
    """Every pre-2026-08-23 caller passes no persona and must be unchanged."""
    from agent.actions.interactive_actions import (
        OPERATOR_PERSONA,
        _session_persona,
    )

    assert _session_persona("") == OPERATOR_PERSONA
    assert _session_persona("personas/operator") == OPERATOR_PERSONA


def test_the_consumer_persona_is_a_different_voice():
    from agent.actions.interactive_actions import (
        OPERATOR_PERSONA,
        _session_persona,
    )

    consumer = _session_persona("personas/consumer")
    assert consumer != OPERATOR_PERSONA
    low = consumer.lower()
    # The operator is told there is software UNDER TEST and that it must not
    # modify it. A consumer knows neither of those things — they were handed a
    # product. If this leaks back in, the flow is a reviewer in costume.
    assert "under test" not in low
    assert "consumer" in low
    assert "confusion" in low, "confusion-as-data is the load-bearing instruction"


def test_an_unknown_persona_falls_back_rather_than_failing_the_run():
    from agent.actions.interactive_actions import (
        OPERATOR_PERSONA,
        _session_persona,
    )

    assert _session_persona("personas/does_not_exist") == OPERATOR_PERSONA


def test_harvest_books_the_entry_even_with_no_findings():
    """A consumer who wanted nothing changed is a RESULT, not a failed run.

    Booking only on findings would let a satisfied pass re-enter forever.
    """
    import asyncio

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish")
    m.quality_verified = True
    out = asyncio.run(
        action_harvest_polish_findings(
            StepInput(context={"mission": m, "polish_findings": "[]"}, params={})
        )
    )
    assert m.polish_entries == 1
    assert m.quality_verified is True, "no findings must not force a re-gate"
    assert out.result["created"] == 0


def test_harvest_lands_functional_goals_and_forces_a_re_gate():
    import asyncio

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish")
    m.quality_verified = True
    findings = (
        '[{"description": "the help text never mentions save", "class": "functional"},'
        ' {"description": "the prose is flat in the second room", "class": "quality"}]'
    )
    asyncio.run(
        action_harvest_polish_findings(
            StepInput(context={"mission": m, "polish_findings": findings}, params={})
        )
    )
    landed = [g for g in m.goals if g.origin == "polish_gate"]
    assert {g.type for g in landed} == {"functional", "quality"}
    assert m.quality_verified is False, "consumer findings must re-open the gate"
    assert m.polish_entries == 1


def test_a_repeated_finding_reopens_rather_than_duplicating():
    import asyncio

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish", polish_max_entries=2)
    finding = '[{"description": "the help text never mentions save"}]'
    asyncio.run(
        action_harvest_polish_findings(
            StepInput(context={"mission": m, "polish_findings": finding}, params={})
        )
    )
    for g in m.goals:
        g.status = "complete"
    asyncio.run(
        action_harvest_polish_findings(
            StepInput(context={"mission": m, "polish_findings": finding}, params={})
        )
    )
    polish_goals = [g for g in m.goals if g.origin == "polish_gate"]
    assert len(polish_goals) == 1, "a rephrased repeat must not spawn a second goal"
    assert polish_goals[0].status == "incomplete"
    assert m.polish_entries == 2


# ── phase stacking: a reopen must not re-verify what it already earned ─


def test_completing_at_a_phase_earns_the_flags_below_it():
    from agent.flow_sets import CODE_CORE_PHASES, flags_satisfied_at

    assert flags_satisfied_at("quality", CODE_CORE_PHASES) == [
        "environment_verified",
        "tests_verified",
        "quality_verified",
    ]
    # Stopping at structural earns nothing — no flag rule sits at or below it.
    assert flags_satisfied_at("structural", CODE_CORE_PHASES) == []
    # test_suite earns environment and its own, but NOT quality above it.
    assert "quality_verified" not in flags_satisfied_at("test_suite", CODE_CORE_PHASES)


def test_an_unknown_or_empty_completed_phase_earns_nothing():
    """A legacy mission carries completed_at_phase="" — it must not be read as
    a licence to skip verification."""
    from agent.flow_sets import CODE_CORE_PHASES, flags_satisfied_at

    assert flags_satisfied_at("", CODE_CORE_PHASES) == []
    assert flags_satisfied_at("nonsense", CODE_CORE_PHASES) == []


def test_a_quality_completed_mission_reopened_at_polish_goes_straight_to_polish():
    """The behaviour the seeding exists for.

    Before it, this mission re-ran the quality gate whose fresh UX session
    harvested four `untested:` goals for features it had already verified.
    """
    from agent.flow_sets import CODE_CORE_PHASES, flags_satisfied_at

    m = _mission(top_phase="polish")
    m.goals = [_done_goal()]
    m.completed_at_phase = "quality"
    # what `mission resume` now does
    for flag in flags_satisfied_at(m.completed_at_phase, CODE_CORE_PHASES):
        setattr(m, flag, True)

    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "polish", "a quality-complete mission must not re-gate quality"


def test_seeding_does_not_skip_a_phase_the_mission_never_reached():
    """Reopened at polish having only completed at test_suite: quality is still
    owed and must run before the consumer ever sees the build."""
    from agent.flow_sets import CODE_CORE_PHASES, flags_satisfied_at

    m = _mission(top_phase="polish")
    m.goals = [_done_goal()]
    m.completed_at_phase = "test_suite"
    for flag in flags_satisfied_at(m.completed_at_phase, CODE_CORE_PHASES):
        setattr(m, flag, True)

    phase, _ = evaluate_phases(m, CODE_CORE_PHASES)
    assert phase == "quality"


# ── triage: complaint -> route ────────────────────────────────────────
#
# The consumer states a COMPLAINT; a goal description is read downstream as a
# SPECIFICATION (diagnose renders it as "THE GOAL: <text>"). On the qwen3.8
# polish run a model spent a 12,000-token turn unable to tell which it had
# been handed — "If goal is desired behavior ... main is wrong. If goal is bug
# report ... current code might be fixed?" — and the finding that caused it
# carried four separate requirements in one sentence, which no single fix and
# no single acceptance check could satisfy. Triage runs after the consumer is
# gone, sees the architecture, rewrites to target state and routes.


def _route(raw: str):
    import asyncio

    from agent.actions.polish_actions import action_route_polish_findings
    from agent.models import StepInput

    return asyncio.run(
        action_route_polish_findings(
            StepInput(context={"inference_response": raw}, params={})
        )
    )


def test_triage_splits_fix_from_design():
    out = _route(
        '```json\n{"findings": ['
        '{"description": "the help text names how to save", "class": "functional", "route": "fix"},'
        '{"description": "death warns, keeps progress, and allows continuing", '
        '"class": "functional", "route": "design"}]}\n```'
    )
    routed = out.context_updates["triaged_findings"]
    assert [f["route"] for f in routed] == ["fix", "design"]
    assert out.result["design"] == 1


def test_an_unroutable_finding_degrades_to_fix():
    """Failing closed would DROP a consumer's complaint — the one outcome this
    gate exists to prevent. The fix loop has always carried these."""
    out = _route(
        '{"findings": [{"description": "the prose is flat", "class": "quality"}]}'
    )
    routed = out.context_updates["triaged_findings"]
    assert routed[0]["route"] == "fix"


def test_design_findings_become_a_directive_not_goals():
    import asyncio

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish")
    m.quality_verified = True
    triaged = [
        {
            "description": "the help text names how to save",
            "class": "functional",
            "route": "fix",
        },
        {
            "description": "death warns, keeps progress, and allows continuing",
            "class": "functional",
            "route": "design",
        },
    ]
    asyncio.run(
        action_harvest_polish_findings(
            StepInput(context={"mission": m, "triaged_findings": triaged}, params={})
        )
    )

    filed = [g for g in m.goals if g.origin == "polish_gate"]
    assert [g.description for g in filed] == ["the help text names how to save"]
    assert "death warns, keeps progress" in m.pending_directive
    assert len(m.polish_designed) == 1
    assert m.quality_verified is False  # a directive is landed work too
    assert m.polish_entries == 1


def test_a_design_finding_is_not_re_routed_on_a_later_entry():
    """It carries no goal to dedup against, so without the signature list the
    same complaint would spend a replan on every later entry, forever."""
    import asyncio

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish")
    triaged = [
        {
            "description": "death warns and keeps progress",
            "class": "functional",
            "route": "design",
        }
    ]
    for _ in range(2):
        m.pending_directive = ""  # replan consumed it between entries
        asyncio.run(
            action_harvest_polish_findings(
                StepInput(
                    context={"mission": m, "triaged_findings": triaged}, params={}
                )
            )
        )

    assert len(m.polish_designed) == 1
    assert m.pending_directive == "", "the repeat must not raise a second directive"
    assert m.polish_entries == 2


def test_untriaged_findings_still_land_as_before():
    """A mission that predates triage, or a flow set without the step, must
    behave exactly as it did — every finding a fix goal."""
    import asyncio

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish")
    findings = (
        '[{"description": "the help text never mentions save", "class": "functional"}]'
    )
    asyncio.run(
        action_harvest_polish_findings(
            StepInput(context={"mission": m, "polish_findings": findings}, params={})
        )
    )
    assert [g.description for g in m.goals if g.origin == "polish_gate"] == [
        "the help text never mentions save"
    ]
    assert not m.pending_directive


def test_the_formatter_accepts_the_raw_string_conclude_emits():
    """THE no-op. polish_findings is carried through as the raw fenced JSON
    string, not a parsed list. Taking only lists handed triage an empty set,
    so it reported "the user reported nothing" and harvest silently fell back
    to untriaged filing — the step ran, logged, and did nothing."""
    from agent.formatters import format_polish_findings

    raw = (
        '```json\n{"findings": [{"description": "the help text never mentions '
        'save", "class": "functional"}]}\n```'
    )
    out = format_polish_findings({"source": raw}, {})
    assert "the help text never mentions save" in out
    assert "reported nothing" not in out


def test_an_empty_triage_falls_back_loudly(caplog):
    """The fallback must stay — never drop a consumer's findings — but it must
    be visible, or a broken triage is indistinguishable from a clean run."""
    import asyncio
    import logging

    from agent.actions.polish_actions import action_harvest_polish_findings
    from agent.models import StepInput

    m = _mission(top_phase="polish")
    findings = (
        '[{"description": "the help text never mentions save", "class": "functional"}]'
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            action_harvest_polish_findings(
                StepInput(
                    context={
                        "mission": m,
                        "polish_findings": findings,
                        "triaged_findings": [],
                    },
                    params={},
                )
            )
        )
    assert [g for g in m.goals if g.origin == "polish_gate"], "findings must survive"
    assert any("UNTRIAGED" in r.message for r in caplog.records)


def test_triage_contention_reference_lists_only_verified_behaviours():
    """The settled-behaviour half of triage's contention trigger checks
    against this list. Structural goals stay out (the architecture block
    already covers modules) and incomplete goals stay out (the product has
    committed to nothing an unfinished goal describes)."""
    from agent.formatters import format_verified_behaviours

    goals = [
        GoalRecord(
            description="player can drop items", type="functional", status="complete"
        ),
        GoalRecord(
            description="death warns before restarting",
            type="functional",
            status="complete",
        ),
        GoalRecord(description="wip behaviour", type="functional", status="incomplete"),
        GoalRecord(description="engine module", type="structural", status="complete"),
    ]
    out = format_verified_behaviours({"source": goals}, {})
    assert "player can drop items" in out
    assert "death warns before restarting" in out
    assert "wip behaviour" not in out
    assert "engine module" not in out
    assert format_verified_behaviours({"source": ""}, {}) == "(none verified yet)"
