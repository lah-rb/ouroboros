"""The stateless flow cache under decode_mode: batched.

Was a documented pool-only deferral ("the persona head already delivers the
dominant prefill saving") — but the persona head covers only the GLOBAL
static, and production runs batched: 67% of production prompt tokens were
redundant re-reads, and the measured flow payout at realistic head sizes is
3.51s/call = 49% of prefill (glm corrected pilot, 2026-07-30).

Mechanics under test: BUILD = on a stream's NORMAL completion the decode
thread range-copies [0, prefix_len) off its seat onto a flow-band seq — the
dynamic tail and the generation sit above the range and are excluded. HIT =
whole-seq install onto a fresh seat via the control inbox. Pins are LRU to the
band size and die with the context on rebuild.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inference.batched_engine import FlowPin, plan_seq_map


class TestSeqMapFlowBand:
    def test_band_sits_above_snapshots(self):
        m = plan_seq_map(4, [], ["low"], snapshots=2, flow_slots=8)
        # 4 working + 1 persona + 1 reasoning = 6; snaps [6,8); flow [8,16)
        assert list(m.snap_seqs) == [6, 7]
        assert list(m.flow_seqs) == list(range(8, 16))
        assert m.n_seq_max == 16

    def test_zero_band_is_the_old_layout(self):
        m = plan_seq_map(4, [], [], snapshots=2, flow_slots=0)
        assert len(m.flow_seqs) == 0
        assert m.n_seq_max == 7

    def test_negative_refused(self):
        with pytest.raises(ValueError):
            plan_seq_map(4, [], [], flow_slots=-1)


# ── engine-side capture/install with a recording ctx ────────────────────

class _Ctx:
    def __init__(self):
        self.ops: list[tuple] = []

    def memory_seq_rm(self, seq, p0, p1):
        self.ops.append(("rm", seq, p0, p1))

    def memory_seq_cp(self, src, dst, p0, p1):
        self.ops.append(("cp", src, dst, p0, p1))


def _engine(flow_slots=2):
    """A minimal object carrying the engine's flow surface — the real methods
    bound onto it, so the logic under test is the production code."""
    from collections import OrderedDict

    import inference.batched_engine as mod

    e = SimpleNamespace(
        _llama=SimpleNamespace(_ctx=_Ctx()),
        _seq_map=plan_seq_map(4, [], [], snapshots=0, flow_slots=flow_slots),
        _flow_pins=OrderedDict(),
        h_flow_builds=0, h_flow_hits=0, h_flow_evicts=0,
    )
    e._capture_flow = mod.BatchedEngine._capture_flow.__get__(e)
    e.control = lambda fn: SimpleNamespace(result=lambda timeout=None: fn())
    e.install_flow_sync = mod.BatchedEngine.install_flow_sync.__get__(e)
    return e


def _done_stream(seat_seq=1, n_past=12, key="ops:plan", plen=8):
    """At capture time (inside _retire, BEFORE the seat update) the seat's
    n_tokens still holds the persona-head length — only the stream's n_past
    carries the true decoded position. The fixture models exactly that:
    seat.n_tokens is deliberately BELOW plen, so a guard that reads the seat
    instead of the stream rejects every capture (the live-acceptance bug)."""
    seat = SimpleNamespace(seq=seat_seq, n_tokens=2)
    return SimpleNamespace(
        slot=seat,
        stream_id="s1",
        n_past=n_past,
        req=SimpleNamespace(flow_build=(key, plen, list(range(plen))), flow_hit=False),
    )


class TestBuildCapture:
    def test_capture_is_a_range_copy_excluding_tail_and_generation(self):
        e = _engine()
        e._capture_flow(_done_stream(n_past=12, plen=8))
        band = list(e._seq_map.flow_seqs)
        pin = e._flow_pins["ops:plan"]
        assert pin.seq in band and pin.n_tokens == 8
        ctx = e._llama._ctx
        assert ("cp", 1, pin.seq, 0, 8) in ctx.ops, (
            "must copy ONLY [0, prefix_len) — the tail and generation sit above"
        )
        assert e.h_flow_builds == 1

    def test_short_stream_pins_nothing(self):
        """A stream that never reached the head boundary (KV-pressure cut,
        early stop) must not pin a truncated prefix as if it were the head."""
        e = _engine()
        e._capture_flow(_done_stream(n_past=5, plen=8))
        assert e._flow_pins == {}

    def test_lru_evicts_the_oldest_and_reuses_its_seq(self):
        e = _engine(flow_slots=1)
        e._capture_flow(_done_stream(key="a", plen=4, n_past=6))
        seq_a = e._flow_pins["a"].seq
        e._capture_flow(_done_stream(key="b", plen=4, n_past=6, seat_seq=2))
        assert "a" not in e._flow_pins
        assert e._flow_pins["b"].seq == seq_a
        assert e.h_flow_evicts == 1

    def test_duplicate_key_is_a_noop(self):
        e = _engine()
        e._capture_flow(_done_stream())
        ops_before = len(e._llama._ctx.ops)
        e._capture_flow(_done_stream(seat_seq=3))
        assert len(e._llama._ctx.ops) == ops_before


class TestHitInstall:
    def test_install_replaces_the_seat_and_counts(self):
        e = _engine()
        pin = FlowPin("ops:plan", 4, 8, list(range(8)))
        e._flow_pins["ops:plan"] = pin
        seat = SimpleNamespace(seq=2, n_tokens=3, static_len=3, input_ids=[0, 1, 2])
        e.install_flow_sync(seat, pin)
        ctx = e._llama._ctx
        assert ("rm", 2, 0, -1) in ctx.ops
        assert ("cp", 4, 2, -1, -1) in ctx.ops
        assert seat.n_tokens == 8
        assert seat.input_ids == list(range(8))
        assert e.h_flow_hits == 1


class TestBackendWiring:
    """The _batched_stream integration is thread-and-engine heavy; these are
    source guards for the seams a refactor could silently sever (the P1
    clamp-site pattern)."""

    def _src(self):
        import inspect

        from inference.backends import llama_cpp_backend as mod

        return inspect.getsource(mod)

    def test_flow_prefix_len_is_no_longer_discarded(self):
        src = self._src()
        assert "flow_kv_cache is pool-only" not in src

    def test_hit_moves_the_split_to_the_flow_boundary(self):
        src = self._src()
        assert "if flow_hit:\n            n_static = flow_prefix_len" in src

    def test_reasoning_swap_refused_under_a_pin(self):
        """Pin the CONDITION, not the log message — a mutant gutted the guard
        to `if False` while the message survived the first pass."""
        src = self._src()
        assert "if flow_hit and _reasoning:" in src
        assert "refused (flow prefix pinned)" in src

    def test_rebuild_clears_the_pins(self):
        import inspect

        from inference.backends import llama_cpp_backend as mod

        src = inspect.getsource(mod.LlamaCppBackend._rebuild_batched_context)
        assert "_flow_pins.clear()" in src

    def test_health_merges_engine_counters(self):
        src = self._src()
        assert 'getattr(_eng, "h_flow_hits", 0)' in src

    def test_retire_captures_only_on_normal_completion(self):
        """The original guard was `reason is None and error is None` — but
        every successful retirement carries a reason ("completed" on EOS,
        "length" on cap), so the capture was UNREACHABLE on all paths and
        the live acceptance measured 0 builds ever. The guard must
        discriminate on error + the known-bad reasons."""
        import inspect

        from inference import batched_engine as mod

        src = inspect.getsource(mod.BatchedEngine._retire)
        assert "reason is None and error is None" not in src, (
            "unreachable-guard regression: no success path retires with "
            "reason=None"
        )
        assert "error is None" in src
        assert 'reason not in (_END_KV_PRESSURE, "abandoned")' in src
