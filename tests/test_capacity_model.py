"""The admission gate: two limits, a race, and a safe direction to fail in.

The model is an optimizer with the server's own admission control behind
it, so these tests care most about the direction of each error. Being too
conservative costs throughput; being too eager costs nothing but a queued
request on the server. Every ambiguous case must resolve toward holding
back — and the tests say so explicitly, because the next reader will be
tempted to "fix" the conservatism.
"""

from __future__ import annotations

import pytest

from agent.effects.capacity import Snapshot
from agent.scheduler.capacity_model import (
    DEGRADED_WIDTH,
    CapacityModel,
    estimate_kv_draw,
)


class _Feed:
    def __init__(self, snap=None):
        self._snap = snap

    def snapshot(self):
        return self._snap

    def set(self, snap):
        self._snap = snap


def _snap(**kw):
    base = dict(
        seq=10,
        serving=True,
        seats_total=4,
        seats_free=4,
        kv_pool_tokens=65536,
        free_cells=40000,
        min_admit_budget=512,
        waiting=0,
        source="ws",
        received_at=0.0,
    )
    base.update(kw)
    return Snapshot(**base)


def _model(snap=None):
    m = CapacityModel(_Feed(snap))
    m._now = lambda: 0.0
    return m


# ── the two limits ────────────────────────────────────────────────────


def test_seats_and_cells_exhaust_independently():
    """Free KV does not manufacture a seat; a free seat does not make a
    large prompt fit. Both must be checked or one silently governs."""
    seats_gone = _model(_snap(seats_free=0, free_cells=60000))
    v = seats_gone.admit("curate", est_kv=1000)
    assert not v.admitted and "seat" in v.reason

    cells_gone = _model(_snap(seats_free=4, free_cells=800))
    v2 = cells_gone.admit("curate", est_kv=10_000)
    assert not v2.admitted and "cells" in v2.reason

    ok = _model(_snap())
    assert ok.admit("curate", est_kv=10_000).admitted


def test_min_admit_budget_is_required_headroom_not_a_suggestion():
    """The server refuses below its floor, so admitting at exactly
    free_cells guarantees a queue."""
    m = _model(_snap(free_cells=10_000, min_admit_budget=512))
    assert not m.admit("x", est_kv=9_600).admitted  # 9600+512 > 10000
    assert m.admit("x", est_kv=9_000).admitted


# ── the race ──────────────────────────────────────────────────────────


def test_reservations_stop_the_same_cells_being_spent_twice():
    """Between our dispatch and the server's next publish, the snapshot
    under-reports our own draw. Without reservations a burst of workers
    all read the same free_cells and all dispatch."""
    m = _model(_snap(free_cells=25_000, seats_free=4))
    assert m.admit("a", est_kv=10_000).admitted
    m.reserve("a", est_kv=10_000)
    assert m.admit("b", est_kv=10_000).admitted
    m.reserve("b", est_kv=10_000)
    # 25k - 20k reserved = 5k left; a third 10k request must not go.
    assert not m.admit("c", est_kv=10_000).admitted
    free_cells, free_seats = m.effective()
    assert free_cells == 5_000 and free_seats == 2


def test_completion_retires_a_reservation_exactly():
    m = _model(_snap(free_cells=20_000))
    tok = m.reserve("a", est_kv=15_000)
    assert not m.admit("b", est_kv=10_000).admitted
    m.release(tok)
    assert m.admit("b", est_kv=10_000).admitted


def test_a_reservation_settles_after_a_full_publish_cycle():
    """seq > issued_against + 1 means a publish happened AFTER our
    dispatch, so the server's numbers already include it. Settling at
    +1 exactly would be wrong: that snapshot may have been in flight."""
    feed = _Feed(_snap(seq=10, free_cells=20_000))
    m = CapacityModel(feed)
    m._now = lambda: 0.0
    m.reserve("a", est_kv=15_000)

    feed.set(_snap(seq=11, free_cells=5_000))  # may predate our admit
    assert m.effective()[0] == 0, "still held at +1"

    feed.set(_snap(seq=12, free_cells=5_000))  # certainly after it
    assert m.effective()[0] == 5_000, "released at +2; server now counts it"


def test_a_reservation_settles_on_timeout_when_nothing_reports_back():
    """Catches a request that died without telling us. Without this the
    model would under-admit forever after one lost dispatch."""
    feed = _Feed(_snap(free_cells=20_000))
    m = CapacityModel(feed)
    clock = {"t": 0.0}
    m._now = lambda: clock["t"]
    m._reservation_settle_s = 30.0
    m.reserve("a", est_kv=15_000)
    assert m.effective()[0] == 5_000
    clock["t"] = 31.0
    assert m.effective()[0] == 20_000


# ── server states that mean STOP ──────────────────────────────────────


def test_a_parked_engine_admits_nothing_however_free_it_looks():
    """A context rebuild leaves cells looking free. This flag is the only
    thing standing between a scheduler and dispatching into it."""
    m = _model(_snap(serving=False, free_cells=64_000, seats_free=4))
    v = m.admit("x", est_kv=100)
    assert not v.admitted and "not serving" in v.reason


def test_a_fatal_engine_admits_nothing():
    m = _model(_snap(engine_fatal="metal latch"))
    assert not m.admit("x", est_kv=100).admitted


def test_a_queued_server_means_stop_not_try_harder():
    """The server's admission queue is head-blocking by design, so work
    sent behind a queued request only lengthens its wait."""
    m = _model(_snap(waiting=2, free_cells=60_000))
    v = m.admit("x", est_kv=100)
    assert not v.admitted and "queue depth" in v.reason


def test_seat_busy_holds_everything_until_the_next_publish():
    """'All instances are busy' is a hard fact from the server and
    outranks any snapshot we hold."""
    m = _model(_snap(seats_free=4, seats_total=4, free_cells=60_000))
    assert m.admit("x", est_kv=100).admitted
    m.on_seat_busy()
    assert not m.admit("x", est_kv=100).admitted


# ── degradation ───────────────────────────────────────────────────────


def test_no_signal_degrades_to_the_pre_capacity_behaviour():
    """Width 1 is what the system did before any of this existed, so the
    worst case of the whole capacity stack is 'no faster than before' —
    never a stall."""
    m = _model(None)
    assert m.admit("x", est_kv=100).admitted
    m.reserve("x", est_kv=100)
    assert not m.admit("y", est_kv=100).admitted
    assert DEGRADED_WIDTH == 1


def test_legacy_snapshot_admits_on_seats_alone():
    """A server too old for the capacity type still knows its seats.
    Reading its unknown free_cells as 0 would stall every lane."""
    legacy = Snapshot(
        seats_total=4,
        seats_free=2,
        kv_pool_tokens=0,
        source="legacy",
        received_at=0.0,
    )
    assert not legacy.knows_kv
    m = _model(legacy)
    assert m.admit("x", est_kv=50_000).admitted, "seats govern when KV is unknown"


# ── draw estimation ───────────────────────────────────────────────────


def test_draw_counts_entitlement_and_discounts_the_shared_prefix():
    """max_tokens counts in FULL — the engine charges it from the moment
    of admission whether or not it is generated. Sizing against expected
    output instead is how the pool ended up 59% phantom."""
    # A real curator doc: ~40k chars of markdown against an 8k pack budget.
    draw = estimate_kv_draw(prompt_chars=40_000, max_tokens=8192)
    assert draw == (40_000 * 13) // 40 + 8192

    # The resident static prefix is forked, not duplicated, so charging it
    # over-bills every request by the same fixed amount.
    discounted = estimate_kv_draw(40_000, 8192, static_prefix_tokens=1765)
    assert discounted == draw - 1765

    # The discount cannot exceed the prompt itself — a short turn does not
    # earn negative cells just because the prefix is large.
    assert estimate_kv_draw(100, 512, static_prefix_tokens=10_000) == 512
