"""Admission sizes a stream against FREE cells, not the whole window.

`effective_max = min(req.max_tokens, n_ctx - total)` is a per-STREAM bound, but
under batched decode `n_ctx` budgets the SUM of live streams. Every stream can
pass its own check while their total does not — which is how a 48,318-token
generation carrying 15 complete files reached `KV cell pool exhausted` and was
discarded (2026-07-27 APEX arm).

Policy: shrink to fit; queue below a useful floor; fail outright when the
request cannot fit even once every evictable stream is gone.

The budget is only correct AT ADMISSION and goes stale as streams come and go —
that is expected, and it is why the reactive ladder (_relieve_pressure) stays.
This makes pressure rare; force-windowing makes it survivable.
"""

from __future__ import annotations

from inference.batched_engine import (
    _MIN_ADMIT_BUDGET,
    _POOL_SLACK,
    StreamPhase,
    _AdmitVerdict,
)

from tests.test_batched_engine import FakeCtx, FakeSampler, _engine_with, _req, _slot


def _eng():
    # The factory is keyed by request_id, so every id a test admits needs one.
    return _engine_with(
        FakeCtx(decode_script=[0]),
        samplers={k: FakeSampler([1]) for k in ("a", "x", "big", "small")},
    )


POOL = 65_536


def _seat(seq, n_ctx=POOL, pinned=False, n_tokens=2):
    slot = _slot(seq)
    slot._n_ctx = n_ctx
    slot.pinned = pinned
    slot.n_tokens = n_tokens
    return slot


def _live(eng, stream_id, seq, gen_start, budget, pinned=False):
    """Register a live stream holding `gen_start + budget` cells.

    Fields are set AFTER admission: admission itself now sizes against the
    pool, so building the fixture through it would clamp the very numbers the
    test is trying to establish.
    """
    req = _req(stream_id, [1], max_tokens=8, slot=_seat(seq, pinned=pinned))
    req._stream_id = stream_id
    eng._admit(req)
    st = eng._streams[stream_id]
    st.phase = StreamPhase.DECODING
    st.gen_start_pos = gen_start
    st.effective_max = budget
    return st


class TestOccupancyCountsEntitlement:
    def test_a_live_stream_is_counted_at_its_BUDGET_not_its_position(self):
        """Counting decoded position under-counts: a stream 500 tokens into a
        40k budget is going to take those 40k. Sizing against its current
        position is precisely how the pool oversubscribes."""
        eng = _eng()
        s = _live(eng, "a", 0, gen_start=1000, budget=40_000)
        s.n_past = 1500  # barely started
        assert eng._live_occupancy() == 41_000

    def test_a_retired_stream_stops_counting(self):
        eng = _eng()
        s = _live(eng, "a", 0, gen_start=1000, budget=40_000)
        s.phase = StreamPhase.DONE
        assert eng._live_occupancy() == 0

    def test_pinned_seats_form_the_irreducible_floor(self):
        eng = _eng()
        eng._seats = [_seat(0, pinned=True, n_tokens=5_000)]
        assert eng._pinned_occupancy() >= 5_000


class TestTheThreeVerdicts:
    def test_a_quiet_pool_admits_the_full_ask(self):
        eng = _eng()
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, n_ctx=POOL, total=100)
        assert verdict is _AdmitVerdict.ADMIT
        assert got == req.max_tokens

    def test_a_busy_pool_shrinks_rather_than_oversubscribing(self):
        """A smaller generation that COMPLETES beats a larger one that gets
        evicted, because eviction discards everything."""
        eng = _eng()
        _live(eng, "a", 0, gen_start=0, budget=50_000)
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, n_ctx=POOL, total=100)
        assert verdict is _AdmitVerdict.ADMIT
        assert got == POOL - 50_000 - _POOL_SLACK - len(req.prompt_tokens)
        assert got < req.max_tokens

    def test_a_full_pool_queues_instead_of_admitting_a_useless_budget(self):
        eng = _eng()
        _live(eng, "a", 0, gen_start=0, budget=65_000)
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, n_ctx=POOL, total=100)
        assert verdict is _AdmitVerdict.QUEUE
        assert got == 0

    def test_what_can_never_fit_fails_instead_of_queueing_forever(self):
        """Pinned occupancy is irreducible — waiting cannot help, so the caller
        gets a diagnosable error rather than a silent stall."""
        eng = _eng()
        eng._seats = [_seat(0, pinned=True, n_tokens=65_000)]
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        _, verdict = eng._size_against_pool(req, n_ctx=POOL, total=100)
        assert verdict is _AdmitVerdict.IMPOSSIBLE

    def test_an_unknown_pool_size_keeps_the_old_per_stream_bound(self):
        eng = _eng()
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, n_ctx=0, total=100)
        assert verdict is _AdmitVerdict.ADMIT
        assert got == req.max_tokens


class TestTheQueueMakesProgress:
    def test_a_queued_request_is_admitted_once_capacity_frees(self):
        eng = _eng()
        big = _live(eng, "a", 0, gen_start=0, budget=65_000)
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        req._stream_id = "x"
        eng._admit(req)
        assert "x" not in eng._streams and eng._waiting == [req]

        big.phase = StreamPhase.DONE  # capacity frees
        eng._drain_waiting()
        assert "x" in eng._streams, "the drain must re-admit once room exists"
        assert eng._waiting == []

    def test_an_abandoned_request_is_dropped_from_the_queue(self):
        eng = _eng()
        _live(eng, "a", 0, gen_start=0, budget=65_000)
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        req._stream_id = "x"
        eng._admit(req)
        req.out.closed = True
        eng._drain_waiting()
        assert eng._waiting == []
        assert "x" not in eng._streams

    def test_the_queue_head_blocks_so_a_large_job_is_not_starved(self):
        """FIFO with a stop at the first non-fitting request: otherwise a
        stream of small asks walks past a big one indefinitely."""
        eng = _eng()
        _live(eng, "a", 0, gen_start=0, budget=65_000)
        big = _req("big", [100], max_tokens=40_000, slot=_seat(3))
        big._stream_id = "big"
        small = _req("small", [100], max_tokens=600, slot=_seat(4))
        small._stream_id = "small"
        eng._admit(big)
        eng._admit(small)
        assert eng._waiting == [big, small]
        eng._drain_waiting()  # still full — nothing moves
        assert eng._waiting == [big, small]

    def test_an_idle_pool_never_leaves_work_queued(self):
        """Guards a busy-spin: the decode loop's wake predicate includes
        _waiting, so if a queued request could sit un-admittable with no active
        streams the loop would spin. With nothing live, occupancy is the pinned
        floor, so the verdict is ADMIT or IMPOSSIBLE — never QUEUE."""
        eng = _eng()
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        _, verdict = eng._size_against_pool(req, n_ctx=POOL, total=100)
        assert verdict is not _AdmitVerdict.QUEUE
