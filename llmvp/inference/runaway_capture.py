#!/usr/bin/env python3
"""
Long-cycle runaway detection and partial-generation capture.

The RepetitionGuard (repetition.py) catches token-level collapse: a
single token run, or an exact cycle of period <= 8 tokens. Live failure
(qwen3-next-coder, June 11): a menu turn produced 130k+ tokens of
PARAGRAPH-scale repetition — the model re-answering the same question in
a loop whose period (~15-40 tokens) sat above the guard's detection
horizon. Nothing saw it except the agent-side watchdog's raw token
ceiling, 43 times in a row, and the cancelled text was discarded so the
failure mode couldn't even be inspected after the fact.

Two additions, both hooked into the generation loop:

* :func:`detect_long_cycle` — a periodic structural check on the
  accumulated text: split the tail window into fixed chunks and measure
  the distinct-chunk ratio. Degenerate paragraph loops collapse to a
  handful of distinct chunks; legitimate prose/code stays near 1.0.
  O(window) per check, run every ``CHECK_INTERVAL`` tokens.

* :func:`dump_capture` — best-effort write of the partial text +
  metadata to ``<logs>/runaway_captures/`` whenever a generation aborts
  abnormally (long-cycle detection, token-level degeneracy, or consumer
  cancellation, i.e. the watchdog). The blind spot becomes a file you
  can read.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("llm-mvp")

# Run the structural check every this many generated tokens. ~8KB of
# text between checks keeps the amortized cost negligible.
CHECK_INTERVAL = 2048

# Tail window of the base tier. Must be filled before any check fires —
# short, legitimate generations (menus, patches) never qualify.
WINDOW_BYTES = 8192

# n-gram size for the distinct-ratio measure. Counted at stride 1 so
# the measure is alignment-proof: a loop of period P yields exactly ~P
# distinct n-grams regardless of where chunk boundaries fall (a fixed
# non-overlapping chunking misses any period not aligned to it).
NGRAM_BYTES = 32

# Trip when <= this fraction of a window's n-grams are distinct.
# A loop of period P yields ratio ≈ P / window, so at 0.125 each tier
# catches periods up to window/8; varied code/prose sits far above
# (templated YAML with shared field names still measures ~0.4 — see
# tests).
MAX_DISTINCT_RATIO = 0.125

# Detection tiers: (window_bytes, trip_ratio), checked smallest-first.
# The 8KB tier catches periods up to ~1KB (the June 11 menu runaway:
# 15-40 token cycles). The 32KB tier catches paragraph-scale orbits
# that sail over it — live failure (qwen3-next conclude turn, July 22):
# a ~3.2KB / ~800-token deliberation cycle repeated 47x, whose 8KB
# ratio plateaued at 0.29 while the 32KB ratio fell to 0.036 (trips at
# ~12k tokens, ~8 min before the agent watchdog's raw ceiling).
WINDOW_TIERS = (
    (WINDOW_BYTES, MAX_DISTINCT_RATIO),
    (32768, MAX_DISTINCT_RATIO),
)

# Capture at most this much tail text per dump — enough to see the loop
# and its onset without writing 130k-token files.
CAPTURE_TAIL_BYTES = 262_144


def detect_long_cycle(acc_bytes: bytes) -> Optional[str]:
    """Return a reason string if the tail of ``acc_bytes`` looks like a
    long-period repetition loop, else None.

    Checks each tier in ``WINDOW_TIERS`` whose window has filled.
    Deliberately conservative: 87.5% of a window must consist of
    repeated chunks before it trips. A tier's window must be full
    before it participates — short, legitimate generations never
    qualify.
    """
    for window_bytes, trip_ratio in WINDOW_TIERS:
        if len(acc_bytes) < window_bytes:
            continue
        window = acc_bytes[-window_bytes:]
        total = len(window) - NGRAM_BYTES + 1
        distinct = len({window[i : i + NGRAM_BYTES] for i in range(total)})
        ratio = distinct / total
        if ratio <= trip_ratio:
            return (
                f"long-cycle repetition: {distinct}/{total} distinct "
                f"{NGRAM_BYTES}B n-grams (ratio {ratio:.4f}, est. period "
                f"~{distinct}B) in the last {window_bytes}B"
            )
    return None


def dump_capture(
    logs_dir: Any,
    reason: str,
    acc_bytes: bytes,
    tokens_generated: int,
    meta: Optional[dict] = None,
) -> Optional[str]:
    """Best-effort dump of a partial generation. Never raises.

    Returns the written path, or None on failure/no-op.
    """
    try:
        dest = Path(logs_dir) / "runaway_captures"
        dest.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        path = dest / f"{stamp}.json"
        tail = acc_bytes[-CAPTURE_TAIL_BYTES:]
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "tokens_generated": tokens_generated,
            "bytes_total": len(acc_bytes),
            "bytes_captured": len(tail),
            "meta": meta or {},
            "text": tail.decode("utf-8", errors="replace"),
        }
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
        log.warning("📼 Runaway capture written: %s (%s)", path, reason)
        return str(path)
    except Exception:  # noqa: BLE001 - capture must never break generation
        log.warning("Runaway capture failed", exc_info=True)
        return None
