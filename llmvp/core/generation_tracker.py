"""
Generation Status Tracker

Thread-safe tracking of in-progress generation state, serving three purposes:

1. **Health polling**: Exposes `tokens_generated` and phase information so
   external clients can distinguish "evaluating prompt" from "generating
   tokens" from "stuck".

2. **Thinking stream**: Captures pre-delimiter content (chain-of-thought)
   separately from the response, making it available through a dedicated
   GraphQL endpoint.

3. **Diagnostics**: Records timing for each phase (eval, first token,
   generation) to help identify performance bottlenecks and stuck states.

The tracker is a singleton accessed by the generation loop (writer) and
the API layer (reader).  All writes happen in the threadpool thread
running generation; all reads happen in the asyncio event loop.  The
threading.Lock serializes access.
"""

import collections
import logging
import statistics
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("llm-mvp")


@dataclass
class GenerationStatus:
    """Snapshot of the current generation's progress."""

    active: bool = False
    tokens_generated: int = 0
    started_at: float = 0.0
    last_token_at: float = 0.0
    thinking_content: str = ""
    thinking_complete: bool = False
    request_id: str = ""

    # Diagnostic phase tracking
    phase: str = "idle"  # idle → eval → generating → complete
    prompt_tokens: int = 0
    first_token_at: float = 0.0  # When the first generated token arrived
    eval_duration: float = 0.0  # Seconds spent evaluating prompt
    finished_at: float = 0.0


class GenerationTracker:
    """Thread-safe generation status tracker (singleton)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._status = GenerationStatus()
        self._last_thinking: str = ""
        self._last_request_id: str = ""
        # Keep last completed status for post-mortem diagnostics
        self._last_status_snapshot: dict = {}
        # Rolling latency/throughput trend (pass-2 deep-health). Each entry is
        # (prefill_tps, decode_tps, ttft_s) from a completed, non-trivial
        # generation. _baseline_decode_tps is the warm baseline (median of an
        # early window, skipping cold starts) — throughput_drift = recent /
        # baseline, so a value well under 1 flags slowdown (GPU spill/throttle,
        # the Apple-Silicon memory-eviction signature) without any per-token cost.
        self._trend: "collections.deque" = collections.deque(maxlen=64)
        self._baseline_decode_tps: float | None = None
        # Cold prefill rate seeded from the boot static eval (tok/s). This is
        # the model's HONEST worst-case prefill speed on this hardware —
        # trend samples measure effective rate (cache-assisted turns look
        # 10-20x faster), so the seed anchors expected_eval_seconds against
        # the cold case. Server-side model knowledge, exposed via health so
        # clients can size their watchdog timeouts without hardcoding
        # per-model numbers (the 300s-vs-334.9s doom loop of 2026-07-23).
        self._prefill_seed_tps: float | None = None

    def start(self, request_id: str = "", prompt_tokens: int = 0) -> None:
        """Mark the beginning of a new generation (entering eval phase)."""
        with self._lock:
            now = time.monotonic()
            self._status = GenerationStatus(
                active=True,
                tokens_generated=0,
                started_at=now,
                last_token_at=now,
                thinking_content="",
                thinking_complete=False,
                request_id=request_id,
                phase="eval",
                prompt_tokens=prompt_tokens,
            )
        log.info(
            "📊 Generation started: request=%s, prompt_tokens=%d",
            request_id or "(unnamed)",
            prompt_tokens,
        )

    def mark_first_token(self) -> None:
        """Mark that the first generated token has arrived.

        This transitions from 'eval' phase to 'generating' phase.
        The time between start() and mark_first_token() is the
        prompt evaluation duration.
        """
        with self._lock:
            now = time.monotonic()
            self._status.first_token_at = now
            self._status.eval_duration = now - self._status.started_at
            self._status.phase = "generating"
            self._status.last_token_at = now
        log.info(
            "📊 First token arrived after %.1fs eval (request=%s)",
            self._status.eval_duration,
            self._status.request_id or "(unnamed)",
        )

    def record_token(self) -> None:
        """Record that a token was generated."""
        with self._lock:
            self._status.tokens_generated += 1
            self._status.last_token_at = time.monotonic()
            # Mark first token transition if not already done
            if self._status.phase == "eval":
                now = self._status.last_token_at
                self._status.first_token_at = now
                self._status.eval_duration = now - self._status.started_at
                self._status.phase = "generating"

    def append_thinking(self, text: str) -> None:
        """Append text to the thinking (pre-delimiter) buffer."""
        with self._lock:
            self._status.thinking_content += text
            self._status.last_token_at = time.monotonic()

    def mark_thinking_complete(self) -> None:
        """Mark that the delimiter was reached — thinking is done."""
        with self._lock:
            self._status.thinking_complete = True

    def finish(self, quiet: bool = False) -> None:
        """Mark generation as complete and (unless quiet) log diagnostics.

        The completion stats are derived from the SHARED status, which is
        only per-request-accurate when one stream runs at a time (the pool
        path). Batched-concurrency callers MUST pass ``quiet=True`` — every
        interleaved ``start()`` resets the shared counters, so a blended
        finish() here would log another stream's tokens as this request's
        and poison the throughput trend (observed 2026-07-21: a 517-token
        stream logged as generated=7361 @ 115.7 tok/s under a 14-stream
        fan-out). In batched mode the ENGINE reports truthful per-stream
        numbers via ``report_completion()`` at stream retirement instead.
        """
        with self._lock:
            now = time.monotonic()
            s = self._status
            s.finished_at = now
            s.phase = "complete"

            total = now - s.started_at if s.started_at else 0
            gen_time = now - s.first_token_at if s.first_token_at else 0

            # Preserve for post-mortem (thinking capture is shared-status
            # either way — see get_thinking).
            self._last_thinking = s.thinking_content
            self._last_request_id = s.request_id
            s.active = False

            if quiet:
                return

        self.report_completion(
            request_id=s.request_id,
            prompt_tokens=s.prompt_tokens,
            generated_tokens=s.tokens_generated,
            eval_s=s.eval_duration,
            gen_s=gen_time,
            total_s=total,
        )

    def report_completion(
        self,
        request_id: str = "",
        prompt_tokens: int = 0,
        generated_tokens: int = 0,
        eval_s: float = 0.0,
        gen_s: float = 0.0,
        total_s: float | None = None,
    ) -> None:
        """Record one COMPLETED generation with per-request-true numbers.

        The authoritative completion path: updates the post-mortem
        snapshot, folds the throughput trend (health drift baseline), and
        emits the "Generation complete" log line. Called by finish() on
        the single-stream pool path, and DIRECTLY by the batched engine at
        stream retirement with per-stream spans (which the shared status
        cannot provide under concurrency).
        """
        if total_s is None:
            total_s = eval_s + gen_s
        snap = {
            "request_id": request_id,
            "prompt_tokens": prompt_tokens,
            "tokens_generated": generated_tokens,
            "eval_duration": round(eval_s, 2),
            "generation_duration": round(gen_s, 2),
            "total_duration": round(total_s, 2),
            "tok_per_sec": (round(generated_tokens / gen_s, 1) if gen_s > 0 else 0),
        }
        with self._lock:
            self._last_status_snapshot = snap
            # Fold into the rolling trend — only non-trivial generations (real
            # decode time + tokens) so cache-hit/empty turns don't skew it.
            if gen_s > 0.05 and generated_tokens >= 4:
                decode_tps = generated_tokens / gen_s
                prefill_tps = prompt_tokens / eval_s if eval_s > 0.02 else 0.0
                self._trend.append((prefill_tps, decode_tps, eval_s))
                # Establish the warm baseline once: median decode tps of an
                # early window (skip the first 2 cold-start samples).
                if self._baseline_decode_tps is None and len(self._trend) >= 10:
                    warm = [t[1] for t in list(self._trend)[2:]]
                    self._baseline_decode_tps = statistics.median(warm)

        log.info(
            "📊 Generation complete: request=%s, prompt=%d tok, "
            "generated=%d tok, eval=%.1fs, gen=%.1fs, total=%.1fs, "
            "speed=%.1f tok/s",
            snap["request_id"] or "(unnamed)",
            snap["prompt_tokens"],
            snap["tokens_generated"],
            snap["eval_duration"],
            snap["generation_duration"],
            snap["total_duration"],
            snap["tok_per_sec"],
        )

    def promote_thinking(self, thinking_text: str) -> None:
        """Forward thinking content captured after finish() into the
        persistent post-mortem field.

        Exists because the session-turn path extracts thinking via the
        FSM labeller on the full raw output *after* the backend has
        already called ``finish()``. Plain ``append_thinking`` at that
        point writes to ``_status.thinking_content`` but ``active`` is
        already False, so ``get_thinking()`` skips the active branch
        and reads from ``_last_thinking`` — which was captured empty
        at finish() time.

        This method writes directly to ``_last_thinking`` so the
        GraphQL ``thinking`` query returns content extracted after
        generation completed. Idempotent: repeat calls just overwrite
        with the latest value.

        902 regression fix: all session-based inference was publishing
        empty thinking_content to trace events because the stream-time
        ``append_thinking`` hook isn't called on the session path
        (session_manager yields raw chunks unchanged; FSM runs later).
        """
        with self._lock:
            self._last_thinking = thinking_text
            # Also keep _status.thinking_content coherent for callers
            # that inspect the live tracker state before a new
            # generation starts. After the next start() it will be
            # reset to "" per the start() contract.
            self._status.thinking_content = thinking_text
            self._status.thinking_complete = True

    def seed_prefill_rate(self, tokens: int, seconds: float) -> None:
        """Seed the cold prefill rate from the boot static eval.

        Called once per boot by the backend after timing the static-prefix
        eval — a genuine cold, cache-free prefill sample. Keeps the SLOWEST
        seed seen (multi-slot pools may eval more than once; later evals can
        be page-cache-warmed and flatter the estimate).
        """
        if tokens <= 0 or seconds <= 0.2:
            return
        rate = tokens / seconds
        with self._lock:
            if self._prefill_seed_tps is None or rate < self._prefill_seed_tps:
                self._prefill_seed_tps = rate

    def _cold_prefill_tps_locked(self) -> float | None:
        """Conservative (worst-case) prefill rate: min of the boot seed and
        observed per-request rates. Effective rates from cache-assisted turns
        are optimistic — using the minimum keeps expected_eval_seconds an
        UPPER estimate, which is the direction a timeout consumer needs."""
        candidates = [tps for tps, _, _ in self._trend if tps and tps > 0]
        if self._prefill_seed_tps:
            candidates.append(self._prefill_seed_tps)
        return min(candidates) if candidates else None

    def get_status(self) -> dict:
        """Get current generation status (for health endpoint)."""
        with self._lock:
            s = self._status
            result = {
                "generation_active": s.active,
                "tokens_generated": s.tokens_generated,
                "request_id": s.request_id,
                "phase": s.phase,
                "prompt_tokens": s.prompt_tokens,
            }
            if s.active:
                now = time.monotonic()
                result["elapsed_seconds"] = round(now - s.started_at, 1)
                result["seconds_since_last_token"] = round(now - s.last_token_at, 1)
                result["eval_duration"] = round(s.eval_duration, 2)
                result["thinking_complete"] = s.thinking_complete
                # Worst-case eval estimate for the IN-FLIGHT prompt, from
                # server-side measured rates. Advisory: clients may use it to
                # size their stuck-eval timeout instead of hardcoding one.
                rate = self._cold_prefill_tps_locked()
                if rate and s.prompt_tokens > 0:
                    result["expected_eval_seconds"] = round(s.prompt_tokens / rate, 1)
            return result

    def get_last_diagnostics(self) -> dict:
        """Get diagnostics from the last completed generation."""
        with self._lock:
            return dict(self._last_status_snapshot)

    def get_trend(self) -> dict:
        """Rolling latency/throughput trend for deep-health (read-only).

        ``throughput_drift`` = recent decode tps / warm baseline; ~1.0 is
        nominal, well under 1 means generation slowed (the degradation signal).
        """
        with self._lock:
            samples = list(self._trend)
            baseline = self._baseline_decode_tps
        if not samples:
            return {"trend_samples": 0}
        recent = samples[-12:]

        def _med(vals):
            vals = [v for v in vals if v and v > 0]
            return statistics.median(vals) if vals else None

        dtps = _med(s[1] for s in recent)
        ptps = _med(s[0] for s in recent)
        ttft = _med(s[2] for s in recent)
        drift = round(dtps / baseline, 3) if (dtps and baseline) else None
        return {
            "trend_samples": len(samples),
            "decode_tps_recent": round(dtps, 1) if dtps else None,
            "prefill_tps_recent": round(ptps, 1) if ptps else None,
            "ttft_recent_s": round(ttft, 2) if ttft else None,
            "decode_tps_baseline": round(baseline, 1) if baseline else None,
            "throughput_drift": drift,
        }

    def get_thinking(self, request_id: str = "") -> dict:
        """Get thinking content (for dedicated thinking endpoint)."""
        with self._lock:
            s = self._status

            if s.active:
                if not request_id or request_id == s.request_id:
                    return {
                        "request_id": s.request_id,
                        "content": s.thinking_content,
                        "complete": s.thinking_complete,
                        "active": True,
                    }

            if not request_id or request_id == self._last_request_id:
                return {
                    "request_id": self._last_request_id,
                    "content": self._last_thinking,
                    "complete": True,
                    "active": False,
                }

            return {
                "request_id": request_id,
                "content": "",
                "complete": False,
                "active": False,
            }


# Module-level singleton
_tracker = GenerationTracker()


def get_tracker() -> GenerationTracker:
    """Get the global generation tracker instance."""
    return _tracker
