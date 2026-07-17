"""The shared per-token decode state machine.

ONE implementation of the token loop's inner block — incremental
detokenization with a cumulative byte accumulator, repetition + long-cycle
guards, bounded-tail stop scan, FinalChannelStop, buffer_mode, and
UTF-8-boundary text emission — driven by BOTH generation paths:

- pool: ``llama_cpp_backend.generate_stream_sync`` feeds it inside its
  generator loop (EOG check, tracker marks, and the effective_max budget
  stay caller-side, exactly as the engine keeps them engine-side);
- batched: ``batched_engine`` binds one pipeline per StreamState.

History: the batched engine originally PORTED the pool's inline block as
``TokenPipeline`` and the two copies immediately drifted (unbounded vs
bounded detok tail; eager vs lazy capture meta). This module is the single
home; neither path carries a private copy anymore.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Union

from inference.decode_constants import DETOK_TAIL, STOP_TAIL_SLACK

logger = logging.getLogger("llm-mvp")


@dataclass
class Verdict:
    degenerate: Optional[str] = None  # guard/long-cycle/detok failure reason
    stop: bool = False
    end_reason: Optional[str] = None  # e.g. "final_channel_close"


def build_capture_meta(
    llama: Any,
    request_id: str,
    temperature: float,
    prompt_tokens: List[int],
) -> Dict[str, Any]:
    """Runaway-capture metadata: request identity + the prompt tail.

    The prompt is the half of the specimen live failures never preserved
    (43 cancelled menu runaways, prompts unrecoverable). Tail only — the
    dynamic context that varies between a clean run and a runaway sits at
    the end. Call lazily (pass as a thunk to TokenPipeline): detok of 768
    tokens is capture-time-only work, not per-request work.
    """
    meta: Dict[str, Any] = {
        "request_id": request_id,
        "temperature": temperature,
        "prompt_tokens": len(prompt_tokens),
    }
    try:
        meta["prompt_tail"] = llama.detokenize(list(prompt_tokens[-768:])).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001 — forensics must not break the request
        meta["prompt_tail"] = "(detokenization failed)"
    return meta


class TokenPipeline:
    """Per-stream token state machine (see module docstring).

    All state is per-instance so N pipelines never share anything. The
    caller owns: EOG detection (before feed), token counting/telemetry,
    the max-tokens budget, and what to DO with a Verdict (the pool raises
    DegenerateGenerationError; the engine converts to a stream error).
    """

    def __init__(
        self,
        llama: Any,
        stop_texts: List[str],
        *,
        guard: Any = None,  # RepetitionGuard or None
        final_stop: Any = None,  # FinalChannelStop or None
        long_cycle_on: bool = True,
        buffer_mode: bool = False,
        capture_dir: str = "./logs",
        capture_meta: Union[Dict[str, Any], Callable[[], Dict[str, Any]], None] = None,
        initial_prior_tokens: Optional[List[int]] = None,
    ):
        self._llama = llama
        self._stop_bytes = [s.encode("utf-8") for s in stop_texts]
        max_stop_len = max((len(sb) for sb in self._stop_bytes), default=0)
        self._stop_tail = max_stop_len + STOP_TAIL_SLACK
        self._guard = guard
        self._final_stop = final_stop
        self._long_cycle_on = long_cycle_on
        self.buffer_mode = buffer_mode
        self._capture_dir = capture_dir
        # Dict OR thunk — resolved only at dump time so the prompt-tail detok
        # (768 tokens) is paid on capture, never per request.
        self._capture_meta = capture_meta
        self.acc_bytes = b""
        self._returned_bytes = 0
        # Seed the detok context with the prompt tail so the FIRST generated
        # token gets a correct piece boundary (pool parity; the original
        # batched port started cold).
        self._prior_tail: List[int] = list((initial_prior_tokens or [])[-DETOK_TAIL:])
        self.n_tokens = 0

    def feed(self, token: int) -> Verdict:
        """Process one sampled token: guard -> long-cycle -> detok ->
        stop scan -> final-stop. The EOG check happens caller-side BEFORE
        feed on both paths."""
        from inference import runaway_capture

        self.n_tokens += 1

        if self._guard is not None:
            reason = self._guard.observe(token)
            if reason:
                return Verdict(degenerate=reason)

        # Long-cycle check runs on the accumulator BEFORE this token's
        # bytes land (parity with the original pool ordering: it fires
        # between append and detok).
        if self._long_cycle_on and self.n_tokens % runaway_capture.CHECK_INTERVAL == 0:
            lc_reason = runaway_capture.detect_long_cycle(self.acc_bytes)
            if lc_reason:
                return Verdict(degenerate=f"long-cycle: {lc_reason}")

        # Incremental detokenize: only the NEW token, with a bounded tail of
        # prior tokens as context. A detok failure (e.g. "Negative size
        # passed to PyBytes_FromStringAndSize" on a degenerate token) is
        # converted to the same controlled-abort path as the guards.
        try:
            piece: bytes = self._llama.detokenize([token], prev_tokens=self._prior_tail)
        except Exception as e:  # noqa: BLE001 — convert to controlled abort
            return Verdict(degenerate=f"detokenization failed: {e}")
        self._prior_tail.append(token)
        if len(self._prior_tail) > DETOK_TAIL:
            del self._prior_tail[:-DETOK_TAIL]
        self.acc_bytes += piece

        # Stop-sequence detection over a bounded tail; the stop text is NOT
        # stripped — it stays in the output so downstream consumers (FSM
        # labeller, capture log) see the full output.
        should_stop = any(
            sb in self.acc_bytes[-self._stop_tail :] for sb in self._stop_bytes
        )
        end_reason = None
        # Stateful harmony final-channel close (runs on the full accumulator —
        # channel state spans the turn, not just the tail).
        if self._final_stop is not None and self._final_stop.update(self.acc_bytes):
            should_stop = True
            end_reason = "final_channel_close"
        return Verdict(stop=should_stop, end_reason=end_reason)

    def pop_text(self) -> Optional[str]:
        """New bytes that form valid UTF-8 since the last pop; None in
        buffer_mode or while a multi-byte char is incomplete."""
        if self.buffer_mode or len(self.acc_bytes) <= self._returned_bytes:
            return None
        try:
            text = self.acc_bytes[self._returned_bytes :].decode("utf-8")
        except UnicodeDecodeError:
            return None  # incomplete multi-byte char — wait for next token
        self._returned_bytes = len(self.acc_bytes)
        return text

    def flush(self) -> Optional[str]:
        """Remaining bytes at stream end (buffer_mode content or a final
        partial char), decoded with errors="replace"."""
        if len(self.acc_bytes) > self._returned_bytes:
            text = self.acc_bytes[self._returned_bytes :].decode(
                "utf-8", errors="replace"
            )
            self._returned_bytes = len(self.acc_bytes)
            return text
        return None

    def dump_capture(self, reason: str) -> None:
        from inference import runaway_capture

        meta = self._capture_meta
        if callable(meta):
            try:
                meta = meta()
            except Exception:  # noqa: BLE001 — forensics must not break the loop
                meta = {}
        try:
            runaway_capture.dump_capture(
                self._capture_dir,
                reason,
                self.acc_bytes,
                self.n_tokens,
                meta=dict(meta or {}),
            )
        except Exception:  # noqa: BLE001 — forensics must not break the loop
            logger.exception("runaway capture failed")
