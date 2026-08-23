"""The capacity bus: multi-subscriber fan-out from the decode thread.

Pins the properties that make it safe to publish from inside the decode
loop — every one of them is a past incident in this codebase or a design
rule the alternative implementation would have broken:

  * TWO subscribers both receive. SessionState holds exactly ONE listener
    (core/session_manager.py:127) and a second subscriber silently evicts
    the first; a capacity bus copied from that shape would break the
    moment a second scheduler connected.
  * A slow consumer gets the NEWEST snapshot, never a backlog. Acting on
    a stale capacity value is worse than having none.
  * A publish never raises into the caller — not against a closed loop,
    not against no subscribers, not against a full bus.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from inference.capacity import (
    MAX_SUBSCRIBERS,
    CapacityBus,
    CapacitySnapshot,
)

FIELDS = {"seats_total": 4, "seats_free": 2, "free_cells": 40_000}


@pytest.mark.asyncio
async def test_two_subscribers_both_receive():
    """The property SessionState.listener does NOT have."""
    bus = CapacityBus()
    bus.bind_loop(asyncio.get_running_loop())
    a = bus.subscribe(asyncio.get_running_loop())
    b = bus.subscribe(asyncio.get_running_loop())
    assert a is not None and b is not None

    bus.publish(dict(FIELDS))
    await asyncio.sleep(0)  # let call_soon_threadsafe callbacks run

    sa, sb = await a.get(), await b.get()
    assert sa.seq == sb.seq == 1
    assert sa.free_cells == sb.free_cells == 40_000
    assert bus.subscriber_count == 2


@pytest.mark.asyncio
async def test_slow_consumer_gets_latest_not_backlog():
    bus = CapacityBus()
    bus.bind_loop(asyncio.get_running_loop())
    mb = bus.subscribe(asyncio.get_running_loop())

    for free in (10_000, 20_000, 30_000):
        bus.publish({**FIELDS, "free_cells": free})
    await asyncio.sleep(0)

    snap = await mb.get()
    assert snap.free_cells == 30_000, "must be the newest, not the oldest"
    assert snap.seq == 3
    assert mb.dropped >= 1, "skipped snapshots are counted, not hidden"


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery_and_frees_the_slot():
    bus = CapacityBus()
    bus.bind_loop(asyncio.get_running_loop())
    mb = bus.subscribe(asyncio.get_running_loop())
    bus.unsubscribe(mb)
    assert bus.subscriber_count == 0
    bus.publish(dict(FIELDS))
    await asyncio.sleep(0)
    # latest() still tracks state for the poll fallback even with no
    # subscribers — that is what makes Query.health work.
    assert bus.latest().free_cells == 40_000


@pytest.mark.asyncio
async def test_publish_from_a_real_thread_reaches_the_loop():
    """The decode thread is a plain threading.Thread; delivery must hop."""
    bus = CapacityBus()
    bus.bind_loop(asyncio.get_running_loop())
    mb = bus.subscribe(asyncio.get_running_loop())

    t = threading.Thread(target=lambda: bus.publish({**FIELDS, "free_cells": 777}))
    t.start()
    t.join()

    snap = await asyncio.wait_for(mb.get(), timeout=2.0)
    assert snap.free_cells == 777


def test_publish_never_raises_without_a_loop_or_subscribers():
    """Startup and shutdown both hit this path; decode must not care."""
    bus = CapacityBus()
    bus.publish(dict(FIELDS))  # no loop bound yet
    assert bus.latest().seats_total == 4

    loop = asyncio.new_event_loop()
    bus.bind_loop(loop)
    mb = bus.subscribe(loop)
    assert mb is not None
    loop.close()
    bus.publish(dict(FIELDS))  # loop closed underneath us


@pytest.mark.asyncio
async def test_subscriber_cap_refuses_rather_than_leaking():
    bus = CapacityBus()
    loop = asyncio.get_running_loop()
    bus.bind_loop(loop)
    boxes = [bus.subscribe(loop) for _ in range(MAX_SUBSCRIBERS)]
    assert all(b is not None for b in boxes)
    assert bus.subscribe(loop) is None
    bus.unsubscribe(boxes[0])
    assert bus.subscribe(loop) is not None


def test_snapshot_is_frozen_and_serializable():
    snap = CapacitySnapshot(seq=1, free_cells=100)
    with pytest.raises(Exception):
        snap.free_cells = 2  # type: ignore[misc]
    assert snap.to_dict()["free_cells"] == 100
