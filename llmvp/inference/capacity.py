"""Serving-capacity snapshots, published as they change.

WHY THIS EXISTS. A scheduler in another process needs to know what the
server can accept right now — free seats AND free KV cells — without
polling and without reaching into Python objects it does not own. The
numbers already exist inside BatchedEngine; the only thing missing was a
way to publish them across the process boundary. Everything here is a
transport for values computed elsewhere: this module computes nothing.

FOUR RULES, each paid for by a past incident.

1. THE KV HALF IS COMPUTED ON THE DECODE THREAD. `_live_occupancy`
   iterates `engine._streams`; calling it from the event loop while the
   decode thread admits or retires can raise "dictionary changed size
   during iteration". Snapshots are therefore built at the mutation
   points and handed to the loop already frozen.

2. CACHED INTS ONLY — never a live context dereference. Three server
   kills (`libllama!llama_n_ctx_seq`, KERN_INVALID_ADDRESS) are recorded
   at llama_cpp_backend.py:5263-5279, and tests/test_n_ctx_seq_health_read.py
   exists to keep health paths off that road. A capacity publish fires far
   more often than a health poll, so the rule binds harder here.

3. PUBLISHING CAN NEVER BREAK DECODE. Every publish is wrapped by its
   caller in the same bare try/except that guards `report_completion`
   (batched_engine.py:1096). `CAPACITY_ENABLED` is the kill switch.

4. THE MAILBOX IS ONE SLOT, LATEST-WINS. Only the newest capacity value
   has meaning; a queue of stale snapshots is worse than none because a
   consumer would act on the oldest first. `seq` is monotone so a client
   can see how many it skipped. A slow subscriber can never back-pressure
   the decode thread.

Multi-subscriber by construction: `subscribe()` returns a NEW mailbox per
call. Do not "simplify" this into a single shared slot — SessionState
holds exactly one listener (core/session_manager.py:127) and a second
subscriber there silently evicts the first.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Kill switch — flip to False to make every publish a no-op without
# touching a call site.
CAPACITY_ENABLED = True

# Bounded fan-out. A leak would otherwise grow unbounded behind a client
# that reconnects without closing; past the cap we log loudly and refuse
# rather than quietly serving a subscriber nobody reads.
MAX_SUBSCRIBERS = 32


@dataclass(frozen=True)
class CapacitySnapshot:
    """What the server can accept, at one instant.

    Frozen because it crosses a thread boundary: the decode thread builds
    it, the event loop reads it, and neither should be able to mutate the
    other's view.
    """

    seq: int = 0
    model: str = ""  # "" = the primary; resident secondaries name themselves
    # Is the engine accepting work at all? False while a context rebuild
    # has the decode thread parked — a scheduler must read this as zero
    # capacity rather than as "lots of free cells".
    serving: bool = True
    # No `status` field on purpose. The engine never computes one, so it
    # would read "ok" through a fatal latch — actively misleading on the
    # subscription path, where a client sees only this type. `serving`
    # plus `engine_fatal` carry the same information and are both set at
    # the moment they change.
    decode_mode: str = "batched"

    # -- seats (the concurrency surface) --
    seats_total: int = 0
    seats_free: int = 0
    seats_checked_out: int = 0
    seats_by_persona: Dict[str, int] = field(default_factory=dict)

    # -- KV (the context surface) --
    kv_pool_tokens: int = 0  # the whole shared cell
    free_cells: int = 0  # what a new stream could actually claim
    live_occupancy: int = 0  # entitlement held by live streams
    pinned_occupancy: int = 0  # irreducible floor
    pool_slack: int = 0
    min_admit_budget: int = 0
    n_ctx_seq: int = 0  # per-sequence window (cached read)
    static_prefix_tokens: int = 0  # ~94% free in practice; clients discount it

    # -- queue + engine health --
    active_streams: int = 0
    waiting: int = 0
    prefill_budget: int = 0
    engine_steps: int = 0
    kv_pressure_events: int = 0
    kv_forced_windows: int = 0
    kv_evictions: int = 0
    decode_failures: int = 0
    engine_fatal: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


class _Mailbox:
    """One subscriber's slot. Latest-wins, never blocks a producer."""

    __slots__ = ("_loop", "_snap", "_event", "_dropped", "closed", "_bus_id")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._snap: Optional[CapacitySnapshot] = None
        self._event = asyncio.Event()
        self._dropped = 0
        self.closed = False

    def _set_threadsafe(self, snap: CapacitySnapshot) -> None:
        """Called from the DECODE THREAD via call_soon_threadsafe."""
        if self._snap is not None and not self._event.is_set():
            # Should not happen (an unset event means it was consumed),
            # but counting is free and a nonzero value is a real signal.
            self._dropped += 1
        if self._snap is not None and self._event.is_set():
            self._dropped += 1
        self._snap = snap
        self._event.set()

    async def get(self) -> CapacitySnapshot:
        await self._event.wait()
        self._event.clear()
        snap = self._snap
        assert snap is not None  # set() always precedes the event
        return snap

    @property
    def dropped(self) -> int:
        return self._dropped


class CapacityBus:
    """Fan-out from the decode thread to any number of loop subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: Dict[int, _Mailbox] = {}
        self._next_id = 0
        self._seq = 0
        self._latest: Dict[str, CapacitySnapshot] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- wiring ----------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the serving loop. Until this is called, publishes are
        stored as `latest` but delivered to nobody — which is correct
        during startup, before the API is accepting connections."""
        self._loop = loop

    # -- producer side (DECODE THREAD) -----------------------------------

    def publish(self, snap_fields: dict, model: str = "") -> None:
        """Freeze a snapshot and hand it to every subscriber.

        Callers pass plain ints they already hold. This never touches the
        engine, so it cannot deadlock against the decode lock.
        """
        if not CAPACITY_ENABLED:
            return
        with self._lock:
            self._seq += 1
            snap = CapacitySnapshot(seq=self._seq, model=model, **snap_fields)
            self._latest[model] = snap
            targets = list(self._subs.values())
            loop = self._loop
        if loop is None or not targets:
            return
        for mb in targets:
            if mb.closed:
                continue
            try:
                loop.call_soon_threadsafe(mb._set_threadsafe, snap)
            except RuntimeError:
                # Loop closed mid-shutdown. Nothing to do and nothing to
                # report — the subscriber is already gone.
                continue

    # -- consumer side (EVENT LOOP) --------------------------------------

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> Optional[_Mailbox]:
        with self._lock:
            if len(self._subs) >= MAX_SUBSCRIBERS:
                logger.warning(
                    "capacity: refusing subscriber, %d already registered " "(leak?)",
                    len(self._subs),
                )
                return None
            self._next_id += 1
            mb = _Mailbox(loop)
            self._subs[self._next_id] = mb
            mb_id = self._next_id
        setattr(mb, "_bus_id", mb_id)
        return mb

    def unsubscribe(self, mb: _Mailbox) -> None:
        mb.closed = True
        mb_id = getattr(mb, "_bus_id", None)
        if mb_id is None:
            return
        with self._lock:
            self._subs.pop(mb_id, None)

    def latest(self, model: str = "") -> Optional[CapacitySnapshot]:
        with self._lock:
            return self._latest.get(model)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


# Process-wide bus. One server, one decode thread per engine, many
# subscribers — a module singleton is the honest shape here, and it keeps
# the engine from having to thread a reference through its constructor.
BUS = CapacityBus()
