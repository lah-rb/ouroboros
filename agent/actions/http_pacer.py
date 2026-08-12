"""Per-host request pacing with a single in-process owner.

WHY THIS EXISTS. `polite_request` paced itself out of mission state — read the
last timestamp, sleep the remainder, issue, write the timestamp back. Serially
that works. Concurrently it is a burst generator: every in-flight caller reads
the SAME `last_ts`, computes the SAME wait, and fires together. Since
`effects.read_state`/`write_state` take no lock, it is also a lost-update race
on the request counter.

That mattered the moment acquisition became concurrent, and it plausibly
mattered before. Measured on the 2026-08-11 spectra run: OpenAlex rejected
**78% of 536 requests** (418 429s) while we believed we were pacing at 1.67 rps
against a documented ~10 rps polite-pool tolerance. Two contributing bugs are
now fixed here:

  * THE SLOT IS RESERVED BEFORE THE REQUEST, NOT STAMPED AFTER IT. The old code
    wrote `last_ts` after the response returned, so the real spacing was
    response-to-request. Under any latency the effective rate drifted away from
    the configured one, and under concurrency it collapsed entirely.
  * RETRY-AFTER IS HONOURED AS GIVEN. The old floor was
    `min(max(retry_after, 10.0), 60.0)` — a flat 10s minimum even when the API
    asked for 1s. Across 714 429s that alone accounted for ~2.0 h of an 8 h
    budget spent asleep.

ADAPTIVE, BECAUSE THE CONFIGURED INTERVAL IS A GUESS. A 429 is the server
telling us our number is wrong. On repeated 429s a host's interval widens
multiplicatively and then decays back toward its configured floor as calls
succeed, so pacing converges on what the API actually grants rather than what a
comment assumed. This only ever makes us SLOWER than configured — it is not a
mechanism for pushing harder, and it must not become one.

Process-local by design: one mission runs on one thread with one event loop
(`agent/mission_runner.py`), so a module-level pacer is the single owner. It
deliberately does NOT persist — pacing is a property of the live process, and a
stale timestamp from a previous run should never make the next one wait.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# Widen by this factor on a 429, decay by this on a clean response.
_BACKOFF_FACTOR = 1.5
_DECAY_FACTOR = 0.9
# Never widen past this multiple of the host's configured interval — beyond it
# the run is not rate-limited, it is broken, and silently crawling hides that.
_MAX_WIDEN = 8.0
# Retry-After bounds. The floor exists only to stop a 0/absent header meaning
# "immediately"; it is NOT the old blanket 10s.
_RETRY_FLOOR_S = 1.0
_RETRY_CAP_S = 60.0


@dataclass
class _HostState:
    base_interval: float
    interval: float
    next_allowed: float = 0.0
    requests: int = 0
    throttled: int = 0
    slept_s: float = 0.0
    backoff_s: float = 0.0
    consecutive_429: int = 0


@dataclass
class HostPacer:
    """Reserve-before-issue pacing, one instance owning all hosts."""

    default_interval: float = 2.0
    intervals: dict[str, float] = field(default_factory=dict)
    _hosts: dict[str, _HostState] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock | None = field(default=None, init=False)

    def _state(self, host: str) -> _HostState:
        st = self._hosts.get(host)
        if st is None:
            base = self.intervals.get(host, self.default_interval)
            st = _HostState(base_interval=base, interval=base)
            self._hosts[host] = st
        return st

    async def reserve(self, url: str) -> float:
        """Claim this host's next slot and sleep until it opens.

        The reservation is taken under the lock and the cursor advanced
        immediately, so N concurrent callers serialise into N spaced slots
        instead of all reading the same timestamp and firing at once.
        Returns the seconds slept, for telemetry.
        """
        host = urlsplit(url).netloc
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            st = self._state(host)
            now = time.monotonic()
            start = max(now, st.next_allowed)
            st.next_allowed = start + st.interval
            st.requests += 1
            wait = start - now
        if wait > 0:
            st.slept_s += wait
            await asyncio.sleep(wait)
        return max(0.0, wait)

    async def note_throttled(self, url: str, retry_after: float | None) -> float:
        """A 429 came back. Widen this host and return the sleep to honour.

        `retry_after` is used AS GIVEN within [1s, 60s]; the caller is expected
        to sleep the returned value before retrying.
        """
        host = urlsplit(url).netloc
        delay = min(max(float(retry_after or 0.0), _RETRY_FLOOR_S), _RETRY_CAP_S)
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            st = self._state(host)
            st.throttled += 1
            st.consecutive_429 += 1
            st.backoff_s += delay
            ceiling = st.base_interval * _MAX_WIDEN
            widened = min(st.interval * _BACKOFF_FACTOR, ceiling)
            if widened > st.interval:
                logger.info(
                    "pacer: %s throttled (%d consecutive) — interval %.2fs → %.2fs",
                    host,
                    st.consecutive_429,
                    st.interval,
                    widened,
                )
            st.interval = widened
            # Push the cursor out too, so in-flight siblings inherit the wider
            # spacing rather than draining the old reservations at the old rate.
            st.next_allowed = max(st.next_allowed, time.monotonic() + delay)
        return delay

    async def note_ok(self, url: str) -> None:
        """A clean response — decay this host back toward its configured floor."""
        host = urlsplit(url).netloc
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            st = self._state(host)
            st.consecutive_429 = 0
            if st.interval > st.base_interval:
                st.interval = max(st.base_interval, st.interval * _DECAY_FACTOR)

    def stats(self) -> dict:
        """Per-host counters for post-hoc booking. Cheap; safe to call often."""
        return {
            host: {
                "requests": st.requests,
                "throttled": st.throttled,
                "interval": round(st.interval, 3),
                "base_interval": st.base_interval,
                "slept_s": round(st.slept_s, 1),
                "backoff_s": round(st.backoff_s, 1),
            }
            for host, st in sorted(self._hosts.items())
        }

    def total_requests(self) -> int:
        return sum(st.requests for st in self._hosts.values())

    def reset(self) -> None:
        """Test hook — drop all host state."""
        self._hosts.clear()
