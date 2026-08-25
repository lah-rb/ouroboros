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
    _POOL_SLACK,
    StreamPhase,
    _AdmitVerdict,
)

from tests.test_batched_engine import (
    EOG,
    FakeCtx,
    FakeSampler,
    _engine_with,
    _req,
    _slot,
)


def _eng():
    # The factory is keyed by request_id, so every id a test admits needs one.
    return _engine_with(
        FakeCtx(decode_script=[0]),
        # Long enough to drive several _step calls; ends on EOG so a
        # natural stop is reachable as well as a budget cut.
        samplers={
            k: FakeSampler([1, 2, 3, 4, EOG]) for k in ("a", "x", "big", "small")
        },
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


class TestACutIsAlwaysAnnounced:
    """Every way a generation can stop short must reach the caller.

    Found by the 2026-07-27 replay: it generated 60,138 of an admitted 60,138
    and reported `truncated: False`. The API derives truncation as
    `tokens_generated >= max_tokens`, but ADMISSION had sized the engine's
    budget (60,138) below the caller's (60,330) — so the cut landed in the gap
    and was invisible. Same shape as the force-window hole, introduced by the
    admission clamp itself.
    """

    def test_hitting_the_engine_budget_retires_as_length_not_completed(self):
        """Drives the REAL _step to its cap. An earlier version of this test
        reimplemented the decision inline and passed against a mutation that
        removed the fix entirely — proving only that the copy agreed with
        itself."""
        from inference.batched_engine import _END_LENGTH

        eng = _eng()
        req = _req("x", [100], max_tokens=2, slot=_seat(3))
        req._stream_id = "x"
        eng._admit(req)
        for _ in range(4):
            eng._step()
        st_reason = req.slot._last_end_reason
        assert st_reason == _END_LENGTH, (
            f"a generation cut at its budget must not look like a natural "
            f"stop — got {st_reason!r}"
        )
        assert req.out.done and req.out.error is None, "and it still succeeds"

    def test_a_stream_that_stops_naturally_is_not_marked_length(self):
        """EOG before the cap — the reason must stay ordinary."""
        eng = _eng()
        req = _req("x", [100], max_tokens=50, slot=_seat(3))
        req._stream_id = "x"
        eng._admit(req)
        for _ in range(6):
            eng._step()
        assert req.slot._last_end_reason != "length"

    def test_both_engine_cuts_mark_the_outcome_truncated(self):
        from core.inference import CompletionOutcome

        for reason in ("length", "kv_pressure_truncated"):
            assert CompletionOutcome(
                text="x", tokens_generated=1, end_reason=reason
            ).truncated_by_engine, reason

    def test_an_ordinary_completion_is_not_marked_truncated(self):
        from core.inference import CompletionOutcome

        for reason in ("", "completed", "final_channel_close"):
            assert not CompletionOutcome(
                text="x", tokens_generated=1, end_reason=reason
            ).truncated_by_engine, reason

    def test_the_engine_budget_can_sit_below_the_callers(self):
        """The precondition that makes the derived test insufficient — if these
        were always equal the API's arithmetic would suffice."""
        eng = _eng()
        _live(eng, "a", 0, gen_start=0, budget=50_000)
        req = _req("x", [100], max_tokens=40_000, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, n_ctx=POOL, total=100)
        assert verdict is _AdmitVerdict.ADMIT
        assert got < req.max_tokens


# ── the 2026-08-24/25 wedge (3h38m, 2,288,327 no-progress decodes) ──
#
# Every request in the tests above uses a ONE-TOKEN prompt (`_req(..., [100])`),
# so the case that actually took production down — a prompt large relative to
# free cells — was untested by construction. These cover it.


def _prompt(n):
    """A prompt of n distinct tokens. The fixtures above use [100]; a
    one-token prompt cannot exercise prompt-vs-pool arithmetic at all."""
    return list(range(1000, 1000 + n))


class TestAPromptMustFitBeforeAGenerationIsSized:
    def test_the_incident_case_queues_instead_of_admitting(self):
        """Verbatim: a live stream holding 31,376 cells, then a 31,244-token
        prompt asking 8,192. Production admitted it at 2,660 and then could
        not prefill a single 2,048-row batch, forever."""
        eng = _engine_with(
            FakeCtx(decode_script=[0]),
            samplers={"a": FakeSampler([1, EOG]), "big": FakeSampler([1, EOG])},
            n_batch=2048,
        )
        eng._llama._n_ctx = POOL
        _live(eng, "a", 1, 23_184, 8_192)  # 31,376 cells entitled

        req = _req("big", _prompt(31_244), max_tokens=8_192, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, POOL, 2)

        assert verdict is _AdmitVerdict.QUEUE
        assert got == 0

    def test_a_prompt_that_leaves_room_still_admits(self):
        eng = _engine_with(
            FakeCtx(decode_script=[0]),
            samplers={"a": FakeSampler([1, EOG]), "small": FakeSampler([1, EOG])},
            n_batch=2048,
        )
        eng._llama._n_ctx = POOL
        _live(eng, "a", 1, 10_000, 2_000)

        req = _req("small", _prompt(4_000), max_tokens=2_048, slot=_seat(3))
        got, verdict = eng._size_against_pool(req, POOL, 2)
        assert verdict is _AdmitVerdict.ADMIT
        assert got == 2_048


class TestThePoolSlackIsAWholeBatchNotAGuess:
    def test_slack_tracks_the_batch_size(self):
        eng = _engine_with(FakeCtx(), samplers={}, n_batch=2048)
        eng._llama._n_ctx = POOL
        assert eng._pool_slack == 2 * 2048

    def test_a_small_context_is_not_eaten_by_its_own_margin(self):
        """n_ctx 4096 with n_batch 2048 would want 4096 of slack — the whole
        pool. The cap turns that from an outage into a margin."""
        eng = _engine_with(FakeCtx(), samplers={}, n_batch=2048)
        eng._llama._n_ctx = 4096
        assert eng._pool_slack <= 4096 // 8
        assert eng._free_cells(4096) > 0

    def test_the_default_fixture_is_unchanged(self):
        """The 64/4096 fixture must keep the historical 256 so every
        assertion written against it stays honest."""
        eng = _engine_with(FakeCtx(), samplers={})
        assert eng._pool_slack == 256


class TestTheAdmissionLogNamesThePrompt:
    def test_a_shrink_prints_prompt_pool_and_residual_separately(self, caplog):
        """The old format string printed `free` for BOTH placeholders, so the
        log was self-consistent and never showed the prompt that was the whole
        story. Three distinct numbers, or the next wedge is invisible too."""
        import logging

        eng = _engine_with(
            FakeCtx(decode_script=[0]),
            samplers={"a": FakeSampler([1, EOG]), "small": FakeSampler([1, EOG])},
            n_batch=2048,
        )
        eng._llama._n_ctx = POOL
        _live(eng, "a", 1, 40_000, 8_000)

        req = _req("small", _prompt(6_000), max_tokens=8_192, slot=_seat(3))
        with caplog.at_level(logging.INFO):
            got, verdict = eng._size_against_pool(req, POOL, 2)

        assert verdict is _AdmitVerdict.ADMIT
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "prompt 6000 tok" in text
        assert "residual" in text


class TestCellsHeldOutsideTheSeatsAreCounted:
    """Bands hold real cells and were counted NOWHERE before 2026-08-25.
    Under kv_unified a seq_cp shares cells, so a band pin is free while its
    source seat lives and costs everything the moment that seat is cleared."""

    def test_a_snapshot_band_counts_against_free_cells(self):
        eng = _engine_with(FakeCtx(decode_script=[0]), samplers={})
        eng._llama._n_ctx = POOL
        before = eng._free_cells(POOL)
        eng.extra_occupancy_fn = lambda: 30_000
        assert eng._free_cells(POOL) == before - 30_000

    def test_a_flow_prefix_counts_against_free_cells(self):
        from inference.batched_engine import FlowPin

        eng = _engine_with(FakeCtx(decode_script=[0]), samplers={})
        eng._llama._n_ctx = POOL
        before = eng._free_cells(POOL)
        eng._flow_pins["k"] = FlowPin(key="k", seq=9, n_tokens=6_000, tokens=[])
        assert eng._free_cells(POOL) == before - 6_000

    def test_a_band_that_raises_never_breaks_the_decode_thread(self):
        def boom():
            raise RuntimeError("registry busy")

        eng = _engine_with(FakeCtx(decode_script=[0]), samplers={})
        eng._llama._n_ctx = POOL
        eng.extra_occupancy_fn = boom
        assert eng._free_cells(POOL) > 0  # no exception escapes

    def test_band_pressure_queues_and_never_fails_the_caller(self):
        """A snapshot is PURGEABLE (sweep_stale_snapshots), so band pressure
        must mean 'wait', never 'impossible'. Without this asymmetry, band
        pressure turns legitimate large prompts into permanent failures."""
        eng = _engine_with(
            FakeCtx(decode_script=[0]),
            samplers={"big": FakeSampler([1, EOG])},
            n_batch=2048,
        )
        eng._llama._n_ctx = POOL
        eng.extra_occupancy_fn = lambda: 60_000

        req = _req("big", _prompt(4_000), max_tokens=2_048, slot=_seat(3))
        _got, verdict = eng._size_against_pool(req, POOL, 2)
        assert verdict is _AdmitVerdict.QUEUE
        assert verdict is not _AdmitVerdict.IMPOSSIBLE


class TestOccupancySumsPerSeatRatherThanTakingAGlobalMax:
    def test_idle_pinned_seats_and_a_live_stream_all_count(self):
        eng = _engine_with(
            FakeCtx(decode_script=[0]), samplers={"a": FakeSampler([1, EOG])}
        )
        eng._llama._n_ctx = POOL
        eng._seats = [
            _seat(0, pinned=True, n_tokens=8_000),
            _seat(1, pinned=True, n_tokens=8_000),
        ]
        _live(eng, "a", 5, 12_000, 8_000)  # 20,000 on a seq with no seat

        total = eng._occupancy()
        assert total == 8_000 + 8_000 + 20_000
        # The old global-max view discarded whichever side was smaller.
        assert total > max(eng._live_occupancy(), eng._pinned_occupancy())
