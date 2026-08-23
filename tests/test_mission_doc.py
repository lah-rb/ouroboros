"""Mission state on Loro: round-trip fidelity, op semantics, convergence.

The gate for the CRDT engine bet (2026-08-15 plan): if two forked docs
exchanging updates don't converge to counter=sum / logs=union / valid LWW
fields, the engine goes back behind a pure-Python apply. Everything here is
also the contract the pilot call-site migration relies on.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.persistence.manager import PersistenceManager
from agent.persistence.mission_doc import LoroMissionDoc
from agent.persistence.models import (
    AspectSpec,
    CounterIncOp,
    FieldSetOp,
    GoalRecord,
    GoalStatusOp,
    MissionConfig,
    MissionState,
    NoteAppendOp,
    NoteRecord,
    ResearchPlanState,
)

LIVE_MISSION = os.path.expanduser("~/corpora/ouroboros-spectra/.agent/mission.json")


def _mission(tmp_path) -> MissionState:
    return MissionState(
        objective="test the doc",
        config=MissionConfig(working_directory=str(tmp_path)),
        principles=["p1"],
        goals=[
            GoalRecord(id="g1", description="first", status="incomplete"),
            GoalRecord(id="g2", description="second", status="complete"),
        ],
        notes=[NoteRecord(content="n1")],
        research_plan=ResearchPlanState(aspects=[AspectSpec(name="gb")]),
        cycles_consumed=7,
    )


# ── round-trip fidelity ───────────────────────────────────────────────


def test_bootstrap_round_trip(tmp_path):
    state = _mission(tmp_path)
    doc = LoroMissionDoc.bootstrap(state)
    view = doc.to_mission_state()
    assert view.model_dump(mode="json") == state.model_dump(mode="json")


def test_replace_from_is_delta_shaped(tmp_path):
    """An unchanged save emits no new ops (oplog stays lean under the ~70
    legacy save_mission sites)."""
    state = _mission(tmp_path)
    doc = LoroMissionDoc.bootstrap(state)
    before = doc._doc.len_ops
    doc.replace_from(state)
    assert doc._doc.len_ops == before


@pytest.mark.skipif(
    not os.path.isfile(LIVE_MISSION), reason="live spectra mission not present"
)
def test_round_trip_live_mission():
    """The migration gate: the real 5.5k-record mission survives
    bootstrap → view byte-for-byte (as dicts)."""
    with open(LIVE_MISSION, encoding="utf-8") as f:
        data = json.load(f)
    state = MissionState.model_validate(data)
    view = LoroMissionDoc.bootstrap(state).to_mission_state()
    assert view.model_dump(mode="json") == state.model_dump(mode="json")


# ── op semantics ──────────────────────────────────────────────────────


def test_ops_apply_on_doc(tmp_path):
    doc = LoroMissionDoc.bootstrap(_mission(tmp_path))
    doc.apply(
        [
            CounterIncOp(field="cycles_consumed", n=3),
            NoteAppendOp(entry=NoteRecord(content="n2").model_dump()),
            GoalStatusOp(goal_id="g1", status="complete"),
            FieldSetOp(key="pending_directive", value="next"),
        ]
    )
    view = doc.to_mission_state()
    assert view.cycles_consumed == 10
    assert [n.content for n in view.notes] == ["n1", "n2"]
    assert view.goals[0].status == "complete"
    assert view.goals[1].status == "complete"
    assert view.pending_directive == "next"


def test_unknown_goal_and_bad_counter_are_skipped(tmp_path):
    doc = LoroMissionDoc.bootstrap(_mission(tmp_path))
    doc.apply(
        [
            GoalStatusOp(goal_id="nope", status="complete"),
            CounterIncOp(field="objective", n=1),  # not a counter
        ]
    )
    view = doc.to_mission_state()
    assert view.goals[0].status == "incomplete"
    assert view.objective == "test the doc"


# ── convergence (the point of the engine) ─────────────────────────────


def test_forked_docs_converge(tmp_path):
    a = LoroMissionDoc.bootstrap(_mission(tmp_path))
    b = LoroMissionDoc.from_snapshot(a.export_snapshot())
    va, vb = a.version, b.version

    a.apply(
        [
            CounterIncOp(field="cycles_consumed", n=5),
            NoteAppendOp(entry=NoteRecord(content="from-a").model_dump()),
            GoalStatusOp(goal_id="g1", status="complete"),
        ]
    )
    b.apply(
        [
            CounterIncOp(field="cycles_consumed", n=2),
            NoteAppendOp(entry=NoteRecord(content="from-b").model_dump()),
        ]
    )

    b.import_updates(a.export_updates(since=vb))
    a.import_updates(b.export_updates(since=va))

    sa, sb = a.to_mission_state(), b.to_mission_state()
    assert sa.model_dump(mode="json") == sb.model_dump(mode="json")
    assert sa.cycles_consumed == 7 + 5 + 2
    assert {n.content for n in sa.notes} == {"n1", "from-a", "from-b"}
    assert sa.goals[0].status == "complete"
    assert a.peer_id != b.peer_id


def test_concurrent_same_field_is_deterministic_lww(tmp_path):
    """Concurrent writes to ONE field don't merge — they pick one winner,
    identically on both replicas. Corruption-free, ownership still matters."""
    a = LoroMissionDoc.bootstrap(_mission(tmp_path))
    b = LoroMissionDoc.from_snapshot(a.export_snapshot())
    va, vb = a.version, b.version
    a.apply([FieldSetOp(key="pending_directive", value="from-a")])
    b.apply([FieldSetOp(key="pending_directive", value="from-b")])
    b.import_updates(a.export_updates(since=vb))
    a.import_updates(b.export_updates(since=va))
    sa, sb = a.to_mission_state(), b.to_mission_state()
    assert sa.pending_directive == sb.pending_directive
    assert sa.pending_directive in ("from-a", "from-b")


# ── manager integration ───────────────────────────────────────────────


def _manager(tmp_path) -> PersistenceManager:
    pm = PersistenceManager(str(tmp_path))
    pm.init_agent_dir()
    return pm


def test_manager_apply_ops_end_to_end(tmp_path):
    pm = _manager(tmp_path)
    pm.save_mission(_mission(tmp_path))
    out = pm.apply_ops([CounterIncOp(field="cycles_consumed", n=1)])
    assert out.cycles_consumed == 8
    # Durable in the view AND the journal exists beside it.
    assert pm.load_mission().cycles_consumed == 8
    assert os.path.isfile(tmp_path / ".agent" / "mission.loro")
    # A fresh manager (new process) reads the same state.
    assert PersistenceManager(str(tmp_path)).load_mission().cycles_consumed == 8


def test_external_writer_resyncs_journal(tmp_path):
    """ouroboros.py pause/message writes mission.json from another process;
    the journal must re-bootstrap from the view, not clobber it."""
    pm = _manager(tmp_path)
    pm.save_mission(_mission(tmp_path))
    # External writer: a second manager (fresh process) mutates the view.
    other = PersistenceManager(str(tmp_path))
    st = other.load_mission()
    st.pending_directive = "external"
    other.save_mission(st)
    # Original manager applies ops — the external edit must survive.
    out = pm.apply_ops([CounterIncOp(field="cycles_consumed", n=1)])
    assert out.pending_directive == "external"
    assert out.cycles_consumed == 8


def test_corrupt_journal_falls_back_to_view(tmp_path):
    pm = _manager(tmp_path)
    pm.save_mission(_mission(tmp_path))
    (tmp_path / ".agent" / "mission.loro").write_bytes(b"garbage")
    out = pm.apply_ops([CounterIncOp(field="cycles_consumed", n=2)])
    assert out.cycles_consumed == 9
    assert pm.load_mission().cycles_consumed == 9


def test_direct_interpreter_parity(tmp_path):
    """The engine-down fallback must agree with the doc semantics."""
    state = _mission(tmp_path)
    PersistenceManager._apply_ops_direct(
        state,
        [
            CounterIncOp(field="cycles_consumed", n=3),
            NoteAppendOp(entry=NoteRecord(content="n2").model_dump()),
            GoalStatusOp(goal_id="g1", status="complete"),
        ],
    )
    assert state.cycles_consumed == 10
    assert [n.content for n in state.notes] == ["n1", "n2"]
    assert state.goals[0].status == "complete"
