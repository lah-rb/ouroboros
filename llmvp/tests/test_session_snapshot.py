"""Semi-permanent session snapshots: backend seq ops + manager lifecycle.

The curator's ingest-once tier: session_snapshot pins a session's KV
under a key (surviving session end/TTL), start_session(from_snapshot=)
forks from it, purge_snapshot frees it. Backend tests drive the REAL
seq-op methods against a recording fake context; manager tests pin the
lifecycle wiring (turn_count seeding, replay fallback, windowing
forbid, refresh demotion to cold).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from types import SimpleNamespace

import numpy as np
import pytest

import core.session_manager as sm
from core.session_manager import (
    SessionManager,
    SessionSnapshotOverflow,
    SessionState,
)
from inference.backends.llama_cpp_backend import (
    SEQ_FLOW_BASE,
    SEQ_WORKING,
    SNAP_GEN_RESERVE,
    LlamaCppBackend,
)

# ── backend-level: real seq-op methods, recording fake context ────────


class RecordingCtx:
    def __init__(self):
        self.ops: list[tuple] = []

    def memory_seq_rm(self, seq, p0, p1):
        self.ops.append(("rm", seq, p0, p1))

    def memory_seq_cp(self, src, dst, p0, p1):
        self.ops.append(("cp", src, dst, p0, p1))

    def memory_seq_add(self, seq, p0, p1, delta):
        self.ops.append(("add", seq, p0, p1, delta))


def _make_backend(snapshot_max=2, flow_band=False) -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=1,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        model=SimpleNamespace(
            resident_seq_cache=True,
            session_snapshot_max=snapshot_max,
            flow_kv_cache=flow_band,
            flow_kv_cache_max=4,
            resident_session_flow_fork=False,
        ),
    )
    backend = LlamaCppBackend(config)
    backend._resident_active = True
    backend._resident_static_len = 3
    backend._resident_static_tokens = [11, 12, 13]
    return backend


def _make_inst(n_tokens=8, n_ctx=SNAP_GEN_RESERVE + 2000):
    inst = SimpleNamespace(
        _ctx=RecordingCtx(),
        input_ids=np.zeros(n_ctx, dtype=np.intc),
        n_tokens=n_tokens,
        _n_ctx=n_ctx,
        _snap_seqs=OrderedDict(),
        _flow_seqs=OrderedDict(),
    )
    seed = [11, 12, 13, 40, 41, 42, 43, 44][:n_tokens]
    inst.input_ids[: len(seed)] = np.array(seed, dtype=np.intc)
    inst.eval_calls = []
    inst.eval = lambda toks: (
        inst.eval_calls.append(list(toks)),
        setattr(inst, "n_tokens", inst.n_tokens + len(toks)),
    )
    return inst


def test_snapshot_capture_pins_seq_and_records_tokens():
    backend = _make_backend()
    inst = _make_inst()
    info = backend.snapshot_working_seq(inst, "paper:x")
    assert info == {"tokens": 8, "resident": True}
    # Band starts right after SEQ_STATIC when no flow band is allocated.
    assert inst._snap_seqs["paper:x"] == SEQ_FLOW_BASE
    assert ("cp", SEQ_WORKING, SEQ_FLOW_BASE, -1, -1) in inst._ctx.ops
    entry = backend._snap_registry["paper:x"]
    assert entry["dyn_tokens"] == [40, 41, 42, 43, 44]  # post-static stream
    assert entry["static_len"] == 3


def test_snapshot_band_sits_above_flow_band():
    backend = _make_backend(flow_band=True)
    inst = _make_inst()
    backend.snapshot_working_seq(inst, "k")
    assert inst._snap_seqs["k"] == SEQ_FLOW_BASE + 4  # flow_kv_cache_max=4


def test_snapshot_capacity_and_duplicate_rejected():
    backend = _make_backend(snapshot_max=1)
    inst = _make_inst()
    backend._all_instances = [inst]  # purge sweeps registered instances
    backend.snapshot_working_seq(inst, "a")
    with pytest.raises(RuntimeError, match="already exists"):
        backend.snapshot_working_seq(inst, "a")
    with pytest.raises(RuntimeError, match="capacity"):
        backend.snapshot_working_seq(inst, "b")
    # Purge frees the slot for reuse.
    assert backend.purge_snapshot("a") is True
    backend.snapshot_working_seq(inst, "b")


def test_snapshot_context_budget_rejected():
    backend = _make_backend()
    inst = _make_inst()
    backend.snapshot_working_seq(inst, "a")
    # Second capture would push pinned + candidate + reserve past n_ctx.
    inst2 = _make_inst(n_tokens=1995)
    inst2._snap_seqs = inst._snap_seqs
    with pytest.raises(RuntimeError, match="context budget"):
        backend.snapshot_working_seq(inst2, "b")


def test_fork_hot_restores_stream():
    backend = _make_backend()
    inst = _make_inst()
    backend.snapshot_working_seq(inst, "k")
    inst.n_tokens = 3  # simulate a later acquire (static only)
    got = backend.fork_snapshot_seq(inst, "k")
    assert got == 8
    assert inst.n_tokens == 8
    assert list(inst.input_ids[:8]) == [11, 12, 13, 40, 41, 42, 43, 44]
    ops = inst._ctx.ops
    assert ("rm", SEQ_WORKING, 0, -1) in ops
    assert ("cp", SEQ_FLOW_BASE, SEQ_WORKING, -1, -1) in ops


def test_fork_cold_miss_returns_none_then_rebuild_reprefills_and_repins():
    backend = _make_backend()
    inst = _make_inst()
    backend.snapshot_working_seq(inst, "k")

    fresh = _make_inst(n_tokens=0)  # refreshed context: no hot pins
    assert backend.fork_snapshot_seq(fresh, "k") is None
    n = backend.rebuild_snapshot_cold(fresh, "k")
    assert n == 8
    assert fresh.eval_calls == [[40, 41, 42, 43, 44]]  # dynamic tail only
    assert fresh._snap_seqs["k"] == SEQ_FLOW_BASE  # re-pinned hot
    assert backend._h_snapshot_rebuilds == 1


def test_fork_unknown_key_raises():
    backend = _make_backend()
    with pytest.raises(KeyError):
        backend.fork_snapshot_seq(_make_inst(), "nope")


def test_replay_registration_is_cold_only():
    backend = _make_backend()
    backend._resident_active = False
    info = backend.register_replay_snapshot("r", [7, 8, 9])
    assert info == {"tokens": 3, "resident": False}
    with pytest.raises(RuntimeError, match="resident cache inactive"):
        backend.snapshot_working_seq(_make_inst(), "x")
    listing = backend.list_snapshots()
    assert listing[0]["key"] == "r" and listing[0]["resident"] is False


# ── manager-level: lifecycle wiring against a fake backend ────────────


class FakeSnapBackend:
    """Implements exactly the snapshot surface the manager touches."""

    def __init__(self, resident=True):
        self._resident_active = resident
        self._snap_registry: dict = {}
        self.fork_calls: list = []
        self.rebuild_calls: list = []
        self.purged: list = []

    async def acquire_instance(self):
        return SimpleNamespace(n_tokens=3, save_state=lambda: "S0")

    async def release_instance(self, inst):
        pass

    def snapshot_working_seq(self, inst, key):
        self._snap_registry[key] = {
            "dyn_tokens": [40, 41],
            "static_len": 3,
            "turn_count": 0,
            "created_at": 0.0,
            "resident": True,
        }
        return {"tokens": 5, "resident": True}

    def register_replay_snapshot(self, key, dyn):
        self._snap_registry[key] = {
            "dyn_tokens": list(dyn),
            "static_len": 0,
            "turn_count": 0,
            "created_at": 0.0,
            "resident": False,
        }
        return {"tokens": len(dyn), "resident": False}

    def fork_snapshot_seq(self, inst, key):
        self.fork_calls.append(key)
        return 5

    def rebuild_snapshot_cold(self, inst, key):
        self.rebuild_calls.append(key)
        return 5

    def purge_snapshot(self, key):
        self.purged.append(key)
        return self._snap_registry.pop(key, None) is not None

    def list_snapshots(self):
        return [{"key": k} for k in self._snap_registry]


def test_manager_snapshot_records_turn_count_and_links_session():
    backend = FakeSnapBackend()
    mgr = SessionManager(backend)
    sess = SessionState(instance=SimpleNamespace(), current_state=None, turn_count=2)
    mgr._sessions["s1"] = sess

    info = asyncio.run(mgr.session_snapshot("s1", "paper:x"))
    assert info == {"key": "paper:x", "tokens": 5, "resident": True, "turn_count": 2}
    assert backend._snap_registry["paper:x"]["turn_count"] == 2
    assert sess.snapshot_key == "paper:x"


def test_manager_fork_seeds_turn_count_resident():
    backend = FakeSnapBackend()
    backend._snap_registry["k"] = {
        "dyn_tokens": [40, 41],
        "static_len": 3,
        "turn_count": 3,
        "created_at": 0.0,
        "resident": True,
    }
    mgr = SessionManager(backend)
    info = asyncio.run(mgr.start_session(from_snapshot="k"))
    sess = mgr._sessions[info.session_id]
    assert sess.snapshot_forked is True
    assert sess.turn_count == 3  # first turn renders the turn transition
    assert backend.fork_calls == ["k"]
    asyncio.run(mgr.end_session(info.session_id))


def test_manager_fork_replay_seeds_token_history():
    backend = FakeSnapBackend(resident=False)
    backend._snap_registry["k"] = {
        "dyn_tokens": [7, 8, 9],
        "static_len": 0,
        "turn_count": 1,
        "created_at": 0.0,
        "resident": False,
    }
    mgr = SessionManager(backend)
    info = asyncio.run(mgr.start_session(from_snapshot="k"))
    sess = mgr._sessions[info.session_id]
    assert sess.token_history == [7, 8, 9]
    assert sess.turn_count == 1
    assert backend.fork_calls == []  # replay path never touches seq ops
    asyncio.run(mgr.end_session(info.session_id))


def test_manager_fork_unknown_key_raises_before_acquire():
    backend = FakeSnapBackend()
    backend.acquire_instance = None  # would explode if reached
    mgr = SessionManager(backend)
    with pytest.raises(KeyError):
        asyncio.run(mgr.start_session(from_snapshot="missing"))


def test_manager_purge_delegates():
    backend = FakeSnapBackend()
    backend._snap_registry["k"] = {"dyn_tokens": [], "static_len": 0}
    mgr = SessionManager(backend)
    assert asyncio.run(mgr.purge_snapshot("k")) is True
    assert backend.purged == ["k"]
    assert asyncio.run(mgr.purge_snapshot("k")) is False


def test_snapshot_overflow_is_a_runtime_error():
    # The windowing branch raises this instead of shifting shared cells;
    # subclassing RuntimeError keeps generic handlers working.
    assert issubclass(SessionSnapshotOverflow, RuntimeError)
    err = SessionSnapshotOverflow("session s1 is snapshot-linked")
    assert "snapshot-linked" in str(err)


def test_windowing_forbid_on_snapshot_linked_sessions(monkeypatch):
    """Drive session_turn to the windowing branch with a snapshot-linked
    session and a context too small for the turn — must raise, not window."""

    class _Renderer:
        def render_turn_transition_segments(self):
            return []

        def render_user_segments(self, prompt):
            return [(prompt, False)]

        def render_generation_prompt_segments(self):
            return []

        def stop_tokens(self, mode=None):
            return []

    class _Model:
        family = "chatml"
        session_full_replay = True
        resident_strip_reasoning = False

    monkeypatch.setattr(
        sm, "get_config", lambda: SimpleNamespace(model=_Model(), generation=None)
    )
    monkeypatch.setattr(sm, "_get_format_renderer", lambda family: _Renderer())
    monkeypatch.setattr(sm, "get_cached_tokenizer", lambda: object())
    monkeypatch.setattr(sm, "tokenize_segments", lambda tok, segs: [1] * 50)
    monkeypatch.setenv("LLMVP_THINK_STRIP", "0")

    windowed = []

    class _Backend:
        _resident_active = True
        _resident_static_len = 3
        _session_flow_fork = False

        def _window_resident_seq(self, inst, n_keep):
            windowed.append(n_keep)
            return 3

        def generate_stream_async(self, **kw):
            async def _gen():
                yield "ok"

            return _gen()

    inst = SimpleNamespace(n_tokens=90, _n_ctx=100)
    mgr = SessionManager(_Backend())
    sess = SessionState(instance=inst, current_state=None, snapshot_forked=True)
    mgr._sessions["s1"] = sess

    async def drive():
        async for _ in mgr.session_turn("s1", "hi", max_tokens=64):
            pass

    with pytest.raises(SessionSnapshotOverflow):
        asyncio.run(drive())
    assert windowed == [], "snapshot-linked session must never window"

    # Identical setup WITHOUT snapshot linkage windows normally.
    sess.snapshot_forked = False
    asyncio.run(drive())
    assert windowed == [3]


# ── refresh gating (the pre-existing live-session refresh bug) ────────


def test_timed_refresh_defers_while_session_pinned(monkeypatch):
    backend = _make_backend()
    backend._refresh_interval = 75
    backend._refresh_seconds = 100
    backend._last_refresh_monotonic = 0.0
    backend._h_requests_since_refresh = 10
    monkeypatch.setattr(
        "inference.backends.llama_cpp_backend.time",
        SimpleNamespace(monotonic=lambda: 500.0, time=lambda: 500.0),
    )
    # A pinned session (checked_out > 0) must DEFER the time-capped refresh —
    # rebuilding the context under it silently vanishes its prior turns.
    backend._checked_out = 1
    backend._active_generations = 0
    assert backend._refresh_decision() is None
    assert backend._h_refresh_deferred == 1
    # Session ends -> the deferred refresh fires.
    backend._checked_out = 0
    assert backend._refresh_decision() == "proactive-timed"


def test_opportunistic_refresh_requires_idle(monkeypatch):
    backend = _make_backend()
    backend._refresh_interval = 5
    backend._refresh_seconds = 10**9
    backend._last_refresh_monotonic = 0.0
    backend._h_requests_since_refresh = 6
    monkeypatch.setattr(
        "inference.backends.llama_cpp_backend.time",
        SimpleNamespace(monotonic=lambda: 1.0, time=lambda: 1.0),
    )
    backend._checked_out = 0
    backend._active_generations = 0
    assert backend._refresh_decision() == "proactive-interval"
    backend._checked_out = 1
    assert backend._refresh_decision() is None
