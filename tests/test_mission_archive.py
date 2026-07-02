"""Mission-history archive: primitives, the sweep, mining access.

Relocation-never-truncation: completed goals' reports/failed attempts
and rolling-list overflow move to append-only JSONL under
.agent/archive/ — the behavioral-mining substrate. These tests pin the
round-trip, the sweep's idempotency and counters, reopen behavior, and
old-mission load compatibility.
"""

from __future__ import annotations

import json
import threading

from agent.persistence.archive import (
    DISPATCH_CAP,
    NOTES_CAP,
    append_goal_records,
    append_overflow,
    archive_mission_overflow,
    iter_archive,
)
from agent.persistence.models import (
    DirectiveReport,
    DispatchRecord,
    FailedAttempt,
    GoalRecord,
    MissionConfig,
    MissionState,
    NoteRecord,
)


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="x",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=goals or [],
    )


def _report(i=0, status="success"):
    return DirectiveReport(flow="file_ops", status=status, summary=f"report {i}")


# ── primitives ────────────────────────────────────────────────────────


def test_goal_records_round_trip(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    n = append_goal_records(
        agent_dir,
        "g1",
        [_report(0), _report(1, "failed")],
        [
            FailedAttempt(
                target_file="a.py",
                flow="file_ops",
                reason="lint",
                diagnosis_summary="d",
            )
        ],
    )
    assert n == 3
    records = list(iter_archive(agent_dir, goal_id="g1"))
    assert [r["kind"] for r in records] == ["report", "report", "failed_attempt"]
    assert records[1]["status"] == "failed"
    assert records[2]["target_file"] == "a.py"
    assert all("archived_at" in r for r in records)
    # kind filter
    assert len(list(iter_archive(agent_dir, kind="failed_attempt"))) == 1


def test_overflow_and_global_iteration_order(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    append_goal_records(agent_dir, "g1", [_report(0)], [])
    append_overflow(
        agent_dir, "notes", "note", [NoteRecord(content="n0", source_flow="t")]
    )
    append_overflow(agent_dir, "dispatch", "dispatch", [DispatchRecord(cycle=1)])
    kinds = [r["kind"] for r in iter_archive(agent_dir)]
    assert kinds == ["report", "note", "dispatch"]  # goals/, then notes, dispatch


def test_concurrent_appends_produce_intact_lines(tmp_path):
    agent_dir = str(tmp_path / ".agent")

    def worker(tag):
        for i in range(50):
            append_goal_records(agent_dir, "g1", [_report(f"{tag}-{i}")], [])

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    records = list(iter_archive(agent_dir, goal_id="g1"))
    assert len(records) == 200  # every line intact, none interleaved/corrupt


# ── the sweep ─────────────────────────────────────────────────────────


def test_sweep_relocates_completed_goals_only(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    done = GoalRecord(description="done", status="complete")
    done.reports = [_report(0), _report(1)]
    done.failed_attempts = [
        FailedAttempt(
            target_file="a.py", flow="file_ops", reason="r", diagnosis_summary="d"
        )
    ]
    active = GoalRecord(description="active", status="incomplete")
    active.reports = [_report(9)]
    mission = _mission([done, active])

    assert archive_mission_overflow(agent_dir, mission) is True
    assert done.reports == [] and done.failed_attempts == []
    assert done.reports_archived == 2 and done.attempts_archived == 1
    assert active.reports and active.reports[0].summary == "report 9"  # untouched
    assert len(list(iter_archive(agent_dir, goal_id=done.id))) == 3

    # Idempotent: second sweep is a no-op.
    assert archive_mission_overflow(agent_dir, mission) is False
    assert len(list(iter_archive(agent_dir, goal_id=done.id))) == 3


def test_sweep_reopen_then_recomplete_appends_not_overwrites(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    goal = GoalRecord(description="g", status="complete")
    goal.reports = [_report(0)]
    mission = _mission([goal])
    archive_mission_overflow(agent_dir, mission)

    # Reopen (quality-fix loop), new work lands, completes again.
    goal.status = "incomplete"
    goal.reports = [_report(1)]
    goal.status = "complete"
    archive_mission_overflow(agent_dir, mission)

    records = list(iter_archive(agent_dir, goal_id=goal.id))
    assert [r["summary"] for r in records] == ["report 0", "report 1"]
    assert goal.reports_archived == 2


def test_sweep_caps_notes_and_dispatch_oldest_first(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    mission = _mission()
    for i in range(NOTES_CAP + 7):
        mission.notes.append(NoteRecord(content=f"note-{i:03d}", source_flow="t"))
    for i in range(DISPATCH_CAP + 3):
        mission.dispatch_history.append(DispatchRecord(cycle=i))

    assert archive_mission_overflow(agent_dir, mission) is True
    assert len(mission.notes) == NOTES_CAP
    assert mission.notes[0].content == "note-007"  # oldest 7 relocated
    assert mission.notes_archived == 7
    assert len(mission.dispatch_history) == DISPATCH_CAP
    assert mission.dispatch_archived == 3
    archived_notes = list(iter_archive(agent_dir, kind="note"))
    assert [r["content"] for r in archived_notes] == [f"note-{i:03d}" for i in range(7)]


def test_sweep_noop_under_caps(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    mission = _mission([GoalRecord(description="g", status="incomplete")])
    mission.notes.append(NoteRecord(content="n", source_flow="t"))
    assert archive_mission_overflow(agent_dir, mission) is False


# ── schema compatibility ──────────────────────────────────────────────


def test_pre_archive_mission_json_loads_with_defaults():
    raw = {
        "objective": "x",
        "status": "active",
        "config": {"working_directory": "/tmp/x"},
        "goals": [{"description": "old goal", "status": "complete"}],
    }
    m = MissionState.model_validate(raw)
    assert m.notes_archived == 0 and m.dispatch_archived == 0
    assert m.goals[0].reports_archived == 0
    assert m.goals[0].attempts_archived == 0


def test_archive_lines_are_plain_jq_able(tmp_path):
    agent_dir = str(tmp_path / ".agent")
    append_goal_records(agent_dir, "g1", [_report(0)], [])
    path = tmp_path / ".agent" / "archive" / "goals" / "g1.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])  # every line standalone-parseable
    assert rec["kind"] == "report" and rec["flow"] == "file_ops"


# ── wire-in: the sweep runs from attach + finalize ────────────────────


def test_attach_report_sweeps_completed_goals(tmp_path):
    import asyncio

    from agent.actions.reporting_actions import action_attach_directive_report
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    goal = GoalRecord(description="g", status="incomplete", type="structural")
    mission = _mission([goal])
    fx = MockEffects(mission=mission)

    si = StepInput(
        context={
            "mission": mission,
            "last_goal_id": goal.id,
            "last_status": "success",
            "last_result": {
                "directive_report": {
                    "flow": "file_ops",
                    "status": "success",
                    "summary": "done",
                }
            },
        },
        inputs={},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="apply_last_result"),
        effects=fx,
    )
    asyncio.run(action_attach_directive_report(si))
    # file_ops success + no block reason -> goal completes -> sweep
    # relocates the report in the SAME cycle's attach pass.
    assert goal.status == "complete"
    assert goal.reports == [] and goal.reports_archived == 1
    agent_dir = fx._get_persistence().agent_dir
    records = list(iter_archive(agent_dir, goal_id=goal.id))
    assert len(records) == 1 and records[0]["summary"] == "done"


def test_finalize_mission_sweeps_last_goal(tmp_path):
    import asyncio

    from agent.actions.mission_actions import action_finalize_mission
    from agent.effects.mock import MockEffects
    from agent.models import FlowMeta, StepInput

    goal = GoalRecord(description="g", status="complete")
    goal.reports = [_report(0)]
    mission = _mission([goal])
    fx = MockEffects(mission=mission)
    si = StepInput(
        context={"mission": mission},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="mission_control", step_id="completed"),
        effects=fx,
    )
    out = asyncio.run(action_finalize_mission(si))
    assert out.result["finalized"] is True
    assert goal.reports == [] and goal.reports_archived == 1
    agent_dir = fx._get_persistence().agent_dir
    assert len(list(iter_archive(agent_dir, goal_id=goal.id))) == 1


# ── load_mission parse cache (churn fix) ──────────────────────────────


def _pm(tmp_path):
    from agent.persistence.manager import PersistenceManager

    pm = PersistenceManager(str(tmp_path))
    pm.init_agent_dir()
    return pm


def test_load_mission_cache_serves_without_reparse(tmp_path, monkeypatch):
    import agent.persistence.manager as mgr

    pm = _pm(tmp_path)
    pm.save_mission(_mission())

    parses = {"n": 0}
    real = mgr.json.load

    def counting_load(*a, **k):
        parses["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(mgr.json, "load", counting_load)
    m1 = pm.load_mission()
    m2 = pm.load_mission()
    m3 = pm.load_mission()
    # save_mission primed the cache -> zero parses; same object served.
    assert parses["n"] == 0
    assert m1 is m2 is m3


def test_external_write_invalidates_cache(tmp_path):
    import json as _json
    import os as _os

    pm = _pm(tmp_path)
    pm.save_mission(_mission())
    m1 = pm.load_mission()

    # Simulate the ouroboros.py CLI writing mission.json externally.
    path = _os.path.join(pm.agent_dir, "mission.json")
    data = _json.loads(open(path).read())
    data["objective"] = "changed externally"
    with open(path, "w") as f:
        f.write(_json.dumps(data, indent=2))
    _os.utime(
        path, ns=(_os.stat(path).st_mtime_ns + 10, _os.stat(path).st_mtime_ns + 10)
    )

    m2 = pm.load_mission()
    assert m2 is not m1
    assert m2.objective == "changed externally"


def test_fresh_pm_parses_then_caches(tmp_path, monkeypatch):
    import agent.persistence.manager as mgr
    from agent.persistence.manager import PersistenceManager

    pm = _pm(tmp_path)
    pm.save_mission(_mission())

    pm2 = PersistenceManager(str(tmp_path))  # cold cache (CLI-style)
    parses = {"n": 0}
    real = mgr.json.load

    def counting_load(*a, **k):
        parses["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(mgr.json, "load", counting_load)
    pm2.load_mission()
    pm2.load_mission()
    assert parses["n"] == 1  # one cold parse, then cached
