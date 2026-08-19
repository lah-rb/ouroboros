"""Batched seat-leak drain (2026-07-24 swarm-study fix).

Pins the three cancellation holes that wedged the engine at scale
(inFlight=30 phantom seats, zero decode, weight eviction) plus the reaper
backstop:

  1. a consumer cancel mid-``generate_async`` must still close the bridge,
     cancel the engine stream, exit the generation guard, and leave the
     seat releasable (shielded ``aclose`` of the delegated asyncgen);
  2. ``release_instance`` must requeue the seat + decrement the checkout
     counter even with a cancellation pending (shield + finally);
  3. a cancel inside ``acquire_instance``'s prepare/restore awaits must
     roll the checkout back (the entry points acquire outside their try);
  4. the seat reaper reclaims leased seats with no live stream after two
     sweeps, skips pinned/streaming seats, and swallows the late duplicate
     release.

Style follows test_pool_scaling.py / test_refresh_drain.py: the REAL
backend with fake engine/seats, no model, sync tests driving asyncio.run.
"""

import asyncio
import concurrent.futures
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402
from inference.batched_engine import SeqSlot, StreamPhase  # noqa: E402


def _backend() -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=2,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        model=SimpleNamespace(
            family="harmony",
            context_refresh_interval=75,
            context_refresh_seconds=1800,
            context_refresh_drain_s=0.0,
        ),
        generation=SimpleNamespace(
            top_p=None,
            top_k=None,
            min_p=None,
            presence_penalty=None,
            repeat_penalty=None,
            penalty_last_n=None,
            dry_multiplier=None,
            # Mirror GenerationConfig: _build_generate_kwargs reads these by
            # direct attribute access, so a double missing one raises
            # AttributeError rather than behaving like an unset option.
            penalty_freq=None,
            reasoning_budget=None,
            reasoning_start=None,
            reasoning_end=None,
            logit_bias=None,
        ),
    )
    be = LlamaCppBackend(config)
    be._decode_mode = "batched"
    be._ready_event.set()
    return be


class FakeEngine:
    """Just enough engine surface for acquire/release/_batched_stream:
    synchronous control ops, recorded seat/cancel calls, a stream registry
    the reaper snapshots."""

    def __init__(self):
        head = SimpleNamespace(seq=1, n_tokens=0, tokens=[])
        self._persona_heads = {"default": head}
        self._streams = {}
        self._waiting = []  # queued admissions — their seats are LEASED
        self.prepared = []
        self.cleared = []
        self.cancelled = []
        self.submitted = []

    def control(self, fn):
        fut = concurrent.futures.Future()
        try:
            fut.set_result(fn())
        except BaseException as exc:  # noqa: BLE001 — surfaced via the Future
            fut.set_exception(exc)
        return fut

    def prepare_seat(self, seat, persona):
        self.prepared.append(seat.seq)
        seat.persona = persona
        seat.static_len = 0
        seat.n_tokens = 0
        seat.input_ids = []

    def clear_seat(self, seat):
        self.cleared.append(seat.seq)
        seat.pinned = False

    def submit(self, req):
        self.submitted.append(req)
        return "stream-1"

    def cancel(self, stream_id):
        self.cancelled.append(stream_id)


def _wire(be, engine, n_seats=1):
    seats = [SeqSlot(seq=i + 2) for i in range(n_seats)]
    be._engine = engine
    be._engine_seats = seats
    be._pool_queue = asyncio.Queue(maxsize=max(n_seats, 2))
    be._persona_queues = {"default": be._pool_queue}
    for seat in seats:
        be._pool_queue.put_nowait(seat)
    return seats


# ── 1. cancelled generate_async drains deterministically ──────────────


def test_cancelled_generate_async_frees_seat_and_counters():
    be = _backend()
    engine = FakeEngine()

    async def scenario():
        _wire(be, engine)
        seat = await be.acquire_instance()
        assert be._checked_out == 1 and seat._leased_at is not None

        task = asyncio.create_task(
            be.generate_async(seat, [1, 2, 3], max_tokens=8, temperature=0.7)
        )
        # let it submit and block on the (never-fed) bridge
        for _ in range(50):
            await asyncio.sleep(0)
            if engine.submitted:
                break
        assert engine.submitted, "stream never submitted"
        bridge = engine.submitted[0].out

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # the shielded aclose ran _batched_stream's finally NOW, not at GC
        assert bridge.closed is True
        assert engine.cancelled == ["stream-1"]
        # generation guard exited — no phantom in_flight
        assert be._active_generations == 0

        await be.release_instance(seat)
        assert be._checked_out == 0
        assert be._pool_queue.qsize() == 1
        assert seat._leased_at is None

    asyncio.run(scenario())


# ── 2. release survives a pending cancellation ────────────────────────


def test_release_instance_requeues_under_pending_cancel():
    be = _backend()

    class SlowClearEngine(FakeEngine):
        """clear_seat's control future resolves later — the release is
        suspended mid-await when the cancel lands."""

        def __init__(self):
            super().__init__()
            self.pending = None

        def control(self, fn):
            fut = concurrent.futures.Future()
            self.pending = (fn, fut)
            return fut

    engine = SlowClearEngine()

    async def scenario():
        (seat,) = _wire(be, engine)
        be._pool_queue.get_nowait()  # seat is "checked out"
        be._checked_out = 1
        seat._leased_at = 1.0

        task = asyncio.create_task(be.release_instance(seat))
        await asyncio.sleep(0.01)  # suspended on the clear await
        task.cancel()
        fn, fut = engine.pending
        fut.set_result(fn())  # clear completes under the shield

        try:
            await task
        except asyncio.CancelledError:
            pass  # the cancel may still surface after the finally ran

        assert engine.cleared == [seat.seq]
        assert be._pool_queue.qsize() == 1, "seat not requeued"
        assert be._checked_out == 0, "checkout counter leaked"

    asyncio.run(scenario())


# ── 3. cancel during acquire rolls the checkout back ──────────────────


def test_acquire_cancel_rolls_back_checkout():
    be = _backend()

    class StuckPrepareEngine(FakeEngine):
        def control(self, fn):
            return concurrent.futures.Future()  # never resolves

    engine = StuckPrepareEngine()

    async def scenario():
        (seat,) = _wire(be, engine)
        task = asyncio.create_task(be.acquire_instance())
        await asyncio.sleep(0.01)  # suspended on the prepare_seat await
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert be._checked_out == 0, "checkout not rolled back"
        assert be._pool_queue.qsize() == 1, "seat lost from the pool"
        assert seat._leased_at is None
        assert be._active_generations == 0

    asyncio.run(scenario())


# ── 4. reaper reclaims orphans, spares pinned/streaming, dedups ───────


def test_seat_reaper_reclaims_orphaned_seats():
    be = _backend()
    engine = FakeEngine()

    async def scenario():
        streaming, orphan, session = _wire(be, engine, n_seats=3)
        for seat in (streaming, orphan, session):
            be._pool_queue.get_nowait()
        be._checked_out = 3
        streaming._leased_at = orphan._leased_at = session._leased_at = 1.0
        session.pinned = True
        engine._streams["s1"] = SimpleNamespace(
            slot=streaming, phase=StreamPhase.DECODING
        )

        strikes = {}
        await be._seat_reaper_sweep(strikes)
        # one strike — nothing reclaimed yet
        assert be._pool_queue.qsize() == 0 and be._checked_out == 3

        await be._seat_reaper_sweep(strikes)
        # two strikes: only the orphan is reclaimed
        assert be._pool_queue.qsize() == 1
        assert be._pool_queue.get_nowait() is orphan
        assert be._checked_out == 2
        assert engine.cleared == [orphan.seq]
        assert orphan._leased_at is None
        assert streaming._leased_at is not None and session.pinned

        # the leaked consumer's late release is swallowed, not double-queued
        be._pool_queue.put_nowait(orphan)
        await be.release_instance(orphan)
        assert be._pool_queue.qsize() == 1
        assert be._checked_out == 2
        assert engine.cleared == [orphan.seq]  # no second clear

        # next acquire clears the reclaim mark: later releases are normal
        got = await be.acquire_instance()
        assert got is orphan and id(orphan) not in be._reaper_reclaimed
        await be.release_instance(orphan)
        assert be._pool_queue.qsize() == 1 and be._checked_out == 2

    asyncio.run(scenario())


# ── strike bookkeeping: a stream reappearing resets the count ─────────


def test_seat_reaper_strike_resets_when_stream_appears():
    be = _backend()
    engine = FakeEngine()

    async def scenario():
        (seat,) = _wire(be, engine)
        be._pool_queue.get_nowait()
        be._checked_out = 1
        seat._leased_at = 1.0

        strikes = {}
        await be._seat_reaper_sweep(strikes)
        assert strikes  # one strike accrued

        engine._streams["s1"] = SimpleNamespace(slot=seat, phase=StreamPhase.DECODING)
        await be._seat_reaper_sweep(strikes)
        assert not strikes  # reset — the seat is doing real work

        del engine._streams["s1"]
        await be._seat_reaper_sweep(strikes)
        assert be._checked_out == 1  # back to strike one, not reclaimed

    asyncio.run(scenario())


# ── health: KV budget + seat accounting surfaced ──────────────────────


def test_health_reports_kv_pool_tokens_and_seat_accounting():
    be = _backend()
    be.config.model.n_ctx = 131072

    class HealthyEngine(FakeEngine):
        def health(self):
            return {"decode_mode": "batched", "active_streams": 3}

    async def scenario():
        _wire(be, HealthyEngine())
        be._checked_out = 2
        info = be.get_health_status()
        assert info["kv_pool_tokens"] == 131072
        assert info["decode_mode"] == "batched"
        assert info["checked_out"] == 2
        assert info["batched_engine"]["active_streams"] == 3

    asyncio.run(scenario())


# ── the reaper must not reclaim a QUEUED admission's seat ─────────────


def test_reaper_spares_a_queued_admissions_seat():
    """THE seq-wedge root cause (2026-08-19, X=1764/Y=9595). A request the
    engine parked in _waiting (QUEUE verdict) holds its pre-acquired seat
    but has no StreamState, so a liveness snapshot of _streams alone calls
    the seat orphaned. Under contention the queue wait outlives two sweep
    strikes, the reaper cleared the seat, and the clear landed between the
    admitted stream's prefill chunks — KV lost 7,830 positions mid-flight
    and the shared context wedged. Queued seats are live seats."""
    be = _backend()
    engine = FakeEngine()

    async def scenario():
        (queued,) = _wire(be, engine)
        be._pool_queue.get_nowait()
        be._checked_out = 1
        queued._leased_at = 1.0  # ancient — stale by wall clock
        engine._waiting.append(SimpleNamespace(slot=queued))

        strikes = {}
        await be._seat_reaper_sweep(strikes)
        await be._seat_reaper_sweep(strikes)
        await be._seat_reaper_sweep(strikes)

        assert engine.cleared == [], "reaper cleared a queued admission's seat"
        assert be._pool_queue.qsize() == 0 and be._checked_out == 1
        assert queued._leased_at is not None

        # Once the queue drains (request admitted then retired, seat
        # released by its consumer), the reaper treats it normally again.
        engine._waiting.clear()
        await be._seat_reaper_sweep(strikes)
        await be._seat_reaper_sweep(strikes)
        assert engine.cleared == [queued.seq]

    asyncio.run(scenario())


# ── clear_seat refuses to strip an occupied seat ──────────────────────


def test_clear_seat_refuses_an_occupied_seat():
    """Belt and braces UNDER the reaper fix: whatever bookkeeping goes
    stale next, a clear that lands on a seat with a live stream (or a
    queued request) removes KV out from under an in-flight prefill and
    wedges the shared context. The engine's own registry is the truth on
    the decode thread, so the clear itself checks it."""
    from inference.batched_engine import BatchedEngine

    class _Ctx:
        def __init__(self):
            self.removed = []

        def memory_seq_rm(self, seq, a, b):
            self.removed.append((seq, a, b))

    ctx = _Ctx()
    eng = SimpleNamespace(
        _streams={},
        _waiting=[],
        _llama=SimpleNamespace(_ctx=ctx),
        h_clear_refusals=0,
    )
    seat = SimpleNamespace(
        seq=2, n_tokens=9595, static_len=1765, input_ids=[1], pinned=False
    )

    # Occupied by a live stream -> refused, KV untouched, counter up.
    eng._streams["s"] = SimpleNamespace(
        slot=seat, phase=StreamPhase.DECODING, stream_id="s"
    )
    BatchedEngine.clear_seat(eng, seat)
    assert ctx.removed == [] and seat.n_tokens == 9595
    assert eng.h_clear_refusals == 1

    # Occupied by a QUEUED request -> also refused.
    eng._streams.clear()
    eng._waiting.append(SimpleNamespace(slot=seat, request_id="q1"))
    BatchedEngine.clear_seat(eng, seat)
    assert ctx.removed == [] and eng.h_clear_refusals == 2

    # Free -> clears normally.
    eng._waiting.clear()
    BatchedEngine.clear_seat(eng, seat)
    assert ctx.removed == [(2, 0, -1)]
    assert seat.n_tokens == 0 and seat.input_ids == [] and seat.static_len == 0
