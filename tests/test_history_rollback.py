"""Rollback: the workspace and the mission state go back to a turn's tree.

Undoable (the current tree is committed first), refused while a mission
process holds the lock, honest about what it cannot restore, and it leaves
excluded directories alone. After a rollback the persistence layer loads the
restored mission.json and re-bootstraps its Loro journal without complaint.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.effects.local import LocalEffects
from agent.history import reader
from agent.history.rollback import format_report, resolve_target, rollback
from agent.history.store import HistoryLocked, cli_lock
from agent.persistence.manager import PersistenceManager
from agent.persistence.models import MissionConfig, MissionState
from agent.trace import CycleStart, InferenceCall, StepEnd, step_context

_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


def _mission(tmp_path) -> tuple[PersistenceManager, MissionState]:
    pm = PersistenceManager(str(tmp_path))
    pm.init_agent_dir()
    m = MissionState(
        objective="build it",
        status="paused",
        config=MissionConfig(working_directory=str(tmp_path)),
    )
    pm.save_mission(m)
    return pm, m


def _three_steps(tmp_path) -> tuple[LocalEffects, list[dict]]:
    """A run with three turns, each followed by a write: turn 1 sees an
    empty workspace, turn 2 sees a.py, turn 3 sees a.py + b.py (+ .venv)."""
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "keep.txt").write_text("venv")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    eff = LocalEffects(str(tmp_path))
    _run(eff.emit_trace(CycleStart(mission_id="m1")))
    for i, (name, content) in enumerate(
        [("a.py", "A = 1\n"), ("b.py", "B = 2\n"), ("a.py", "A = 3\n")]
    ):
        with step_context("m1", 0, "build", f"s{i}"):
            _run(
                eff.emit_trace(
                    InferenceCall(
                        mission_id="m1",
                        step=f"s{i}",
                        prompt_content=f"p{i}",
                        response_content=f"r{i}",
                    )
                )
            )
            _run(eff.write_file(name, content))
            _run(eff.emit_trace(StepEnd(mission_id="m1", step=f"s{i}")))
    _run(eff.history_close("paused"))
    return eff, reader.load_turns(str(tmp_path / ".agent"))


def test_rollback_to_a_turn_restores_exact_bytes_and_deletes_later_files(tmp_path):
    pm, mission = _mission(tmp_path)
    eff, turns = _three_steps(tmp_path)
    # turn 2 (seq order) saw a.py = "A = 1" and no b.py
    t2 = turns[1]
    assert (tmp_path / "a.py").read_text() == "A = 3\n" and (tmp_path / "b.py").exists()

    dry = rollback(str(tmp_path), to=t2["turn_id"], dry_run=True)
    assert (
        dry.dry_run
        and set(dry.restore.restored) >= {"a.py"}
        and dry.restore.deleted == ["b.py"]
    )
    assert (tmp_path / "b.py").exists() and (
        tmp_path / "a.py"
    ).read_text() == "A = 3\n", "dry run touched nothing"

    report = rollback(str(tmp_path), to=t2["turn_id"])
    assert (tmp_path / "a.py").read_text() == "A = 1\n"
    assert not (tmp_path / "b.py").exists()
    assert (
        tmp_path / ".venv" / "keep.txt"
    ).read_text() == "venv", "excluded dirs untouched"
    assert (tmp_path / ".git" / "HEAD").exists()
    assert report.pre_rollback_commit and report.rollback_commit
    assert report.rollback_commit != report.pre_rollback_commit
    print(format_report(report))  # renders without error

    agent_dir = str(tmp_path / ".agent")
    commits = reader.load_commits(agent_dir)
    assert [c["source"] for c in commits[-2:]] == ["pre_rollback", "rollback"]
    assert (
        commits[-1]["parent_sha"] == commits[-2]["commit_sha"]
    ), "append-only: the rollback commit descends from pre_rollback"
    events = [e for e in reader.load_events(agent_dir) if e["event_type"] == "rollback"]
    assert (
        len(events) == 1
        and events[0]["turn_id"] == t2["turn_id"]
        and events[0]["deleted"] == ["b.py"]
    )
    runs = reader.list_runs(agent_dir)
    assert (
        runs[-1]["run_id"].startswith("cli-rollback-")
        and runs[-1]["final_status"] == "rollback"
    )


def test_rolling_back_to_the_pre_rollback_commit_undoes_it_byte_for_byte(tmp_path):
    _mission(tmp_path)
    eff, turns = _three_steps(tmp_path)
    before = {p: (tmp_path / p).read_text() for p in ("a.py", "b.py")}
    report = rollback(str(tmp_path), to=turns[0]["turn_id"])
    assert not (tmp_path / "a.py").exists() and not (tmp_path / "b.py").exists()
    undo = rollback(str(tmp_path), to_commit=report.pre_rollback_commit[:12])
    assert {p: (tmp_path / p).read_text() for p in ("a.py", "b.py")} == before
    assert undo.rollback_commit


def test_mission_state_travels_with_the_tree_and_loro_rebootstraps(tmp_path):
    pm, mission = _mission(tmp_path)
    eff, turns = _three_steps(
        tmp_path
    )  # snapshots include .agent/mission.json (status paused, 0 goals)
    # a later save changes the mission: the run's LAST snapshot did not see it
    mission.status = "active"
    pm.save_mission(mission)
    assert pm.load_mission().status == "active"
    report = rollback(str(tmp_path), to=turns[0]["turn_id"])
    assert "ACTIVE" in " ".join(report.warnings)  # crashed-run warning, not a refusal
    fresh = PersistenceManager(str(tmp_path))
    loaded = fresh.load_mission()
    assert loaded.status == "paused", "mission.json restored with the tree"
    # a save after the rollback must not choke on the stale Loro sidecar
    loaded.status = "active"
    assert fresh.save_mission(loaded) is not False
    assert PersistenceManager(str(tmp_path)).load_mission().status == "active"


def test_keep_mission_state_restores_code_only(tmp_path):
    pm, mission = _mission(tmp_path)
    eff, turns = _three_steps(tmp_path)
    mission.status = "active"
    pm.save_mission(mission)
    rollback(str(tmp_path), to=turns[0]["turn_id"], keep_mission_state=True)
    assert not (tmp_path / "a.py").exists()
    assert PersistenceManager(str(tmp_path)).load_mission().status == "active"


def test_refused_while_a_mission_process_holds_the_lock(tmp_path):
    _mission(tmp_path)
    eff, turns = _three_steps(tmp_path)
    with cli_lock(str(tmp_path)):
        with pytest.raises(HistoryLocked):
            rollback(str(tmp_path), to=turns[0]["turn_id"])
    assert (tmp_path / "a.py").exists()


def test_targets_are_validated(tmp_path):
    _mission(tmp_path)
    eff, turns = _three_steps(tmp_path)
    agent_dir = str(tmp_path / ".agent")
    with pytest.raises(ValueError):
        resolve_target(agent_dir, None, None)
    with pytest.raises(ValueError):
        resolve_target(agent_dir, "nope", None)
    with pytest.raises(ValueError):
        resolve_target(agent_dir, None, "zzzz")
    seq = turns[0]["_history"]["seq"]  # bare seq: resolved in the latest run
    sha, turn_id = resolve_target(agent_dir, str(seq), None)
    assert sha == turns[0]["tree_before"] and turn_id == turns[0]["turn_id"]


def test_a_turn_from_a_snapshot_less_run_cannot_be_a_target(tmp_path):
    _mission(tmp_path)
    eff = LocalEffects(str(tmp_path), history_snapshot=False)
    with step_context("m1", 0, "f", "s"):
        _run(eff.emit_trace(InferenceCall(mission_id="m1")))
    _run(eff.history_close())
    with pytest.raises(ValueError, match="no tree"):
        rollback(str(tmp_path), to="1")


def test_the_cli_reports_and_refuses_cleanly(tmp_path, capsys):
    import argparse

    from agent.history.cli import cmd_history

    _mission(tmp_path)
    eff, turns = _three_steps(tmp_path)
    ns = argparse.Namespace(
        history_command="rollback",
        working_dir=str(tmp_path),
        to=turns[1]["turn_id"],
        to_commit=None,
        dry_run=True,
        keep_mission_state=False,
    )
    cmd_history(ns)
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "- b.py" in out
    ns.to = "does-not-exist"
    with pytest.raises(SystemExit):
        cmd_history(ns)
    assert "Error" in capsys.readouterr().out
