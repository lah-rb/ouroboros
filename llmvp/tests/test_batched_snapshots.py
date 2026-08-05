"""Session snapshots under decode_mode: batched — the v1 port.

Was a hard raise ("pool-only in batched mode v1") — which meant the snapshot
tier was UNAVAILABLE in the production shape (corpus §5.11; Block E2 measured
the refusal clean, 2026-07-30). The port pins a snapshot band above the
reasoning heads in the seq map; capture and hot fork route their KV surgery
through the engine's control inbox (step-boundary, decode-thread — the
``SeqSlot.purge_to`` contract). Cold rebuild stays a CLEAN REFUSAL under
batched: the pool cold path replays via ``inst.eval``, which a seat does not
have, and a control-op replay would stall every live stream.
"""

from __future__ import annotations

from collections import OrderedDict
from types import SimpleNamespace

import pytest

from inference.batched_engine import plan_seq_map
from inference.backends.llama_cpp_backend import LlamaCppBackend

# ── the seq-map arithmetic (pure) ────────────────────────────────────────


class TestSeqMapBand:
    def test_band_sits_above_reasoning(self):
        m = plan_seq_map(4, ["default", "user_sim"], ["low", "high"], snapshots=2)
        # 4 working + 2 personas + 2 reasoning = 8; band = [8, 10)
        assert list(m.snap_seqs) == [8, 9]
        assert m.n_seq_max == 10

    def test_zero_band_is_the_old_layout(self):
        m = plan_seq_map(4, [], [], snapshots=0)
        assert len(m.snap_seqs) == 0
        assert m.n_seq_max == 5  # 4 working + default persona

    def test_negative_refused(self):
        with pytest.raises(ValueError):
            plan_seq_map(4, [], [], snapshots=-1)

    def test_production_shape(self):
        # gpt-oss swarm: 128 seats + default + 2 reasoning heads + 2 snapshots
        m = plan_seq_map(128, [], ["low", "high"], snapshots=2)
        assert m.n_seq_max == 133
        assert list(m.snap_seqs) == [131, 132]


# ── capture / fork / purge with a recording engine ──────────────────────


class _Ctx:
    def __init__(self):
        self.ops: list[tuple] = []

    def memory_seq_rm(self, seq, p0, p1):
        self.ops.append(("rm", seq, p0, p1))

    def memory_seq_cp(self, src, dst, p0, p1):
        self.ops.append(("cp", src, dst, p0, p1))


class _Fut:
    def __init__(self, value=None):
        self._v = value

    def result(self, timeout=None):
        return self._v


class _Engine:
    """control() executes inline — the surgery contract without the thread —
    and COUNTS its calls, so a mutant that runs the surgery directly (racing
    the decode thread) is visible."""

    def __init__(self):
        self._persona_heads = {
            "default": SimpleNamespace(name="default", seq=4, tokens=[7, 8, 9])
        }
        self.control_calls = 0

    def control(self, fn):
        self.control_calls += 1
        return _Fut(fn())


def _backend(snapshots=2) -> LlamaCppBackend:
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
            session_snapshot_max=snapshots,
            session_full_replay=True,
            flow_kv_cache=False,
            flow_kv_cache_max=4,
            resident_session_flow_fork=False,
            reasoning_head_swap=False,
        ),
        personas={},
    )
    b = LlamaCppBackend(config)
    b._decode_mode = "batched"
    b._pool_size = 4
    b._snap_registry = OrderedDict()
    b._batched_snap_seqs = {}
    b._engine = _Engine()
    b._primary_instance = SimpleNamespace(_ctx=_Ctx())
    return b


def _seat(seq=1, n_tokens=8, static_len=3, persona="default"):
    return SimpleNamespace(
        seq=seq,
        persona=persona,
        n_tokens=n_tokens,
        static_len=static_len,
        input_ids=[7, 8, 9, 20, 21, 22, 23, 24],  # head(3) + 5 dynamic
        _engine_ref=_Engine(),
    )


class TestCapture:
    def test_capture_pins_on_the_band(self):
        b = _backend()
        seat = _seat()
        out = b.snapshot_working_seq(seat, "doc:a")
        assert out == {"tokens": 8, "resident": True}
        # The surgery MUST route through the control inbox — a direct call
        # races the decode thread at a step boundary.
        assert seat._engine_ref.control_calls >= 1
        # rm(snap) then cp(seat -> snap), through control
        ctx = b._primary_instance._ctx
        snap_seq = b._batched_snap_seqs["doc:a"]
        assert snap_seq in list(b._batched_seq_map().snap_seqs)
        assert ("rm", snap_seq, 0, -1) in ctx.ops
        assert ("cp", 1, snap_seq, -1, -1) in ctx.ops
        entry = b._snap_registry["doc:a"]
        assert entry["dyn_tokens"] == [20, 21, 22, 23, 24]
        assert entry["static_len"] == 3
        assert entry["persona"] == "default"
        assert entry["resident"] is True

    def test_zero_band_still_refuses_cleanly(self):
        """session_snapshot_max: 0 keeps the E2 behaviour — a clean error,
        not a hazard."""
        b = _backend(snapshots=0)
        with pytest.raises(RuntimeError, match="session_snapshot_max is 0"):
            b.snapshot_working_seq(_seat(), "doc:a")

    def test_capacity_raises_never_evicts(self):
        b = _backend(snapshots=1)
        b.snapshot_working_seq(_seat(), "doc:a")
        with pytest.raises(RuntimeError, match="capacity"):
            b.snapshot_working_seq(_seat(seq=2), "doc:b")
        assert "doc:a" in b._snap_registry, "capacity must not evict the pin"

    def test_duplicate_key_refused(self):
        b = _backend()
        b.snapshot_working_seq(_seat(), "doc:a")
        with pytest.raises(RuntimeError, match="already exists"):
            b.snapshot_working_seq(_seat(seq=2), "doc:a")


class TestFork:
    def test_hot_fork_rebuilds_the_seat(self):
        b = _backend()
        b.snapshot_working_seq(_seat(seq=1), "doc:a")
        target = _seat(seq=2, n_tokens=3)
        target.input_ids = [7, 8, 9]
        got = b.fork_snapshot_seq(target, "doc:a")
        assert got == 8  # static 3 + 5 dynamic
        assert target._engine_ref.control_calls >= 1, "fork must use the inbox"
        assert target.n_tokens == 8
        assert target.input_ids == [7, 8, 9, 20, 21, 22, 23, 24]
        ctx = b._primary_instance._ctx
        snap_seq = b._batched_snap_seqs["doc:a"]
        assert ("rm", 2, 0, -1) in ctx.ops
        assert ("cp", snap_seq, 2, -1, -1) in ctx.ops

    def test_cross_persona_fork_refused(self):
        b = _backend()
        b.snapshot_working_seq(_seat(persona="default"), "doc:a")
        with pytest.raises(RuntimeError, match="cross-persona"):
            b.fork_snapshot_seq(_seat(seq=2, persona="user_sim"), "doc:a")

    def test_cold_fork_returns_none_and_rebuild_refuses(self):
        """After a context rebuild the pin is gone. Fork says None; the cold
        rebuild is a CLEAN REFUSAL under batched (v1 boundary) — never an
        AttributeError on seat.eval."""
        b = _backend()
        b.snapshot_working_seq(_seat(), "doc:a")
        b._snap_registry["doc:a"]["resident"] = False
        b._batched_snap_seqs.clear()
        assert b.fork_snapshot_seq(_seat(seq=2), "doc:a") is None
        with pytest.raises(RuntimeError, match="COLD under batched"):
            b.rebuild_snapshot_cold(_seat(seq=2), "doc:a")


class TestRebuildCallsDemotion:
    def test_rebuild_invokes_the_demotion(self):
        """_rebuild_batched_context needs a real Metal context, so the unit
        tests exercise _demote_batched_snapshots directly — and THIS guard pins
        the call site, or a refactor could silently orphan the demotion and
        leave stale hot pins pointing into a dead context (fork would seq_cp
        from a seq the rebuilt context never populated)."""
        import inspect

        from inference.backends import llama_cpp_backend as mod

        src = inspect.getsource(mod.LlamaCppBackend._rebuild_batched_context)
        assert "self._demote_batched_snapshots()" in src


class TestPurgeAndDemotion:
    def test_purge_frees_the_band_seq(self):
        b = _backend()
        b._all_instances = []
        b.snapshot_working_seq(_seat(), "doc:a")
        snap_seq = b._batched_snap_seqs["doc:a"]
        assert b.purge_snapshot("doc:a") is True
        assert "doc:a" not in b._snap_registry
        assert "doc:a" not in b._batched_snap_seqs
        assert ("rm", snap_seq, 0, -1) in b._primary_instance._ctx.ops[-1:]

    def test_rebuild_demotion_clears_pins_keeps_registry(self):
        """A context rebuild kills every snapshot's cells. The demotion must
        mark entries cold (so fork says None -> the clean cold refusal) while
        KEEPING the registry — the tokens are the re-capture provenance."""
        b = _backend()
        b.snapshot_working_seq(_seat(), "doc:a")
        n = b._demote_batched_snapshots()
        assert n == 1
        assert b._batched_snap_seqs == {}
        assert b._snap_registry["doc:a"]["resident"] is False
        assert b.fork_snapshot_seq(_seat(seq=2), "doc:a") is None

    def test_the_freed_seq_is_reusable(self):
        b = _backend(snapshots=1)
        b._all_instances = []
        b.snapshot_working_seq(_seat(), "doc:a")
        b.purge_snapshot("doc:a")
        out = b.snapshot_working_seq(_seat(seq=2), "doc:b")
        assert out["resident"] is True
