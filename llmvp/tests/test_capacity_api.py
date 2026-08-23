"""The GraphQL capacity surface: one type, two deliveries.

`Subscription.capacity` pushes and `health { capacity }` returns the SAME
type built from the SAME snapshot, so a scheduler cannot get one answer
from the push path and a contradicting one from the poll fallback. These
tests pin that, plus the two lifecycle properties a long-lived subscriber
depends on: an immediate first frame, and deregistration on disconnect.
"""

from __future__ import annotations

import asyncio

import pytest

import api.graphql_api as gql
from inference.capacity import BUS, CapacityBus


@pytest.fixture(autouse=True)
def _fresh_bus(monkeypatch):
    """Each test gets its own bus — the module singleton is process-wide."""
    bus = CapacityBus()
    monkeypatch.setattr("inference.capacity.BUS", bus)
    return bus


FIELDS = {
    "serving": True,
    "seats_total": 4,
    "seats_free": 3,
    "free_cells": 41_000,
    "live_occupancy": 20_000,
    "waiting": 0,
}


def test_schema_exposes_capacity_on_both_paths():
    sdl = str(gql.schema)
    assert "type Capacity" in sdl
    # Nullable on health (an older backend has none), non-null on the
    # subscription (a frame is always a real snapshot).
    assert "capacity: Capacity\n" in sdl
    assert "capacity: Capacity!" in sdl


def test_health_capacity_prefers_backend_seat_truth(_fresh_bus):
    """The engine derives seats from stream occupancy; the backend owns
    the queue a request must actually win. Health reports the latter."""
    _fresh_bus.publish(dict(FIELDS))
    cap = gql._health_capacity(
        {"available_instances": 1, "pool_size": 4, "checked_out": 3}
    )
    assert cap is not None
    assert cap.seats_free == 1, "backend availability wins"
    assert cap.free_cells == 41_000, "KV still comes from the engine snapshot"


def test_health_capacity_is_none_before_any_publish(_fresh_bus):
    """None, not zeros — a zeroed capacity reads as 'server full' and
    would stall a scheduler that has no other signal."""
    assert gql._health_capacity({"available_instances": 4}) is None


def test_health_capacity_survives_a_broken_bus(monkeypatch):
    class _Boom:
        @property
        def latest(self):
            raise RuntimeError("bus exploded")

    monkeypatch.setattr("inference.capacity.BUS", _Boom())
    assert gql._health_capacity({"available_instances": 4}) is None


@pytest.mark.asyncio
async def test_subscription_yields_current_snapshot_first(_fresh_bus):
    """A scheduler connecting to an IDLE server must learn capacity
    immediately, not block until something happens to change."""
    _fresh_bus.publish(dict(FIELDS))
    agen = gql.Subscription().capacity()
    first = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
    assert first.free_cells == 41_000
    assert first.seats_total == 4
    await agen.aclose()


@pytest.mark.asyncio
async def test_subscription_pushes_changes_then_unregisters(_fresh_bus):
    agen = gql.Subscription().capacity()
    _fresh_bus.publish(dict(FIELDS))
    await asyncio.wait_for(agen.__anext__(), timeout=2.0)  # first frame
    assert _fresh_bus.subscriber_count == 1

    _fresh_bus.publish({**FIELDS, "free_cells": 9_000, "waiting": 2})
    await asyncio.sleep(0)
    nxt = await asyncio.wait_for(agen.__anext__(), timeout=2.0)
    assert nxt.free_cells == 9_000 and nxt.waiting == 2

    await agen.aclose()
    assert _fresh_bus.subscriber_count == 0, "finally must unregister"


@pytest.mark.asyncio
async def test_subscription_closes_cleanly_at_the_subscriber_cap(
    _fresh_bus, monkeypatch
):
    """Refusing must end the stream, not hang a client waiting forever."""
    monkeypatch.setattr(_fresh_bus, "subscribe", lambda loop: None)
    agen = gql.Subscription().capacity()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(agen.__anext__(), timeout=2.0)


def test_snapshot_mapping_covers_every_scalar_field(_fresh_bus):
    """Field-by-field mapping means a new dataclass field is invisible on
    the wire until someone adds it here — this test names that cost."""
    from inference.capacity import CapacitySnapshot

    snap = CapacitySnapshot(seq=7, free_cells=123, waiting=4, engine_fatal="x")
    cap = gql._capacity_from_snapshot(snap)
    assert (cap.seq, cap.free_cells, cap.waiting, cap.engine_fatal) == (
        7,
        123,
        4,
        "x",
    )
    scalars = {
        f
        for f, t in CapacitySnapshot.__dataclass_fields__.items()
        if f != "seats_by_persona"
    }
    missing = {f for f in scalars if not hasattr(cap, f)}
    assert not missing, f"unmapped snapshot fields: {missing}"
