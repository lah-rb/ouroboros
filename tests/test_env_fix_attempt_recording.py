"""The project_ops (environment) fix route must record its attempts.

`FailedAttempt` was appended on the file_ops route only, so on the environment
route `goal.failed_attempts` stayed `[]` forever. That silently disabled three
mechanisms which are all already built and merely starved of input:

  - the "## Prior attempts" section of the diagnose seed
  - the repeat-target "CRITICAL: X has failed N times" warning
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
