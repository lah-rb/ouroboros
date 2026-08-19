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
