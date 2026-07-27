"""The project_ops (environment) fix route must record its attempts.

`FailedAttempt` was appended on the file_ops route only, so on the environment
route `goal.failed_attempts` stayed `[]` forever. That silently disabled three
mechanisms which are all already built and merely starved of input:

  - the "## Prior attempts" section of the diagnose seed
  - the repeat-target CRITICAL warning ("N fix attempts have targeted X and
    the goal still fails")
  - the stuck-goal web-search gate (`search_gate` requires >= 2 attempts)

So every diagnose cycle on that route was seeded byte-identically and the model,
correctly, produced an identical conclusion — 26 times in one run before the
wall-clock backstop stopped it (dev/POOLSIDE_TRAP_ROOTCAUSE.md).
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import (
    _ENV_ATTEMPT_TARGET,
    _sweep_after_project_ops,
)
from agent.persistence.models import DirectiveReport, GoalRecord


def _goal_with_env_history() -> GoalRecord:
    """A functional goal that failed a startup test, was diagnosed, and has just
    had an environment fix applied."""
    g = GoalRecord(description="Program starts cleanly and exits without errors",
                   type="functional", interaction_mode="deterministic")
    g.reports = [
        DirectiveReport(flow="interact", status="failure",
                   headline="ModuleNotFoundError: No module named 'yaml'",
                   summary="startup failed"),
        DirectiveReport(flow="diagnose_issue", status="success",
                   summary="PyYAML is declared but not installed"),
        DirectiveReport(flow="project_ops", status="success",
                   summary="setup complete; install commands exited 0"),
    ]
    return g


@pytest.mark.asyncio
async def test_project_ops_route_records_the_attempt():
    goal = _goal_with_env_history()
    assert goal.failed_attempts == []
    await _sweep_after_project_ops(
        goal, goal.reports[-1], "success", "deterministic", "python main.py", ""
    )
    assert len(goal.failed_attempts) == 1, "the env fix attempt must be recorded"
    att = goal.failed_attempts[0]
    assert att.flow == "project_ops"
    assert att.target_file == _ENV_ATTEMPT_TARGET


@pytest.mark.asyncio
async def test_recorded_on_success_too_because_success_is_not_a_fix():
    """project_ops reporting success only means its install commands exited 0.
    The empty-venv run reported success on every cycle while the venv stayed
    empty, so 'success' cannot be the signal that an attempt need not be
    recorded. If the re-test passes the goal completes and the record goes with
    it."""
    goal = _goal_with_env_history()
    await _sweep_after_project_ops(
        goal, goal.reports[-1], "success", "deterministic", "python main.py", ""
    )
    assert len(goal.failed_attempts) == 1


@pytest.mark.asyncio
async def test_repeated_env_fixes_accumulate_so_the_warning_can_fire():
    """The repeat-target warning keys on target_file and needs a count >= 2;
    an empty target_file is skipped entirely, hence the stable marker."""
    goal = _goal_with_env_history()
    for _ in range(3):
        await _sweep_after_project_ops(
            goal, goal.reports[-1], "success", "deterministic", "python main.py", ""
        )
    assert len(goal.failed_attempts) == 3
    keys = {a.target_file for a in goal.failed_attempts}
    assert keys == {_ENV_ATTEMPT_TARGET}, "must share one key so counts add up"
    assert all(a.target_file for a in goal.failed_attempts), "empty keys are skipped"


@pytest.mark.asyncio
async def test_attempt_carries_the_diagnosis_and_pre_headline():
    """Feeds the before/after regression comparison in the next diagnose seed."""
    goal = _goal_with_env_history()
    await _sweep_after_project_ops(
        goal, goal.reports[-1], "failure", "deterministic", "python main.py", ""
    )
    att = goal.failed_attempts[0]
    assert "PyYAML" in att.diagnosis_summary
    assert "ModuleNotFoundError" in att.pre_headline


class TestRepeatWarningWording:
    """The warning's SUBJECT must be the action, not the file.

    "engine.py has failed 2 times" reads just as easily as "the edits failed to
    land on disk" — a mechanical failure the model cannot act on — as "editing
    it did not fix the goal". Only the second is true, and only the second hints
    the defect may not be in that file at all. TRAP_BRIEF §7 records a run where
    116 of 116 edits hit the wrong package while the runtime error named the
    right file 117 times.
    """

    def test_the_subject_is_the_edit_not_the_file(self):
        from agent.actions.diagnosis_session_actions import repeat_target_warning

        w = repeat_target_warning("engine.py", 2)
        assert "editing engine.py has failed to resolve the goal 2 times" in w
        # The ambiguous form must not survive anywhere in the sentence.
        assert "engine.py has failed 2 times" not in w

    def test_it_rules_out_the_write_having_failed(self):
        from agent.actions.diagnosis_session_actions import repeat_target_warning

        w = repeat_target_warning("engine.py", 3)
        assert "not that the edits failed to apply" in w

    def test_it_offers_relocalisation_as_a_peer_option(self):
        """Two equally-weighted next moves, not a prohibition — "DO NOT" framing
        makes models avoid even partial-match targets, or rebel outright."""
        from agent.actions.diagnosis_session_actions import repeat_target_warning

        w = repeat_target_warning("engine.py", 2)
        assert "sharpen the instruction" in w
        assert "defect lies elsewhere" in w
        assert "DO NOT" not in w

    def test_symbol_qualified_targets_read_naturally(self):
        from agent.actions.diagnosis_session_actions import repeat_target_warning

        w = repeat_target_warning("engine.py:resolve_turn", 2)
        assert "editing engine.py:resolve_turn has failed" in w

    def test_environment_attempts_get_their_own_phrasing(self):
        """"editing <environment>" would be nonsense, and the mechanical reading
        to rule out is different: those commands ran, and exited clean."""
        from agent.actions.diagnosis_session_actions import (
            ENV_ATTEMPT_TARGET,
            repeat_target_warning,
        )

        w = repeat_target_warning(ENV_ATTEMPT_TARGET, 2)
        assert "editing" not in w
        assert "2 environment fixes have been applied" in w
        assert "not that they failed to run" in w
        assert "is not environmental" in w

    def test_env_marker_matches_the_one_the_sweep_records(self):
        """A drift between these two makes every env attempt fall through to the
        file phrasing and render "editing <environment> has failed"."""
        from agent.actions.diagnosis_session_actions import ENV_ATTEMPT_TARGET
        from agent.actions.mission_actions import _ENV_ATTEMPT_TARGET

        assert ENV_ATTEMPT_TARGET == _ENV_ATTEMPT_TARGET

    def test_count_is_interpolated_not_hardcoded(self):
        from agent.actions.diagnosis_session_actions import repeat_target_warning

        assert "7 times" in repeat_target_warning("a.py", 7)
        assert "4 environment fixes" in repeat_target_warning("<environment>", 4)
