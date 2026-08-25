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
    +1 exactly would be wrong: that snapshot may have been in flight.

    The clock is advanced past _min_settle_s deliberately — the seq
    evidence alone is not sufficient (see the burst test below), so this
    exercises the seq rule in the regime where it is trustworthy.
    """
    feed = _Feed(_snap(seq=10, free_cells=20_000))
    m = CapacityModel(feed)
    clock = {"t": 0.0}
    m._now = lambda: clock["t"]
    m.reserve("a", est_kv=15_000)
    clock["t"] = 10.0  # past the dispatch -> admit floor

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


# ── exact sizing via the model's own tokenizer ────────────────────────


@pytest.mark.asyncio
async def test_exact_tokens_beat_the_char_heuristic_on_non_latin():
    """THE case the character estimate cannot get right.

    chars x 13/40 is a constant fitted to English prose. The same
    character count is a very different token count in CJK, so a
    translation lane sized by characters mis-reserves every non-Latin
    paper — in the direction that matters, since the pool is charged by
    entitlement and over-reserving is capacity nobody can use.
    """
    from agent.scheduler.capacity_model import estimate_kv_draw

    zh = "样品的光谱分析显示在532纳米处有明显吸收" * 50  # 950 chars
    guessed = estimate_kv_draw(len(zh), max_tokens=1000)
    exact = estimate_kv_draw(len(zh), max_tokens=1000, exact_prompt_tokens=1400)
    assert guessed != exact
    # The heuristic under-counts CJK badly; the exact path is authoritative.
    assert exact - 1000 == int(1400 * 1.10)


def test_margin_covers_the_template_the_client_cannot_see():
    """We tokenize the text we hold; the server wraps it in a chat
    template before decoding. The margin is for that wrapper."""
    from agent.scheduler.capacity_model import TOKENIZE_MARGIN, estimate_kv_draw

    d = estimate_kv_draw(0, max_tokens=0, exact_prompt_tokens=1000)
    assert d == int(1000 * TOKENIZE_MARGIN) > 1000


def test_static_prefix_is_discounted_on_the_exact_path_too():
    from agent.scheduler.capacity_model import estimate_kv_draw

    d = estimate_kv_draw(
        0, max_tokens=500, static_prefix_tokens=800, exact_prompt_tokens=1000
    )
    assert d == int(1000 * 1.10) - 800 + 500


@pytest.mark.asyncio
async def test_size_request_uses_the_server_when_it_answers():
    from agent.effects.mock import MockEffects
    from agent.scheduler.capacity_model import size_request

    fx = MockEffects()
    fx._token_counts = [4321]
    cells, how = await size_request(fx, "some prompt", max_tokens=2048)
    assert how == "exact"
    assert cells == int(4321 * 1.10) + 2048


@pytest.mark.asyncio
async def test_size_request_degrades_silently_in_value_but_loudly_in_label():
    """A server that will not tokenize must not block a dispatch — but the
    caller has to be able to SEE that it got the weaker answer, or a
    sizing regression becomes invisible."""
    from agent.effects.mock import MockEffects
    from agent.scheduler.capacity_model import size_request

    fx = MockEffects()  # no canned counts: the degrade branch
    cells, how = await size_request(fx, "x" * 4000, max_tokens=2048)
    assert how == "estimated"
    assert cells == (4000 * 13) // 40 + 2048


@pytest.mark.asyncio
async def test_a_raising_tokenizer_still_yields_a_size():
    from agent.scheduler.capacity_model import size_request

    class _Boom:
        async def token_count(self, texts, model=""):
            raise ConnectionError("server down")

    cells, how = await size_request(_Boom(), "abc" * 100, max_tokens=64)
    assert how == "estimated" and cells > 0


def test_a_reservation_cannot_clear_before_our_request_could_be_admitted():
    """MEASURED REGRESSION. The seq rule reads 'a publish cycle passed, so
    our admit is counted' — but `seq` is moved by EVERY seat, and observed
    publishes came as little as 46 ms apart. Without a floor, a burst of
    other streams' activity clears our reservation before our own request
    reaches the engine, and the same cells go out twice exactly when the
    pool is busiest."""
    from agent.effects.capacity import Snapshot
    from agent.scheduler.capacity_model import CapacityModel

    class _F:
        def __init__(self, s):
            self._s = s

        def snapshot(self):
            return self._s

        def set(self, s):
            self._s = s

    def snap(seq, free):
        return Snapshot(
            seq=seq,
            serving=True,
            seats_total=4,
            seats_free=4,
            kv_pool_tokens=65536,
            free_cells=free,
            min_admit_budget=512,
            source="ws",
            received_at=0.0,
        )

    feed = _F(snap(10, 20_000))
    m = CapacityModel(feed)
    clock = {"t": 0.0}
    m._now = lambda: clock["t"]
    m._min_settle_s = 5.0
    m.reserve("a", est_kv=15_000)

    # A burst: seq races ahead in 100ms because other seats are churning.
    clock["t"] = 0.1
    feed.set(snap(14, 5_000))
    assert m.effective()[0] == 0, "held — our request cannot have landed yet"

    # Past the round-trip floor, the seq evidence is trustworthy.
    clock["t"] = 6.0
    assert m.effective()[0] == 5_000


def test_work_served_off_the_text_pool_is_not_refused_for_want_of_a_seat():
    """CAUGHT ON THE FIRST MILEAGE RUN. Seats are the batched TEXT pool's
    limit. paddle OCR runs on its own device and figure reads run on
    muse's separate vision contexts, so gating them on text-seat
    availability starves one GPU on the other's contention — the live
    report read 'ocr: no free seat' while paddle sat idle."""
    m = _model(_snap(seats_free=0, seats_total=4, free_cells=60_000))
    assert not m.admit("curate", est_kv=1000, seats=1).admitted
    v = m.admit("ocr", est_kv=0, seats=0)
    assert v.admitted, v.reason


def test_a_seatless_lane_is_still_bounded_by_free_cells():
    """seats=0 means 'not a text seat', not 'unbounded'. A lane that
    genuinely draws KV must still fit."""
    m = _model(_snap(seats_free=0, free_cells=500))
    assert not m.admit("odd", est_kv=10_000, seats=0).admitted


def test_work_that_opens_no_text_stream_is_not_bound_by_text_cells():
    """The seat fix's twin, one check further down. paddle OCR allocates
    nothing in the batched text cell, so charging it min_admit_budget
    refuses it precisely when muse is busy — observed live as
    'ocr: needs 512 cells, 0 free' while paddle had capacity to spare."""
    m = _model(_snap(seats_free=0, free_cells=0, min_admit_budget=512))
    # A text lane is correctly refused.
    assert not m.admit("curate", est_kv=8000, seats=1).admitted
    # A lane served entirely elsewhere is not.
    v = m.admit("ocr", est_kv=0, seats=0)
    assert v.admitted, v.reason


def test_a_lane_that_draws_any_kv_is_still_bounded():
    """est_kv=0 AND seats=0 is the escape hatch; either one non-zero means
    the unit really does touch the pool."""
    m = _model(_snap(seats_free=0, free_cells=100, min_admit_budget=512))
    assert not m.admit("odd", est_kv=5_000, seats=0).admitted
    assert not m.admit("odd2", est_kv=0, seats=1).admitted


# ── claims may be corrected downward (2026-08-25) ────────────────────


def test_a_claim_may_be_corrected_downward_only():
    """Shrinking hands cells back to sibling lanes. Growing would be a
    SECOND admission decision, and this model is an optimizer with the
    engine as the authority — a claim that could grow would be a client
    quietly re-admitting itself."""
    m = _model(_snap(free_cells=60_000))
    tok = m.reserve("curate", 50_000, 1)
    m.resize(tok, 20_000)
    assert m._pending[tok].est_kv == 20_000
    m.resize(tok, 45_000)  # upward: ignored
    assert m._pending[tok].est_kv == 20_000
    m.resize(tok, 0)  # nonsense: ignored
    assert m._pending[tok].est_kv == 20_000


def test_resize_frees_cells_for_a_sibling_lane():
    m = _model(_snap(free_cells=60_000))
    tok = m.reserve("curate", 50_000, 1)
    before_cells, _ = m.effective()
    m.resize(tok, 20_000)
    after_cells, _ = m.effective()
    assert after_cells == before_cells + 30_000


def test_resize_on_an_unknown_token_is_a_no_op():
    m = _model(_snap(free_cells=60_000))
    m.resize("not-a-token", 1_000)  # must not raise
