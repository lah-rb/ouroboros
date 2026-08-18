"""The capacity feed: the wire protocol, and the ladder under it.

Driven through the REAL loop with the timing knobs zeroed and the socket
replaced at a seam — the discipline from tests/test_health_watchdog.py,
which exists because a watchdog whose loop can only be tested by waiting
does not get tested.

The properties under test are the failure ones. A capacity feed that
works when the server is healthy is worth little; what matters is that
losing the subscription degrades to polling, an old server degrades to
seats-only, and losing everything degrades to the behaviour the system
had before any of this was written.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.effects.capacity import CapacityFeed, Snapshot

CAP_PAYLOAD = {
    "seq": 7,
    "serving": True,
    "decodeMode": "batched",
    "seatsTotal": 4,
    "seatsFree": 3,
    "kvPoolTokens": 65536,
    "freeCells": 41000,
    "liveOccupancy": 24000,
    "minAdmitBudget": 512,
    "staticPrefixTokens": 1765,
    "waiting": 0,
}


class _FakeConn:
    """A graphql-transport-ws server, scripted."""

    def __init__(self, frames, *, ack=True):
        self._frames = list(frames)
        self._ack = ack
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        return json.dumps(
            {"type": "connection_ack"} if self._ack else {"type": "connection_error"}
        )

    def __aiter__(self):
        async def gen():
            for f in self._frames:
                yield json.dumps(f)

        return gen()

    async def close(self):
        self.closed = True


def _feed(conn=None, **kw):
    feed = CapacityFeed("http://127.0.0.1:8008/graphql", **kw)
    feed._connect_timeout_s = 0
    feed._backoff_initial_s = 0
    feed._backoff_max_s = 0
    feed._poll_fallback_s = 0
    if conn is not None:
        feed._connect = lambda: _ready(conn)
    return feed


async def _ready(value):
    return value


# ── the wire ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handshake_then_snapshot():
    conn = _FakeConn(
        [{"type": "next", "id": "cap", "payload": {"data": {"capacity": CAP_PAYLOAD}}}]
    )
    feed = _feed(conn)
    await feed._subscribe_once()

    kinds = [m["type"] for m in conn.sent]
    assert kinds[0] == "connection_init" and kinds[1] == "subscribe"
    snap = feed.snapshot()
    assert snap is not None
    assert (snap.free_cells, snap.seats_free, snap.seq) == (41000, 3, 7)
    assert snap.source == "ws"
    assert conn.closed, "the connection is closed on the way out"


@pytest.mark.asyncio
async def test_ping_is_answered_with_pong():
    """An unanswered ping ends the connection and costs a reconnect."""
    conn = _FakeConn([{"type": "ping"}, {"type": "complete"}])
    feed = _feed(conn)
    await feed._subscribe_once()
    assert {"type": "pong"} in conn.sent


@pytest.mark.asyncio
async def test_missing_ack_is_an_error_not_a_hang():
    conn = _FakeConn([], ack=False)
    feed = _feed(conn)
    with pytest.raises(RuntimeError, match="connection_ack"):
        await feed._subscribe_once()


@pytest.mark.asyncio
async def test_schema_error_raises_so_the_ladder_can_degrade():
    """An old server errors on the unknown `capacity` field. That is a
    version gap, not a transient — reconnecting forever would starve the
    scheduler of the fallback it could have had immediately."""
    conn = _FakeConn(
        [{"type": "error", "id": "cap", "payload": [{"message": "no such field"}]}]
    )
    feed = _feed(conn)
    with pytest.raises(RuntimeError, match="subscription error"):
        await feed._subscribe_once()


# ── the ladder ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_falls_back_to_polling_then_to_legacy():
    polled = {"n": 0}

    async def post(query):
        polled["n"] += 1
        return {"data": {"health": {"capacity": {**CAP_PAYLOAD, "freeCells": 100}}}}

    feed = _feed(post_fn=post)
    await feed._fallback_once()
    snap = feed.snapshot()
    assert snap.source == "poll" and snap.free_cells == 100
    assert polled["n"] == 1

    # Now the poll path stops answering; legacy pool_health carries seats.
    async def dead_post(query):
        raise ConnectionError("down")

    async def pool_health():
        return {"kvPoolTokens": 65536, "poolSize": 4, "availableInstances": 2}

    feed2 = _feed(post_fn=dead_post, pool_health_fn=pool_health)
    await feed2._fallback_once()
    snap2 = feed2.snapshot()
    assert snap2.source == "legacy"
    assert snap2.seats_free == 2
    assert not snap2.knows_kv, "legacy cannot know free cells and must say so"


@pytest.mark.asyncio
async def test_total_failure_leaves_no_snapshot_rather_than_zeros():
    """Zeros would read as 'server full' and stall every lane. None makes
    the caller use its own conservative default instead."""

    async def dead(*a, **k):
        raise ConnectionError("down")

    feed = _feed(post_fn=dead, pool_health_fn=dead)
    await feed._fallback_once()
    assert feed.snapshot() is None


@pytest.mark.asyncio
async def test_backoff_grows_and_is_bounded():
    """Recorded, not slept — the loop's timing is asserted directly."""
    slept: list[float] = []

    feed = CapacityFeed("http://x/graphql")
    feed._backoff_initial_s = 1.0
    feed._backoff_max_s = 4.0
    feed._sleep = lambda s: _record(slept, s)
    attempts = {"n": 0}

    async def always_fail():
        attempts["n"] += 1
        if attempts["n"] > 5:
            feed._stopping = True
        raise ConnectionError("nope")

    feed._subscribe_once = always_fail
    await feed._run()
    assert slept == [1.0, 2.0, 4.0, 4.0, 4.0], slept


async def _record(bucket, seconds):
    bucket.append(seconds)


# ── staleness ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stale_snapshot_reads_as_no_snapshot():
    """Acting on a stale capacity picture is worse than admitting we have
    none: one dispatches confidently into the past, the other degrades."""
    feed = _feed()
    feed._snapshot_stale_s = 5.0
    feed._publish(Snapshot(free_cells=1000, source="ws", received_at=0.0))
    import time as _t

    assert feed._snapshot is not None
    # received_at 0.0 against a monotonic clock well past 5s
    assert _t.monotonic() > 5.0
    assert feed.snapshot() is None


@pytest.mark.asyncio
async def test_wait_for_change_wakes_on_the_next_snapshot():
    """A worker that could not be admitted waits on capacity FREEING —
    which is a stream retiring — rather than on a timer."""
    feed = _feed()
    waiter = asyncio.create_task(feed.wait_for_change(timeout=2.0))
    await asyncio.sleep(0)
    import time as _t

    feed._publish(Snapshot(free_cells=999, source="ws", received_at=_t.monotonic()))
    got = await waiter
    assert got is not None and got.free_cells == 999
