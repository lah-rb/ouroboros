"""regress-startup-on-edit: editing a file must re-open the startup goal.

The startup goal ("Program starts cleanly...") is verified by actually running
the program. When a file is edited, the agent regresses the file's structural
goal; it must ALSO regress the startup goal so the startup check re-runs —
catching an edit that broke a passing startup, and verifying a fix.
"""

from types import SimpleNamespace

from agent.actions.mission_actions import _regress_startup_goal
from agent.persistence.models import GoalRecord


def _mission(*goals):
    return SimpleNamespace(goals=list(goals))


def _startup(status="complete"):
    return GoalRecord(
        description="Program starts cleanly and exits without errors",
        type="functional",
        status=status,
        interaction_mode="deterministic",
    )


def test_regresses_complete_startup_goal():
    g = _startup("complete")
    m = _mission(g)
    assert _regress_startup_goal(m) is True
    assert g.status == "incomplete"


def test_noop_when_startup_already_incomplete():
    g = _startup("incomplete")
    m = _mission(g)
    assert _regress_startup_goal(m) is False
    assert g.status == "incomplete"


def test_noop_when_no_startup_goal():
    structural = GoalRecord(
        description="engine.py", type="structural", status="complete"
    )
    exploratory = GoalRecord(
        description="User can move",
        type="functional",
        status="complete",
        interaction_mode="exploratory",
    )
    m = _mission(structural, exploratory)
    assert _regress_startup_goal(m) is False
    assert structural.status == "complete"  # untouched
    assert exploratory.status == "complete"  # untouched


def test_only_deterministic_functional_is_regressed():
    structural = GoalRecord(description="main.py", type="structural", status="complete")
    startup = _startup("complete")
    m = _mission(structural, startup)
    _regress_startup_goal(m)
    assert startup.status == "incomplete"
    assert (
        structural.status == "complete"
    )  # structural regression is handled separately
