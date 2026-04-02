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

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

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

    def finish(self) -> None:
        """Mark generation as complete and log diagnostics."""
        with self._lock:
            now = time.monotonic()
            s = self._status
            s.finished_at = now
            s.phase = "complete"

            total = now - s.started_at if s.started_at else 0
            gen_time = now - s.first_token_at if s.first_token_at else 0

            # Preserve for post-mortem
            self._last_thinking = s.thinking_content
            self._last_request_id = s.request_id
            self._last_status_snapshot = {
                "request_id": s.request_id,
                "prompt_tokens": s.prompt_tokens,
                "tokens_generated": s.tokens_generated,
                "eval_duration": round(s.eval_duration, 2),
                "generation_duration": round(gen_time, 2),
                "total_duration": round(total, 2),
                "tok_per_sec": (
                    round(s.tokens_generated / gen_time, 1) if gen_time > 0 else 0
                ),
            }

            s.active = False

        snap = self._last_status_snapshot
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
            return result

    def get_last_diagnostics(self) -> dict:
        """Get diagnostics from the last completed generation."""
        with self._lock:
            return dict(self._last_status_snapshot)

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
