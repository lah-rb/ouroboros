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
