"""The history store: parquet rows in, the old trace dict shape out.

The JSONL trace is retired; this is the only record a mission leaves. These
tests pin what the rest of the system relies on: every trace event survives
the round trip unchanged, a turn is durable the moment it is ingested,
compaction never loses or duplicates a row from the reader's point of view,
one process holds the lock, and the ledger summary computed live equals the
one recomputed from what was written.
"""

from __future__ import annotations

import asyncio
import glob
import os
import shutil

import pytest

from agent.history import reader
from agent.history.store import HistoryLocked, HistoryStore, cli_lock, is_locked
from agent.trace import (
    CapacitySample,
    CommandRun,
    CycleEnd,
    CycleStart,
    FlowInvoke,
    FlowReturn,
    HealthSample,
    InferenceCall,
    McpToolCall,
    NotePushed,
    SessionEnd,
    SessionSnapshot,
    SessionStart,
    StepEnd,
    StepStart,
    finalize_ledger,
    fold_event,
    new_ledger,
    summarize_events,
)

# One loop for the whole module: the store's asyncio.Lock binds to the loop
# it first waits on, so every coroutine here must run on the same one.
_LOOP = asyncio.new_event_loop()


def _run(coro):
    return _LOOP.run_until_complete(coro)


@pytest.fixture
def ws(tmp_path):
    (tmp_path / ".agent").mkdir()
    return str(tmp_path)


def _agent(ws: str) -> str:
    return os.path.join(ws, ".agent")


def _every_event() -> list:
    return [
        CycleStart(
            mission_id="m1",
            cycle=0,
            flow="mission_control",
            entry_inputs=["mission_id"],
        ),
        StepStart(
            mission_id="m1",
            cycle=0,
            flow="mission_control",
            step="load_state",
            action_type="action",
            action="load_mission",
            context_consumed=["a", "b"],
            context_required=["a"],
            input_build_ms=1.5,
        ),
        InferenceCall(
            mission_id="m1",
            cycle=0,
            flow="design_and_plan",
            step="draft",
            tokens_in=10,
            tokens_out=20,
            wall_ms=123.4,
            temperature=0.7,
            max_tokens=4096,
            purpose="step_inference",
            thinking_content="let me think",
            prompt_content="the FULL prompt",
            response_content="the FULL response",
            truncated=False,
            prompt_render_ms=2.0,
            injection_ms=0.5,
            pre_compute_ms=0.25,
            cached_prefix_tokens=100,
            fresh_prefill_tokens=50,
            generated_tokens=20,
            reasoning_tokens=5,
            cache_hit=True,
            flow_key="design_and_plan",
            prefill_ms=30.0,
            decode_ms=80.0,
            reasoning="high",
        ),
        StepEnd(
            mission_id="m1",
            cycle=0,
            flow="mission_control",
            step="load_state",
            published=["mission"],
            resolver_type="rules",
            resolver_decision="dispatch",
            options_available=["dispatch", "done"],
            step_duration_ms=40.0,
            resolver_ms=0.3,
            observations_preview="loaded",
        ),
        FlowInvoke(
            mission_id="m1",
            cycle=0,
            flow="interact",
            step="run",
            child_flow="run_session",
            child_inputs=["x"],
        ),
        FlowReturn(
            mission_id="m1",
            cycle=0,
            flow="interact",
            child_flow="run_session",
            return_status="success",
            child_duration_ms=9.0,
        ),
        SessionStart(
            mission_id="m1",
            cycle=0,
            flow="diagnose_issue",
            step="open",
            session_id="abc123",
            config={"temperature": 0.7},
            from_snapshot="",
        ),
        SessionSnapshot(
            mission_id="m1",
            cycle=0,
            flow="diagnose_issue",
            step="pin",
            session_id="abc123",
            key="k",
            tokens=1200,
            resident=True,
        ),
        SessionEnd(
            mission_id="m1",
            cycle=0,
            flow="diagnose_issue",
            step="close",
            session_id="abc123",
            success=True,
            wall_ms=3.0,
            span_ms=500.0,
        ),
        CommandRun(
            mission_id="m1",
            cycle=0,
            flow="interact",
            step="run",
            command="python main.py",
            return_code=0,
            timed_out=False,
            stdout_preview="hi",
            stderr_preview="",
            wall_ms=12.0,
        ),
        McpToolCall(
            mission_id="m1",
            cycle=0,
            flow="interact",
            step="send",
            server="terminal",
            tool="send_input",
            arg_keys=["session_id", "text"],
            error="",
            result_preview="> ",
            wall_ms=5.0,
        ),
        NotePushed(
            mission_id="m1",
            cycle=0,
            flow="interact",
            step="note",
            category="learning",
            tags=["a"],
            source_flow="interact",
            content_preview="seen",
            success=True,
        ),
        HealthSample(
            mission_id="m1",
            cycle=0,
            flow="interact",
            health={"flowHits": 3, "sessionStrategy": "resident"},
        ),
        CapacitySample(
            mission_id="m1",
            cycle=-1,
            seats_total=4,
            seats_free=2,
            seq=7,
            lanes={"a": "1d/0i/0f"},
            last_refusal="",
        ),
        CycleEnd(
            mission_id="m1",
            cycle=0,
            flow="mission_control",
            outcome="tail_call",
            target_flow="design_and_plan",
            status=None,
            cycle_duration_ms=200.0,
            projection_ms=1.0,
            tail_resolution_ms=0.5,
        ),
    ]


def _strip(d: dict) -> dict:
    d = dict(d)
    d.pop("_history", None)
    return d


# ── round trip ────────────────────────────────────────────────────────


def test_every_event_type_round_trips_unchanged(ws):
    store = HistoryStore(ws, "m1", "full")
    events = _every_event()
    for e in events:
        store.ingest(e)
    _run(store.flush("test"))
    got = reader.load_events(_agent(ws))
    assert len(got) == len(events)
    for e, g in zip(events, got):
        original = e.to_dict()
        back = _strip(g)
        # The turn gains store columns the trace never had; everything the
        # trace had must come back byte-for-byte.
        for k, v in original.items():
            assert back.get(k) == v, (e.event_type, k, back.get(k), v)
        if e.event_type != "inference_call":
            assert set(back) == set(original), (e.event_type, set(back) ^ set(original))
    _run(store.close())


def test_seq_orders_events_and_turns_together(ws):
    store = HistoryStore(ws, "m1", "full")
    for e in _every_event():
        store.ingest(e)
    _run(store.close())
    got = reader.load_events(_agent(ws))
    seqs = [g["_history"]["seq"] for g in got]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert [g["event_type"] for g in got][:3] == [
        "cycle_start",
        "step_start",
        "inference_call",
    ]


def test_capacity_sample_keeps_its_own_seq(ws):
    """CapacitySample has a ``seq`` field of its own (the lane-report tick);
    the store's sequence must not overwrite it."""
    store = HistoryStore(ws, "m1", "full")
    store.ingest(CycleStart(mission_id="m1"))
    store.ingest(CapacitySample(mission_id="m1", cycle=-1, seq=7))
    _run(store.close())
    cap = [
        e
        for e in reader.load_events(_agent(ws))
        if e["event_type"] == "capacity_sample"
    ][0]
    assert cap["seq"] == 7
    assert cap["_history"]["seq"] == 2


# ── modes ─────────────────────────────────────────────────────────────


def test_metrics_mode_drops_content_but_keeps_every_metric(ws):
    store = HistoryStore(ws, "m1", "metrics")
    call = _every_event()[2]
    store.ingest(call)
    _run(store.close())
    (turn,) = reader.load_turns(_agent(ws))
    for c in ("prompt_content", "response_content", "thinking_content"):
        assert c not in turn
    assert turn["content_dropped"] is True
    assert turn["generated_tokens"] == 20 and turn["wall_ms"] == 123.4


def test_off_is_not_a_store_mode():
    with pytest.raises(ValueError):
        HistoryStore("/tmp/nowhere", "m1", "off", lock=False)


# ── durability ────────────────────────────────────────────────────────


def test_a_turn_is_written_the_moment_it_is_recorded(ws):
    store = HistoryStore(ws, "m1", "full")
    call = _every_event()[2]
    _run(store.record(call))
    _run(store.record(call))
    parts = glob.glob(
        os.path.join(_agent(ws), "history", "turns", "run=*", "part-*.parquet")
    )
    assert len(parts) == 2, "one part per turn, before any step boundary"
    # Nothing else was flushed yet (no events recorded).
    assert reader.load_turns(_agent(ws)).__len__() == 2
    _run(store.close())


def test_a_step_end_flushes_the_events_buffer(ws):
    store = HistoryStore(ws, "m1", "full")
    store.ingest(CycleStart(mission_id="m1"))
    store.ingest(StepStart(mission_id="m1", step="s"))
    assert reader.load_events(_agent(ws)) == []
    _run(store.record(StepEnd(mission_id="m1", step="s")))
    assert [e["event_type"] for e in reader.load_events(_agent(ws))] == [
        "cycle_start",
        "step_start",
        "step_end",
    ]
    _run(store.close())


# ── compaction ────────────────────────────────────────────────────────


def test_cycle_compaction_leaves_one_file_and_the_same_rows(ws):
    store = HistoryStore(ws, "m1", "full")
    for e in _every_event():
        _run(store.record(e))
    before = reader.load_events(_agent(ws))
    run_dir = os.path.join(_agent(ws), "history", "events", "run=*")
    assert len(glob.glob(os.path.join(run_dir, "part-*.parquet"))) >= 2
    _run(store.compact("cycle", cycle=0))
    files = glob.glob(os.path.join(run_dir, "*.parquet"))
    assert len(files) == 1 and os.path.basename(files[0]).startswith("cycle-000000-")
    assert [_strip(e) for e in reader.load_events(_agent(ws))] == [
        _strip(e) for e in before
    ]
    _run(store.close())
    files = glob.glob(os.path.join(run_dir, "*.parquet"))
    assert len(files) == 1 and os.path.basename(files[0]).startswith(
        "cycle-"
    ), "one file stays one file"


def test_close_folds_the_run_into_one_file(ws):
    store = HistoryStore(ws, "m1", "full")
    for e in _every_event():
        _run(store.record(e))
    _run(store.compact("cycle", cycle=0))
    for e in _every_event():
        _run(store.record(e))
    _run(store.close())
    for table in ("turns", "events"):
        files = glob.glob(
            os.path.join(_agent(ws), "history", table, "run=*", "*.parquet")
        )
        assert len(files) == 1 and os.path.basename(files[0]).startswith("run-"), (
            table,
            files,
        )
    assert len(reader.load_events(_agent(ws))) == 2 * len(_every_event())


def test_a_crash_between_rename_and_unlink_is_deduped(ws):
    """Compaction renames the merged file in, then unlinks its inputs. If
    the process dies in between, the rows exist twice on disk — the reader
    must return them once."""
    store = HistoryStore(ws, "m1", "full")
    for e in _every_event():
        _run(store.record(e))
    run_dir = glob.glob(os.path.join(_agent(ws), "history", "events", "run=*"))[0]
    parts = sorted(glob.glob(os.path.join(run_dir, "part-*.parquet")))
    stash = os.path.join(ws, "stash")
    os.makedirs(stash)
    for p in parts:
        shutil.copy(p, stash)
    n = len(reader.load_events(_agent(ws)))
    _run(store.compact("cycle", cycle=0))
    for p in glob.glob(os.path.join(stash, "*.parquet")):  # the crash: inputs survive
        shutil.copy(p, run_dir)
    assert len(glob.glob(os.path.join(run_dir, "*.parquet"))) > 1
    assert len(reader.load_events(_agent(ws))) == n
    _run(store.close())


# ── lock ──────────────────────────────────────────────────────────────


def test_one_process_holds_the_lock(ws):
    store = HistoryStore(ws, "m1", "full")
    assert is_locked(ws)
    with pytest.raises(HistoryLocked):
        HistoryStore(ws, "m1", "full")
    with pytest.raises(HistoryLocked):
        with cli_lock(ws):
            pass
    _run(store.close())
    assert not is_locked(ws)
    with cli_lock(ws):
        assert is_locked(ws)


# ── summary ───────────────────────────────────────────────────────────


def test_live_ledger_equals_the_recomputed_summary(ws):
    store = HistoryStore(ws, "m1", "full")
    ledger = new_ledger()
    for e in _every_event():
        fold_event(ledger, store.ingest(e))
    live = finalize_ledger(ledger, total_wall_ms=1000.0)
    _run(store.close(final_status="completed", summary=live))
    head = reader.load_summary(_agent(ws))
    assert head["summary"] == live
    recomputed = summarize_events(reader.load_events(_agent(ws)), total_wall_ms=1000.0)
    assert recomputed["counts"] == live["counts"]
    assert recomputed["tokens"] == live["tokens"]
    assert recomputed["time_ms"] == live["time_ms"]


def test_runs_row_exists_from_the_first_moment(ws):
    store = HistoryStore(
        ws, "m1", "full", meta={"endpoint": "http://x", "flow_set": "code_core"}
    )
    runs = reader.list_runs(_agent(ws))
    assert [r["run_id"] for r in runs] == [store.run_id]
    assert runs[0]["final_status"] == "" and runs[0]["ended_at"] is None
    assert runs[0]["endpoint"] == "http://x"
    _run(store.close(final_status="paused"))
    (run,) = reader.list_runs(_agent(ws))
    assert run["final_status"] == "paused" and run["ended_at"]


# ── tree_after ────────────────────────────────────────────────────────


def test_tree_after_is_the_first_commit_after_the_turn(ws):
    store = HistoryStore(ws, "m1", "full")
    call = _every_event()[2]
    store.ingest(call)  # seq 1
    store.add_commit(
        {"commit_sha": "c2", "parent_sha": "", "tree_sha": "t2", "source": "step_end"}
    )  # seq 2
    store.ingest(call)  # seq 3
    store.add_commit(
        {"commit_sha": "c4", "parent_sha": "c2", "tree_sha": "t4", "source": "step_end"}
    )  # seq 4
    store.ingest(call)  # seq 5 — nothing after it
    _run(store.close())
    turns = reader.load_turns(_agent(ws))
    assert [t["tree_after"] for t in turns] == ["c2", "c4", ""]
    assert reader.get_turn(_agent(ws), "3")["_history"]["seq"] == 3
    assert reader.get_turn(_agent(ws), f"{store.run_id}:5")["tree_after"] == ""
    assert reader.get_turn(_agent(ws), turns[0]["turn_id"]) is not None
    assert reader.load_commits(_agent(ws))[0]["commit_sha"] == "c2"


def test_token_totals_sum_every_turn(ws):
    store = HistoryStore(ws, "m1", "full")
    call = _every_event()[2]
    store.ingest(call)
    store.ingest(call)
    _run(store.close())
    assert reader.token_totals(_agent(ws)) == (20, 40)


def test_close_finishes_even_if_the_awaiting_task_is_cancelled_at_once(ws):
    """The teardown drain can be cancelled at any await. close() has none
    between its steps, so a cancel that lands right after it starts finds
    the work already done: parts flushed, run compacted, runs row closed."""
    store = HistoryStore(ws, "m1", "full")
    for e in _every_event():
        store.ingest(e)
    _run(store.flush("mid"))
    for e in _every_event():
        store.ingest(e)  # left in the buffer for close to flush

    async def scenario():
        task = asyncio.ensure_future(store.close("completed", {"total_wall_ms": 1.0}))
        await asyncio.sleep(0)  # let close() run to its first await — it has none
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    _run(scenario())
    (run,) = reader.list_runs(_agent(ws))
    assert run["final_status"] == "completed" and run["turns"] == 2
    assert len(reader.load_events(_agent(ws))) == 2 * len(_every_event())
    for table in ("turns", "events"):
        files = glob.glob(
            os.path.join(_agent(ws), "history", table, "run=*", "*.parquet")
        )
        assert len(files) == 1 and os.path.basename(files[0]).startswith("run-"), (
            table,
            files,
        )
    assert not is_locked(ws)
