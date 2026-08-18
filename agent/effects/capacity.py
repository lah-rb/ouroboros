"""Serving-capacity feed — what LLMVP can accept, learned over the wire.

WHY A SUBSCRIPTION AND NOT A SHARED OBJECT. The agent and the inference
server are separate programs with a service boundary between them, and
that boundary is the thing that makes multi-process and multi-machine
operation possible later. A local semaphore mirroring the server's seats
would be faster to write and would quietly make this one program in two
pieces: it would assume it is the only client, and it would be wrong the
first time a second agent connected. Capacity is therefore something the
server SAYS, over graphql-transport-ws, and never something this process
infers about the server's internals.

WHY A SUBSCRIPTION AND NOT A POLL. Capacity changes when a stream is
admitted or retires — moments the server knows exactly and a client can
only guess at. A poller either runs hot (a request per interval to learn
nothing) or runs cold (dispatching against a stale picture). Neither
error is necessary when the server can simply push.

DEGRADATION, NOT STALLING. Everything below has a rung under it:

    subscription  ->  poll health { capacity }  ->  legacy pool_health
                  ->  no signal at all (caller falls back to width 1)

The last rung is exactly today's behaviour, so the worst case of this
whole module is "no faster than before", never a stall. Every rung
transition logs at WARNING: silent degradation is how a 48k-token gate
went on quietly guarding a 131k pool.

Timing knobs are instance attributes, not module constants, so tests
drive the real loop with them set to zero (TESTING.md; the pattern comes
from InferenceEffect's health watchdog).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# The subscription document. Sent as a string for the same reason the rest
# of this codebase does (agent/effects/inference.py): there is no schema
# client here, and adding one to send six lines of GraphQL is not a trade
# worth making.
CAPACITY_SUBSCRIPTION = """
subscription Capacity {
  capacity {
    seq serving decodeMode
    seatsTotal seatsFree seatsCheckedOut
    kvPoolTokens freeCells liveOccupancy pinnedOccupancy
    poolSlack minAdmitBudget nCtxSeq staticPrefixTokens
    activeStreams waiting prefillBudget engineFatal
  }
}
"""

CAPACITY_HEALTH_QUERY = """
query CapacityHealth {
  health {
    status
    capacity {
      seq serving decodeMode
      seatsTotal seatsFree seatsCheckedOut
      kvPoolTokens freeCells liveOccupancy pinnedOccupancy
      poolSlack minAdmitBudget nCtxSeq staticPrefixTokens
      activeStreams waiting prefillBudget engineFatal
    }
  }
}
"""


@dataclass
class Snapshot:
    """One capacity reading, plus how we came by it.

    `source` and `received_at` are local metadata, not server fields: a
    scheduler must be able to tell a pushed snapshot from a polled one and
    a fresh one from a stale one, and neither question is answerable from
    the server's payload alone.
    """

    seq: int = 0
    serving: bool = True
    decode_mode: str = ""
    seats_total: int = 0
    seats_free: int = 0
    seats_checked_out: int = 0
    kv_pool_tokens: int = 0
    free_cells: int = 0
    live_occupancy: int = 0
    pinned_occupancy: int = 0
    pool_slack: int = 0
    min_admit_budget: int = 512
    n_ctx_seq: int = 0
    static_prefix_tokens: int = 0
    active_streams: int = 0
    waiting: int = 0
    prefill_budget: int = 0
    engine_fatal: Optional[str] = None

    source: str = "none"  # "ws" | "poll" | "legacy" | "none"
    received_at: float = 0.0

    @classmethod
    def from_graphql(cls, payload: dict, source: str, now: float) -> "Snapshot":
        def i(key: str, default: int = 0) -> int:
            try:
                return int(payload.get(key) or default)
            except (TypeError, ValueError):
                return default

        return cls(
            seq=i("seq"),
            serving=bool(payload.get("serving", True)),
            decode_mode=str(payload.get("decodeMode") or ""),
            seats_total=i("seatsTotal"),
            seats_free=i("seatsFree"),
            seats_checked_out=i("seatsCheckedOut"),
            kv_pool_tokens=i("kvPoolTokens"),
            free_cells=i("freeCells"),
            live_occupancy=i("liveOccupancy"),
            pinned_occupancy=i("pinnedOccupancy"),
            pool_slack=i("poolSlack"),
            min_admit_budget=i("minAdmitBudget", 512),
            n_ctx_seq=i("nCtxSeq"),
            static_prefix_tokens=i("staticPrefixTokens"),
            active_streams=i("activeStreams"),
            waiting=i("waiting"),
            prefill_budget=i("prefillBudget"),
            engine_fatal=payload.get("engineFatal"),
            source=source,
            received_at=now,
        )

    @classmethod
    def from_legacy_pool_health(cls, health: dict, now: float) -> "Snapshot":
        """A server that predates the capacity type still reports seats and
        the pool size. free_cells is UNKNOWABLE from those, so it is left
        at 0 and `source` says why — a caller must treat legacy as
        seats-only and not read 0 free cells as "server full"."""
        try:
            pool = int(health.get("kvPoolTokens") or 0)
        except (TypeError, ValueError):
            pool = 0
        try:
            seats = int(health.get("poolSize") or 0)
            free = int(health.get("availableInstances") or 0)
        except (TypeError, ValueError):
            seats = free = 0
        return cls(
            serving=str(health.get("status") or "") in ("ok", "healthy", ""),
            seats_total=seats,
            seats_free=free,
            kv_pool_tokens=pool,
            source="legacy",
            received_at=now,
        )

    @property
    def knows_kv(self) -> bool:
        """Whether free_cells is a real measurement. False for legacy."""
        return self.source in ("ws", "poll") and self.kv_pool_tokens > 0


class CapacityFeed:
    """Keeps a current Snapshot, by whatever means still work."""

    def __init__(
        self,
        graphql_url: str,
        *,
        pool_health_fn: Optional[Callable[[], Any]] = None,
        post_fn: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.graphql_url = graphql_url
        # Injected rather than imported so tests need no server and no
        # HTTP: the fallback rungs are the part most likely to be wrong,
        # so they have to be the easiest part to exercise.
        self._pool_health_fn = pool_health_fn
        self._post_fn = post_fn

        self._snapshot: Optional[Snapshot] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._source = "none"
        self._waiters: list[asyncio.Future] = []

        # Timing knobs — instance attributes so tests can zero them.
        self._connect_timeout_s = 10.0
        self._backoff_initial_s = 1.0
        self._backoff_max_s = 30.0
        self._poll_fallback_s = 5.0
        self._snapshot_stale_s = 15.0

        self._backoff = self._backoff_initial_s
        self._sleep = asyncio.sleep  # seam: tests record instead of sleeping

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run(), name="capacity-feed")

    async def stop(self) -> None:
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # -- reading ---------------------------------------------------------

    def snapshot(self) -> Optional[Snapshot]:
        """The freshest reading, or None if it is too old to act on.

        A STALE SNAPSHOT IS MORE DANGEROUS THAN NONE: None makes a caller
        fall back to its conservative default, while a stale one makes it
        confidently dispatch against a picture of the past.
        """
        snap = self._snapshot
        if snap is None:
            return None
        if time.monotonic() - snap.received_at > self._snapshot_stale_s:
            return None
        return snap

    @property
    def source(self) -> str:
        return self._source

    def _publish(self, snap: Snapshot) -> None:
        self._snapshot = snap
        if snap.source != self._source:
            logger.warning(
                "capacity feed: %s -> %s (free_cells=%s seats=%s/%s)",
                self._source,
                snap.source,
                snap.free_cells if snap.knows_kv else "unknown",
                snap.seats_free,
                snap.seats_total,
            )
            self._source = snap.source
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(snap)
        self._waiters.clear()

    async def wait_for_change(self, timeout: float) -> Optional[Snapshot]:
        """Block until the next snapshot arrives. The scheduler's wake-up:
        capacity freeing IS a stream retiring, so a worker that could not
        be admitted waits on this rather than on a timer."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    # -- the ladder ------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._subscribe_once()
                self._backoff = self._backoff_initial_s  # clean disconnect
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — every failure degrades
                logger.warning(
                    "capacity subscription lost (%s: %s); falling back",
                    type(exc).__name__,
                    str(exc)[:120],
                )
                await self._fallback_once()
                if self._stopping:
                    # Do not spend a backoff we have already decided not to
                    # use — shutdown would otherwise wait out the full
                    # interval before noticing.
                    break
                await self._sleep(self._backoff)
                self._backoff = min(self._backoff * 2, self._backoff_max_s)

    async def _ws_url(self) -> str:
        url = self.graphql_url
        if url.startswith("https://"):
            return "wss://" + url[len("https://") :]
        if url.startswith("http://"):
            return "ws://" + url[len("http://") :]
        return url

    async def _connect(self):
        import websockets

        return await websockets.connect(
            await self._ws_url(),
            subprotocols=["graphql-transport-ws"],
            open_timeout=self._connect_timeout_s,
        )

    async def _subscribe_once(self) -> None:
        """One connection's lifetime, speaking graphql-transport-ws.

        Hand-rolled on purpose (see the module docstring): the protocol is
        six message types, while the reconnect policy wrapped around it is
        the part that needs to be ours.
        """
        conn = await self._connect()
        try:
            await conn.send(json.dumps({"type": "connection_init", "payload": {}}))
            ack = json.loads(await conn.recv())
            if ack.get("type") != "connection_ack":
                raise RuntimeError(f"no connection_ack (got {ack.get('type')!r})")
            await conn.send(
                json.dumps(
                    {
                        "id": "cap",
                        "type": "subscribe",
                        "payload": {"query": CAPACITY_SUBSCRIPTION},
                    }
                )
            )
            async for raw in conn:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "next":
                    data = ((msg.get("payload") or {}).get("data") or {}).get(
                        "capacity"
                    )
                    if data:
                        self._publish(
                            Snapshot.from_graphql(data, "ws", time.monotonic())
                        )
                elif kind == "error":
                    # A schema error means this server has no capacity
                    # field — a version gap, not a transient. Drop straight
                    # to the fallback rung instead of reconnecting forever.
                    raise RuntimeError(f"subscription error: {msg.get('payload')}")
                elif kind == "complete":
                    return
                elif kind == "ping":
                    await conn.send(json.dumps({"type": "pong"}))
        finally:
            try:
                await conn.close()
            except Exception:  # noqa: BLE001
                pass

    async def _fallback_once(self) -> None:
        """Rung 2 then rung 3. Never raises — this IS the failure path."""
        if self._post_fn is not None:
            try:
                data = await self._post_fn(CAPACITY_HEALTH_QUERY)
                cap = (((data or {}).get("data") or {}).get("health") or {}).get(
                    "capacity"
                )
                if cap:
                    self._publish(Snapshot.from_graphql(cap, "poll", time.monotonic()))
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("capacity poll failed: %s", exc)

        if self._pool_health_fn is not None:
            try:
                health = await self._pool_health_fn()
                if health:
                    self._publish(
                        Snapshot.from_legacy_pool_health(health, time.monotonic())
                    )
                    return
            except Exception as exc:  # noqa: BLE001
                logger.debug("legacy pool health failed: %s", exc)
        # Rung 4: nothing. snapshot() will go stale and return None, which
        # is the caller's signal to use its conservative default.
