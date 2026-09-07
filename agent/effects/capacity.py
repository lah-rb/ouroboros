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
from dataclasses import dataclass
from typing import Any, Callable, Optional

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
        # How long a snapshot survives AFTER the link drops. Short: once
        # disconnected we no longer learn about admits or retires.
        self._snapshot_stale_s = 15.0
        # Backstop for a connection that is open but delivering nothing.
        # Far above the measured 27.7 s median publish gap, because on a
        # push feed a long quiet period is normal and means "no change".
        # Read in TWO places that must agree: snapshot() ages the feed out
        # at this bound, and _subscribe_once bounds recv() by it so the
        # transport reconnects at the same moment (the 2026-08-26 stall
        # was the first half working without the second). 0 disables the
        # recv bound (tests).
        self._absolute_stale_s = 900.0

        self._connected = False
        self._disconnected_at: Optional[float] = None

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
        """The freshest reading, or None when we cannot vouch for it.

        AGE IS NOT STALENESS ON A PUSH FEED, and getting this wrong makes
        the whole capacity system inert. Capacity only changes when a
        stream is admitted or retires; through a network-bound discovery
        stretch nothing changes for minutes, and the server is right to
        say nothing. Measured beside the live mission over 25 minutes:
        median gap between publishes 27.7 s, and the original 15 s age cap
        would have declared the feed dead in 46 intervals out of 25
        minutes — every lane degrading to width 1 while the subscription
        sat there perfectly healthy.

        So what actually invalidates a snapshot is losing the CONNECTION:
        while we are connected, silence means "nothing changed" and the
        last frame is current by construction. Once disconnected we no
        longer know what we missed, and the age cap applies from the
        moment the link dropped.

        `_snapshot_stale_s` is kept as an absolute backstop against a
        connection that is nominally open but delivering nothing.
        """
        snap = self._snapshot
        if snap is None:
            return None
        now = time.monotonic()
        age = now - snap.received_at

        # A PUSHED frame on a live link: silence means nothing changed, so
        # only the backstop applies.
        if snap.source == "ws" and self._connected:
            return snap if age <= self._absolute_stale_s else None

        # A PULLED frame (poll or legacy) has no such guarantee — nobody
        # promised to tell us about the next change, so its freshness is
        # simply its age.
        if snap.source in ("poll", "legacy"):
            return snap if age <= self._snapshot_stale_s else None

        # A pushed frame whose link has since dropped: we stopped learning
        # about admits and retires at the drop, so age from there.
        since_drop = now - (self._disconnected_at or snap.received_at)
        return snap if since_drop <= self._snapshot_stale_s else None

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
            # Subscribed and listening: from here, silence means "nothing
            # changed" rather than "we have lost track".
            self._connected = True
            self._disconnected_at = None
            from websockets.exceptions import ConnectionClosedOK

            while True:
                # THE STALENESS WATCHDOG. `async for raw in conn` here is
                # what turned the 2026-08-26 half-open stall into a
                # permanent width-1 degradation: the server stopped
                # delivering to THIS socket while keeping it open (WS-level
                # pings still answered), so no exception ever fired and the
                # reconnect ladder below never ran — snapshot() correctly
                # aged the feed out via _absolute_stale_s and nothing acted
                # on it. A fresh subscriber on the same server received
                # frames immediately (seq 1196-1198), proving the fix is a
                # reconnect. Bounding recv() by the SAME constant closes
                # the loop: detection now reaches the transport, and the
                # push-feed philosophy is preserved — 900 s is ~32x the
                # measured median publish gap, so a healthy quiet link is
                # never churned.
                try:
                    raw = await asyncio.wait_for(
                        conn.recv(), timeout=self._absolute_stale_s or None
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"no frame for {self._absolute_stale_s:.0f}s with the "
                        "link open — server-side delivery stalled; reconnecting"
                    ) from None
                except ConnectionClosedOK:
                    return  # server closed cleanly (the old async-for exit)
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
            # Link down — we stop learning about admits and retires here,
            # which is the moment the snapshot starts aging out.
            if self._connected:
                self._connected = False
                self._disconnected_at = time.monotonic()
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
