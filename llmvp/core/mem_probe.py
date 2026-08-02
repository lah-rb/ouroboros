"""Memory instrumentation for context rebuilds — the swarm-kill black box.

WHY THIS EXISTS. On 2026-08-02 the swarm server (524k ctx, ~41.5 GB of KV)
vanished 2.5s into a context rebuild that normally takes ~9.8s: no traceback,
no shutdown line, no kernel memorystatus record. Two hypotheses were tested
against a live repro and BOTH refuted — the geometry (the same config rebuilt
fine 30 minutes earlier in the same run) and the forced drain (reproduced
exactly, survived cleanly). Cause unknown; even "OOM" is a guess.

The reason it stayed a guess is that nothing recorded memory across the one
window where it matters. A rebuild frees a multi-GB KV allocation and then
immediately allocates another; the interesting number is the PEAK during that
window, which no before/after pair can see. So:

  - ``sample()`` is one cheap reading (psutil, no shelling out).
  - ``RebuildWatch`` samples on a background thread every 250ms for the life
    of the rebuild and keeps the extremes, so a transient spike between the
    close and the allocation cannot hide.
  - the last N records live on the backend and are published on /health, so
    the next occurrence leaves evidence even if the process dies before it
    can write a conclusion. The log line is for humans reading a run; the
    health record is for whatever is polling when it happens.

Deliberately dependency-light and failure-tolerant: instrumentation must
never be the thing that breaks a rebuild. Every entry point swallows its own
errors and degrades to an empty reading.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# Keep the tail short: these are read by a human after an incident, and an
# unbounded list on a long-lived server is a slow leak.
MAX_RECORDS = 12
SAMPLE_INTERVAL_S = 0.25


def sample() -> Dict[str, float]:
    """One memory reading, in MB. Empty dict if psutil is unavailable.

    TOTAL IS THE MEASURE; WIRED IS NOT (operator, 2026-08-03). On unified
    memory the wired figure is bimodal and inflated by file-backed cache —
    the same reason `top`'s "used" cannot be trusted to decide whether a load
    is safe. Measured across four consecutive healthy swarm rebuilds, wired
    bounced 112.5 / 117.0 / 112.6 / 114.2 GB against a nominal
    iogpu.wired_limit_mb of 116,000 without anything being wrong; reading a
    limit breach into that noise is a mistake this file exists to prevent.

    So the headline is `used_mb` (total - available), with `available_mb` as
    the floor the allocator actually works against. `wired_mb` is still
    recorded — it is the Metal ceiling and cheap to read — but it is a
    secondary, explicitly noisy channel and nothing should alarm on it alone.
    """
    try:
        import psutil

        vm = psutil.virtual_memory()
        total = float(vm.total)
        available = float(vm.available)
        out: Dict[str, float] = {
            "total_mb": round(total / 1e6, 1),
            "available_mb": round(available / 1e6, 1),
            # total - available: what is genuinely committed, net of the
            # reclaimable file cache that inflates the naive "used" meters.
            "used_mb": round((total - available) / 1e6, 1),
            "used_percent": vm.percent,
            "rss_mb": round(psutil.Process().memory_info().rss / 1e6, 1),
        }
        wired = getattr(vm, "wired", None)
        if wired is not None:
            out["wired_mb_noisy"] = round(wired / 1e6, 1)
        return out
    except Exception:  # noqa: BLE001 — instrumentation never raises
        return {}


class RebuildWatch:
    """Context manager that brackets a context rebuild with memory sampling.

    Usage::

        with RebuildWatch("batched", backend=self) as w:
            ...close the old context...
            w.mark("closed")
            ...allocate the new one...
            w.mark("allocated")

    The marks are the three points that matter; the background sampler
    catches whatever happens between them. On exit it logs one INFO line and
    appends a record to the backend's ring (if one was supplied).
    """

    def __init__(self, kind: str, backend: Any = None) -> None:
        self.kind = kind
        self._backend = backend
        self.marks: Dict[str, Dict[str, float]] = {}
        self._samples: List[Dict[str, float]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0

    # -- lifecycle ---------------------------------------------------
    def __enter__(self) -> "RebuildWatch":
        self._t0 = time.perf_counter()
        self.mark("start")
        try:
            self._thread = threading.Thread(
                target=self._run, name=f"mem-watch-{self.kind}", daemon=True
            )
            self._thread.start()
        except Exception:  # noqa: BLE001 — sampling is best-effort
            self._thread = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.mark("end")
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            self._publish(failed=exc_type is not None)
        except Exception:  # noqa: BLE001
            log.debug("mem watch publish failed", exc_info=True)
        return False  # never suppress

    def _run(self) -> None:
        while not self._stop.is_set():
            s = sample()
            if s:
                self._samples.append(s)
            self._stop.wait(SAMPLE_INTERVAL_S)

    # -- API ---------------------------------------------------------
    def mark(self, name: str) -> None:
        """Record a named point (start / closed / allocated / end)."""
        s = sample()
        if s:
            s["at_s"] = round(time.perf_counter() - self._t0, 3)
            self.marks[name] = s

    def record(self) -> Dict[str, Any]:
        """The finished record: marks, extremes, and the derived headline."""
        avail = [s["available_mb"] for s in self._samples if "available_mb" in s]
        used = [s["used_mb"] for s in self._samples if "used_mb" in s]
        wired = [s["wired_mb_noisy"] for s in self._samples if "wired_mb_noisy" in s]
        rec: Dict[str, Any] = {
            "kind": self.kind,
            "duration_s": round(time.perf_counter() - self._t0, 2),
            "samples": len(self._samples),
            "marks": self.marks,
        }
        if used:
            # THE HEADLINE: peak committed memory across the window.
            rec["used_peak_mb"] = max(used)
            rec["used_min_mb"] = min(used)
        if avail:
            rec["available_min_mb"] = min(avail)
            rec["available_max_mb"] = max(avail)
            # How close the allocation came to the floor. A rebuild that dies
            # should leave a small value on its LAST surviving record — or
            # none at all, if it died before the sampler could publish.
            rec["headroom_low_mb"] = min(avail)
        if wired:
            # Secondary and explicitly noisy — see sample().
            rec["wired_max_mb_noisy"] = max(wired)
        for s in self._samples:
            if "total_mb" in s:
                rec["total_mb"] = s["total_mb"]
                break
        return rec

    def _publish(self, failed: bool) -> None:
        rec = self.record()
        rec["failed"] = failed
        if self._backend is not None:
            ring = getattr(self._backend, "_mem_rebuild_records", None)
            if ring is None:
                ring = []
                try:
                    self._backend._mem_rebuild_records = ring
                except Exception:  # noqa: BLE001 — read-only backend, skip
                    ring = None
            if ring is not None:
                ring.append(rec)
                del ring[:-MAX_RECORDS]
        log.info(
            "📏 rebuild memory [%s] %.2fs: used peak %s / %s MB, headroom low "
            "%s MB (wired peak %s MB, noisy — do not alarm on it)",
            self.kind,
            rec["duration_s"],
            rec.get("used_peak_mb", "?"),
            rec.get("total_mb", "?"),
            rec.get("headroom_low_mb", "?"),
            rec.get("wired_max_mb_noisy", "?"),
        )


def health_block(backend: Any) -> Dict[str, Any]:
    """The /health view: current reading + the rebuild tail.

    Published even when the tail is empty, so a poller can tell
    "instrumented, nothing yet" from "not instrumented".
    """
    out: Dict[str, Any] = {"current": sample()}
    recs = list(getattr(backend, "_mem_rebuild_records", []) or [])
    out["rebuilds_recorded"] = len(recs)
    if recs:
        out["last_rebuild"] = recs[-1]
        lows = [r["headroom_low_mb"] for r in recs if "headroom_low_mb" in r]
        if lows:
            # The trend is the point: a headroom floor walking downward across
            # rebuilds is the shape that precedes a death, and it is invisible
            # in any single record.
            out["headroom_low_mb_trend"] = lows
            out["headroom_low_mb_min"] = min(lows)
        peaks = [r["used_peak_mb"] for r in recs if "used_peak_mb" in r]
        if peaks:
            # Same trend read from the committed side — total is the measure
            # (see sample()), so this is the one to alarm on.
            out["used_peak_mb_trend"] = peaks
            out["used_peak_mb_max"] = max(peaks)
    return out
