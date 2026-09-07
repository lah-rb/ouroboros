"""Lane workers: no barrier, real backpressure, and a clean shutdown.

The property that matters most is the one the parallel step could not
give: a fast lane must NOT be held by a slow one. The live failure was a
26-minute paddle batch during which muse ran a single curate paper and
then idled, so the first test drives exactly that shape and asserts the
fast lane completes many units while the slow one runs once.
"""

from __future__ import annotations

import asyncio

import pytest

from agent.scheduler.capacity_model import CapacityModel
from agent.scheduler.worker_pool import Lane, WorkerPool, _did_work
from agent.effects.capacity import Snapshot


class _Feed:
    def __init__(self, snap):
        self._snap = snap

    def snapshot(self):
        return self._snap

    async def wait_for_change(self, timeout):
        await asyncio.sleep(0)
        return None


def _snap(**kw):
    base = dict(
        seq=1,
        serving=True,
        seats_total=4,
        seats_free=4,
        kv_pool_tokens=65536,
        free_cells=60_000,
        min_admit_budget=512,
        waiting=0,
        source="ws",
        received_at=0.0,
    )
    base.update(kw)
    return Snapshot(**base)


def _pool(lanes, runner, snap=None, **kw):
    """A pool whose flow execution is replaced at the seam."""
    model = CapacityModel(_Feed(snap if snap is not None else _snap()))
    model._now = lambda: 0.0
    pool = WorkerPool(effects=None, lanes=lanes, capacity_model=model, **kw)
    pool._run_flow = runner
    pool._admit_wait_s = 0
    pool._stop_grace_s = 1
    return pool


# ── the barrier is gone ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_slow_lane_does_not_hold_a_fast_one():
    """THE regression for the whole restructure.

    Live 2026-08-18: a 26-minute OCR batch of German dissertations shared
    a parallel window with the muse drains, so muse ran one unit and idled
    for the rest. Under lanes the fast one keeps going.
    """
    counts = {"slow": 0, "fast": 0}

    async def runner(lane):
        counts[lane.name] += 1
        await asyncio.sleep(0.20 if lane.name == "slow" else 0.005)
        return True

    lanes = [
        Lane(name="slow", flow="f", resource="paddle", idle_backoff_s=0.01),
        Lane(name="fast", flow="f", resource="vision_ctx", idle_backoff_s=0.01),
    ]
    pool = _pool(lanes, runner)
    async with pool:
        await asyncio.sleep(0.25)

    assert counts["slow"] <= 2, counts
    assert counts["fast"] >= 8, f"fast lane was held by the slow one: {counts}"


@pytest.mark.asyncio
async def test_resource_cap_limits_concurrency_per_resource_not_globally():
    """Two lanes on the same resource share its cap; a lane on another
    resource is unaffected."""
    live = {"text": 0, "peak": 0}

    async def runner(lane):
        if lane.resource == "text_seat":
            live["text"] += 1
            live["peak"] = max(live["peak"], live["text"])
            await asyncio.sleep(0.02)
            live["text"] -= 1
        else:
            await asyncio.sleep(0.001)
        return True

    lanes = [
        Lane(name="a", flow="f", resource="text_seat", idle_backoff_s=0.01),
        Lane(name="b", flow="f", resource="text_seat", idle_backoff_s=0.01),
        Lane(name="c", flow="f", resource="paddle", idle_backoff_s=0.01),
    ]
    pool = _pool(lanes, runner, max_inflight={"text_seat": 1, "paddle": 1})
    async with pool:
        await asyncio.sleep(0.12)
    assert live["peak"] == 1, "text_seat cap of 1 was exceeded"


# ── backpressure comes from the server ────────────────────────────────


@pytest.mark.asyncio
async def test_a_parked_server_dispatches_nothing():
    """serving=False is the rebuild window. Free cells mean nothing then."""
    ran = {"n": 0}

    async def runner(lane):
        ran["n"] += 1
        return True

    lanes = [
        Lane(
            name="curate",
            flow="f",
            resource="text_seat",
            est_kv=100,
            idle_backoff_s=0.01,
        )
    ]
    pool = _pool(lanes, runner, snap=_snap(serving=False))
    async with pool:
        await asyncio.sleep(0.06)
    assert ran["n"] == 0


@pytest.mark.asyncio
async def test_a_unit_too_large_for_the_pool_waits_instead_of_failing():
    ran = {"n": 0}

    async def runner(lane):
        ran["n"] += 1
        return True

    lanes = [
        Lane(
            name="curate",
            flow="f",
            resource="text_seat",
            est_kv=50_000,
            idle_backoff_s=0.01,
        )
    ]
    pool = _pool(lanes, runner, snap=_snap(free_cells=1_000))
    async with pool:
        await asyncio.sleep(0.06)
    assert ran["n"] == 0, "must wait for cells, not dispatch and be queued"


@pytest.mark.asyncio
async def test_reservations_are_released_even_when_a_unit_raises():
    """A leaked reservation permanently shrinks the pool this process
    believes in — worse than the failure that caused it."""

    async def boom(lane):
        raise RuntimeError("unit exploded")

    lanes = [
        Lane(
            name="curate",
            flow="f",
            resource="text_seat",
            est_kv=100,
            idle_backoff_s=0.01,
        )
    ]
    pool = _pool(lanes, boom)
    async with pool:
        await asyncio.sleep(0.05)
    assert pool.model.inflight == 0
    assert pool.state["curate"].units_failed > 0


# ── idleness ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_empty_queue_backs_off_instead_of_spinning():
    """A drain that declines did no work. Counting it as work would spin
    the lane at full speed against an empty databank."""
    calls = {"n": 0}

    async def declines(lane):
        calls["n"] += 1
        return False

    lanes = [Lane(name="ocr", flow="f", resource="paddle", idle_backoff_s=0.05)]
    pool = _pool(lanes, declines)
    async with pool:
        await asyncio.sleep(0.12)
    assert calls["n"] <= 4, f"spun instead of backing off: {calls['n']}"


def test_did_work_reads_the_drains_own_idleness_reports():
    assert _did_work({"attempted": 1}, {})
    assert _did_work({}, {"figtext_summary": {"figures": 12}})
    assert _did_work({}, {"translate_summary": {"chunks": 8}})
    assert not _did_work({"attempted": 0, "reason": "nothing pending"}, {})
    assert not _did_work({}, {"figtext_summary": {"figures": 0}})


# ── lifecycle ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_lets_an_inflight_unit_finish_booking():
    """A unit in flight holds a claim against real work. Killing it
    mid-book is how the batch form lost 350 pages."""
    finished = {"ok": False}

    async def slow_but_finishes(lane):
        await asyncio.sleep(0.05)
        finished["ok"] = True
        return True

    lanes = [Lane(name="ocr", flow="f", resource="paddle", idle_backoff_s=0.01)]
    pool = _pool(lanes, slow_but_finishes)
    pool.start()
    await asyncio.sleep(0.01)  # let the unit start
    await pool.stop()
    assert finished["ok"], "stop cut a unit off mid-flight"


@pytest.mark.asyncio
async def test_deadline_stops_dispatching():
    """The run's wall clock binds the pool too — otherwise a lane would
    keep working past the budget park the loop honours."""
    ran = {"n": 0}

    async def runner(lane):
        ran["n"] += 1
        return True

    lanes = [Lane(name="ocr", flow="f", resource="paddle", idle_backoff_s=0.01)]
    pool = _pool(lanes, runner, deadline=-1.0)  # already expired
    async with pool:
        await asyncio.sleep(0.05)
    assert ran["n"] == 0


@pytest.mark.asyncio
async def test_lanes_run_real_flows_through_child_effects(monkeypatch):
    """Workers run FLOWS, not actions: trace emission, the ownership
    contract and branch stamping all come free that way."""
    seen = {}

    async def fake_execute_flow(**kw):
        seen["flow"] = kw["flow_def"]
        seen["branch"] = kw["effects"]._branch
        from agent.models import FlowResult

        return FlowResult(status="success", result={"attempted": 1}, context={})

    monkeypatch.setattr("agent.runtime.execute_flow", fake_execute_flow)
    lane = Lane(name="ocr", flow="ocr_drain", resource="paddle")
    pool = WorkerPool(
        effects=object(),
        lanes=[lane],
        flow_registry={"ocr_drain": "FLOWDEF"},
        inputs={"working_directory": "/tmp/x"},
    )
    assert await pool._run_flow(lane) is True
    assert seen["flow"] == "FLOWDEF"
    assert seen["branch"] == "lane:ocr"


@pytest.mark.asyncio
async def test_a_failing_idle_wait_does_not_silently_kill_the_lane():
    """A lane task holds a reference in the pool, so asyncio never
    reports its exception — a raise in the idle path killed the lane
    SILENTLY and the pool went on looking alive with a dead lane inside
    it. Observed on the live v2 run as a pool that had stopped reporting
    while the controller kept working."""
    calls = {"n": 0}

    async def declines(lane):
        calls["n"] += 1
        return False

    lanes = [Lane(name="ocr", flow="f", resource="paddle", idle_backoff_s=0.01)]
    pool = _pool(lanes, declines)

    boom = {"n": 0}

    async def bad_idle(lane):
        boom["n"] += 1
        raise RuntimeError("idle exploded")

    pool._idle = bad_idle
    async with pool:
        await asyncio.sleep(0.08)
    # The lane kept going despite the idle path failing every time.
    assert boom["n"] >= 2, boom
    assert calls["n"] >= 2, "lane died on the first idle failure"


@pytest.mark.asyncio
async def test_the_report_loop_survives_a_failing_snapshot():
    """Telemetry that dies silently is worse than none: the resulting
    quiet is indistinguishable from a healthy idle pool."""
    pool = _pool([Lane(name="x", flow="f", resource="paddle")], lambda l: _true())
    pool._report_every_s = 0.01
    boom = {"n": 0}

    def bad_report():
        boom["n"] += 1
        raise RuntimeError("snapshot exploded")

    pool._emit_report = bad_report
    task = asyncio.create_task(pool._report_loop())
    await asyncio.sleep(0.06)
    pool._stopping.set()
    await asyncio.sleep(0.02)
    task.cancel()
    assert boom["n"] >= 2, f"report loop died after {boom['n']} failure(s)"


async def _true():
    return True


def test_attempted_is_not_accomplished():
    """THE 7-HOUR BUG. When the inference server died, the OCR drain kept
    reporting 'attempted 4' and failing instantly, and the lane counted
    each failure as work — 2,864 rounds against a dead machine. A round
    that DECLINED (drains set `reason`) or outright FAILED is not work."""
    assert not _did_work({"attempted": 4, "status": "failed"}, {})
    assert not _did_work({"attempted": 0, "reason": "nothing pending"}, {})
    assert not _did_work({}, {"ocr_summary": {"attempted": 8, "status": "failed"}})
    # A round that actually judged papers still counts.
    assert _did_work({"attempted": 4, "status": "partial"}, {})
    assert _did_work({"attempted": 1, "outcomes": [{"paper_key": "p"}]}, {})


@pytest.mark.asyncio
async def test_a_lane_cannot_spin_even_if_work_is_misclassified():
    """Belt and braces for the above: whatever the verdict says, a unit
    that returned in milliseconds did not do a document's worth of work.
    This is the backstop for a FUTURE classification bug, not a substitute
    for classifying correctly."""
    calls = {"n": 0}

    async def instant_success(lane):
        calls["n"] += 1
        return True  # lies: claims work, returns immediately

    lanes = [Lane(name="ocr", flow="f", resource="paddle", idle_backoff_s=0.05)]
    pool = _pool(lanes, instant_success)
    pool._min_unit_s = 1.0
    async with pool:
        await asyncio.sleep(0.25)
    # Without the floor this would run hundreds of times.
    assert calls["n"] <= 8, f"lane spun {calls['n']} times despite the rate floor"


# ── capacity telemetry ────────────────────────────────────────────────


class _RecordingEffects:
    """Minimal effects stand-in that captures emitted trace events."""

    def __init__(self, boom: bool = False):
        self.events: list = []
        self._boom = boom
        self._traced_mission_id = "m1"

    async def emit_trace(self, event):
        if self._boom:
            raise RuntimeError("trace backend down")
        self.events.append(event)


@pytest.mark.asyncio
async def test_capacity_sample_records_entitlement_oversubscription():
    """THE state this event exists for.

    Live 2026-08-19: seats_total=4, seats_free=2, free_cells=0 — two seats
    idle while two streams had entitled 67,045 cells of a 65,536 pool. The
    seat count alone says there is room; only the ratio shows why there
    isn't. free_cells clamps at 0, so the overshoot has to be carried as a
    ratio or it is lost.
    """
    eff = _RecordingEffects()
    snap = _snap(
        seats_total=4,
        seats_free=2,
        free_cells=0,
        kv_pool_tokens=65_536,
        live_occupancy=67_045,
        pinned_occupancy=0,
        active_streams=2,
    )
    pool = _pool([Lane(name="curate", flow="f", resource="text_seat")], None, snap=snap)
    pool.effects = eff

    await pool._emit_capacity_trace(pool.model._feed.snapshot())

    assert len(eff.events) == 1
    e = eff.events[0]
    assert e.event_type == "capacity_sample"
    assert e.seats_free == 2 and e.free_cells == 0
    assert e.occupancy_ratio > 1.0, "oversubscription must survive the clamp"
    assert e.cycle == -1, "out-of-band events must not claim cycle 0"
    assert e.mission_id == "m1"
    assert "curate" in e.lanes


@pytest.mark.asyncio
async def test_capacity_trace_failure_does_not_kill_the_report_loop():
    """Same rule as the log line: the thing that reports trouble must not
    become the trouble. A raise here previously had no guard at all."""
    pool = _pool([Lane(name="x", flow="f", resource="paddle")], None)
    pool.effects = _RecordingEffects(boom=True)
    # Must not raise.
    await pool._emit_capacity_trace(pool.model._feed.snapshot())


@pytest.mark.asyncio
async def test_capacity_trace_is_silent_without_effects_or_snapshot():
    """A degraded feed yields no snapshot; unit pools carry no effects.
    Neither is an error, and neither may emit a half-filled event."""
    pool = _pool([Lane(name="x", flow="f", resource="paddle")], None)
    pool.effects = None
    await pool._emit_capacity_trace(pool.model._feed.snapshot())  # no effects
    eff = _RecordingEffects()
    pool.effects = eff
    await pool._emit_capacity_trace(None)  # no snapshot
    assert eff.events == []


def test_network_lane_work_verbs_count_as_work():
    """Live 2026-08-21: biblio's first 8 rounds mined 90 papers and
    promoted 63 candidates while reporting 0d/8i — 'mined'/'promoted'/
    'recovered' are the network lanes' work verbs and must count."""
    assert _did_work({"mined": 10, "dois_total": 200}, {})
    assert _did_work({"promoted": 40, "backlog": 900}, {})
    assert _did_work({}, {"recover_summary": {"attempted": 6, "recovered": 2}})
    # A cap/decline round still reads as idle.
    assert not _did_work({"promoted": 0, "reason": "cap reached (2000/2000)"}, {})


# ── dynamic lanes claim what they were admitted against (2026-08-25) ──


def test_a_dynamic_lane_claims_the_pool_not_its_estimate():
    """est_kv is the ADMISSION GATE for a dynamic lane, not the draw. If the
    reservation stayed at est_kv while the doc budget authorised the whole
    pool, sibling lanes would admit against cells already spent — which is
    exactly the ~4x oversubscription behind the 2026-08-24/25 wedge."""
    from agent.scheduler.worker_pool import Lane

    lane = Lane(
        name="curate",
        flow="curate_drain",
        resource="text_seat",
        est_kv=18_000,
        seats=1,
        dynamic_kv=True,
    )
    assert lane.dynamic_kv is True
    static = Lane(name="ocr", flow="ocr_drain", resource="paddle")
    assert static.dynamic_kv is False


def test_the_claim_is_visible_inside_the_unit_and_cleared_after():
    from agent.scheduler.capacity_claim import Claim, claim_scope, current_claim

    assert current_claim() is None
    with claim_scope(Claim(tokens=55_000, lane="curate")):
        c = current_claim()
        assert c is not None and c.tokens == 55_000
    assert current_claim() is None


def test_the_claim_is_cleared_even_when_the_unit_raises():
    from agent.scheduler.capacity_claim import Claim, claim_scope, current_claim

    with pytest.raises(RuntimeError):
        with claim_scope(Claim(tokens=1_000, lane="curate")):
            raise RuntimeError("unit blew up")
    assert current_claim() is None


def test_a_claim_hands_back_only_downward():
    from agent.scheduler.capacity_claim import Claim

    class _M:
        def __init__(self):
            self.calls = []

        def resize(self, token, kv):
            self.calls.append((token, kv))

    m = _M()
    c = Claim(tokens=55_000, lane="curate", _model=m, _token="t1")
    c.resize(34_000)
    assert c.tokens == 34_000 and m.calls == [("t1", 34_000)]
    c.resize(50_000)  # upward: refused, model untouched
    assert c.tokens == 34_000 and m.calls == [("t1", 34_000)]


def test_a_claim_never_breaks_a_lane_when_the_model_raises():
    from agent.scheduler.capacity_claim import Claim

    class _Boom:
        def resize(self, token, kv):
            raise RuntimeError("model gone")

    c = Claim(tokens=55_000, lane="curate", _model=_Boom(), _token="t1")
    c.resize(20_000)  # must not raise
    assert c.tokens == 20_000
