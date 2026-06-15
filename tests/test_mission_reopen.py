"""`mission reopen` — let a finished mission run again.

A completed/aborted mission has a terminal status, so `start` refuses it
("Mission is 'completed'. Cannot start."). reopen flips it back to active and
stamps provenance (reopen_count + a reopen event). Phase stays a pure function
of goal state, so what runs next follows automatically — these tests cover the
lifecycle transition; the phase routing is covered by the pipeline-phase tests.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.persistence.manager import PersistenceManager
from agent.persistence.models import GoalRecord, MissionConfig, MissionState
from ouroboros import cmd_mission_reopen


def _save_mission(tmp_path, status: str, goals: list[GoalRecord]) -> PersistenceManager:
    pm = PersistenceManager(str(tmp_path))
    pm.save_mission(
        MissionState(
            objective="test",
            status=status,
            goals=goals,
            config=MissionConfig(working_directory=str(tmp_path)),
        )
    )
    return pm


def _args(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(working_dir=str(tmp_path))


def test_reopen_completed_mission_goes_active(tmp_path):
    pm = _save_mission(
        tmp_path,
        "completed",
        [GoalRecord(description="g1", status="complete")],
    )

    cmd_mission_reopen(_args(tmp_path))

    reopened = pm.load_mission()
    assert reopened.status == "active"
    assert reopened.reopen_count == 1
    events = pm.read_events()
    assert any(
        e.type == "reopen" and e.payload.get("from_status") == "completed"
        for e in events
    )


def test_reopen_aborted_mission_goes_active(tmp_path):
    pm = _save_mission(tmp_path, "aborted", [GoalRecord(description="g1")])
    cmd_mission_reopen(_args(tmp_path))
    assert pm.load_mission().status == "active"


def test_reopen_active_mission_is_noop(tmp_path):
    pm = _save_mission(
        tmp_path, "active", [GoalRecord(description="g1", status="complete")]
    )
    cmd_mission_reopen(_args(tmp_path))  # prints guidance, changes nothing
    m = pm.load_mission()
    assert m.status == "active"
    assert m.reopen_count == 0


def test_reopen_paused_mission_refuses(tmp_path):
    _save_mission(tmp_path, "paused", [GoalRecord(description="g1")])
    with pytest.raises(SystemExit):
        cmd_mission_reopen(_args(tmp_path))


def _args_scope(tmp_path, add_goal=None, directive=None) -> SimpleNamespace:
    return SimpleNamespace(
        working_dir=str(tmp_path), add_goal=add_goal, directive=directive
    )


def test_reopen_add_goal_appends_directly_and_dedups(tmp_path):
    pm = _save_mission(
        tmp_path, "completed", [GoalRecord(description="existing", status="complete")]
    )
    cmd_mission_reopen(_args_scope(tmp_path, add_goal=["Add a help alias", "existing"]))
    m = pm.load_mission()
    assert m.status == "active"
    descs = [g.description for g in m.goals]
    assert "Add a help alias" in descs  # new goal appended
    assert descs.count("existing") == 1  # duplicate skipped
    new = next(g for g in m.goals if g.description == "Add a help alias")
    assert new.type == "functional" and new.status == "incomplete"
    assert new.origin == "directive"


def test_reopen_directive_persists_for_planning(tmp_path):
    pm = _save_mission(
        tmp_path, "completed", [GoalRecord(description="g1", status="complete")]
    )
    cmd_mission_reopen(
        _args_scope(tmp_path, directive="Add a boss room behind a puzzle")
    )
    m = pm.load_mission()
    assert m.status == "active"
    assert m.pending_directive == "Add a boss room behind a puzzle"
    assert len(m.goals) == 1  # no goal added directly — awaits planning pass
    events = pm.read_events()
    assert any(e.type == "reopen" and e.payload.get("directive") for e in events)


def test_reopen_increments_across_generations(tmp_path):
    pm = _save_mission(
        tmp_path, "completed", [GoalRecord(description="g1", status="complete")]
    )
    cmd_mission_reopen(_args(tmp_path))
    # Simulate the run finishing again, then reopening once more.
    m = pm.load_mission()
    m.status = "completed"
    pm.save_mission(m)
    cmd_mission_reopen(_args(tmp_path))
    assert pm.load_mission().reopen_count == 2
