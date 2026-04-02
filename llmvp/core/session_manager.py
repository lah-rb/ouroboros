"""Session Manager — memoryful inference sessions with KV cache persistence.

Each session pins a pool instance and maintains per-turn KV cache state
snapshots. Between turns, the snapshot is saved after generation completes
and restored before the next turn begins — giving the model natural
conversational memory without re-processing the full history.

Sessions have a TTL. Expiry behavior:
- If an active subscription listener exists: push a SessionEvent.
- If no listener: silently release the instance.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from starlette.concurrency import run_in_threadpool

from core.config import get_config
from core.interaction_logger import log_interaction
from inference.tokenizer import get_cached_tokenizer, tokenize_text
from formats.registry import get_renderer as _get_format_renderer
from preprocessing.static_tokens import manager as static_tokens_manager

log = logging.getLogger("llm-mvp")


@dataclass
class SessionState:
    """Internal state for an active memoryful session."""

    instance: Any  # Pinned pool instance
    current_state: Any  # LlamaState from save_state()
    last_assistant_text: str = ""  # Captured generation for next turn prefix
    ttl: int = 300
    listener: Optional[asyncio.Queue] = None  # For expiry event push
    created_at: float = field(default_factory=time.monotonic)
    last_turn_at: float = field(default_factory=time.monotonic)
    turn_count: int = 0


@dataclass
class SessionInfo:
    """Returned when a session is created."""

    session_id: str
    instance_index: int
    ttl_seconds: int


@dataclass
class SessionEvent:
    """Push notification for session lifecycle events."""

    session_id: str
    event_type: str  # "expired" | "error"
    message: str


def _generate_session_id() -> str:
    return uuid.uuid4().hex[:16]


class SessionManager:
    """Manages memoryful inference sessions.

    Each session pins a pool instance and maintains a per-turn
    KV cache state snapshot.
    """

    def __init__(self, backend: Any):
        self._backend = backend
        self._sessions: dict[str, SessionState] = {}
        self._expiry_tasks: dict[str, asyncio.Task] = {}
        self._turn_transition_cache: str | None = None

    @property
    def active_session_count(self) -> int:
        return len(self._sessions)

    def get_session_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def _get_turn_transition(self) -> str:
        """Get the tokens that close a previous assistant turn.

        Derived from the format schema — no template probing needed.
        The result is cached for the lifetime of the SessionManager.
        """
        if self._turn_transition_cache is not None:
            return self._turn_transition_cache

        config = get_config()
        renderer = _get_format_renderer(config.model.family)
        self._turn_transition_cache = renderer.render_turn_transition()
        log.info(
            "Session turn transition from schema: %r (%d chars)",
            self._turn_transition_cache[:60],
            len(self._turn_transition_cache),
        )
        return self._turn_transition_cache

    async def start_session(self, ttl_seconds: int = 300) -> SessionInfo:
        """Acquire instance, save initial state, return session info."""
        instance = await self._backend.acquire_instance()
        session_id = _generate_session_id()

        # The instance already has the static snapshot restored
        # (from acquire_instance). Save this as the session's
        # initial state — it becomes turn 0.
        initial_state = await run_in_threadpool(instance.save_state)

        self._sessions[session_id] = SessionState(
            instance=instance,
            current_state=initial_state,
            ttl=ttl_seconds,
            created_at=time.monotonic(),
            last_turn_at=time.monotonic(),
        )

        # Start TTL expiry timer
        self._expiry_tasks[session_id] = asyncio.create_task(
            self._ttl_monitor(session_id, ttl_seconds)
        )

        log.info(
            "📌 Session %s started (ttl=%ds, pinned instance)",
            session_id,
            ttl_seconds,
        )

        return SessionInfo(
            session_id=session_id,
            instance_index=0,  # We don't expose internal index
            ttl_seconds=ttl_seconds,
        )

    async def session_turn(
        self,
        session_id: str,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        grammar: str | None = None,
        raw: bool = False,
    ) -> AsyncGenerator[str, None]:
        """Execute a turn within a memoryful session (streaming).

        1. Restore the session's saved KV state.
        2. Build continuation tokens: [close previous assistant turn] +
           [new user turn] + [generation prompt].
        3. Generate with reset=False (KV cache preserved).
        4. Save the post-generation KV state for next turn.
        5. Yield tokens as they're generated.

        IMPORTANT: The KV cache from load_state already contains the
        previous turn's generation.  We must NOT re-inject it as a
        message — that would duplicate it in the context.  Instead,
        for continuation turns (turn_count > 0), we only emit the
        transition tokens that close the previous assistant turn and
        open the new user turn.

        When a delimiter_token is configured and raw=False, pre-delimiter
        content is captured as thinking via the GenerationTracker. Only
        post-delimiter content is yielded to the caller.

        When raw=True, all generated tokens are yielded without any
        delimiter processing. This is used by session_turn_complete which
        runs the CRF on the full output for more accurate extraction.
        """
        session = self._sessions.get(session_id)
        if session is None:
            log.error(
                "❌ Session %s not found (active sessions: %s)",
                session_id,
                list(self._sessions.keys()),
            )
            raise ValueError(f"Session {session_id} not found")

        instance = session.instance

        # Restore session state (includes all prior turns)
        await run_in_threadpool(instance.load_state, session.current_state)

        # Build turn tokens — different paths for first turn vs continuation
        config = get_config()
        renderer = _get_format_renderer(config.model.family)

        if session.turn_count == 0:
            # First turn: user message + generation prompt.
            # No prior assistant output to worry about.
            rendered = renderer.render_user(prompt) + renderer.render_generation_prompt()
        else:
            # Continuation turn: the KV cache already contains everything
            # up to and including the previous generation's content tokens.
            # We only need to append:
            #   1. The tokens that close the previous assistant turn
            #   2. The new user message
            #   3. The generation prompt (assistant start)
            transition = self._get_turn_transition()
            user_turn = renderer.render_user(prompt) + renderer.render_generation_prompt()
            rendered = transition + user_turn

        tokenizer = get_cached_tokenizer()
        turn_tokens = tokenize_text(tokenizer, rendered)

        # Build generation kwargs
        gen_kwargs = {}
        if grammar:
            gen_kwargs["grammar"] = grammar

        delim = renderer.delimiter_pattern()

        # Delimiter-aware streaming (only when not in raw mode)
        delim_pattern = None
        thinking_tracker = None
        if delim and not raw:
            from core.inference import _compile_delimiter_pattern, _find_delimiter

            delim_pattern = _compile_delimiter_pattern(delim)
            try:
                from core.generation_tracker import get_tracker

                thinking_tracker = get_tracker()
            except Exception:
                pass

        # Collect generated text for next turn's assistant prefix
        generated_parts: list[str] = []
        buffer = ""
        delimiter_found = False

        # Generate (KV cache has full prior context from load_state)
        async for chunk in self._backend.generate_stream_async(
            instance=instance,
            prompt_tokens=turn_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            **gen_kwargs,
        ):
            generated_parts.append(chunk)

            if not delim:
                # No delimiter — yield everything
                yield chunk
                continue

            if not delimiter_found:
                # Accumulate and look for delimiter (glob-aware, last match)
                buffer += chunk
                if thinking_tracker:
                    thinking_tracker.append_thinking(chunk)

                m = _find_delimiter(delim_pattern, buffer) if delim_pattern else None
                if m:
                    delimiter_found = True
                    if thinking_tracker:
                        thinking_tracker.mark_thinking_complete()
                    # Yield any content after the matched delimiter
                    after = buffer[m.end() :]
                    if after:
                        yield after
                    buffer = ""
            else:
                # Delimiter already found — yield everything
                yield chunk

        # Handle case where delimiter was never found
        if delim and not delimiter_found and buffer:
            # Yield the entire buffer — it's all content
            yield buffer

        # Save post-generation state for next turn
        session.current_state = await run_in_threadpool(instance.save_state)
        session.last_assistant_text = "".join(generated_parts)
        session.last_turn_at = time.monotonic()
        session.turn_count += 1

        log.info(
            "Session %s turn %d complete (%d chars generated, delimiter_found=%s)",
            session_id,
            session.turn_count,
            len(session.last_assistant_text),
            delimiter_found,
        )

    async def session_turn_complete(
        self,
        session_id: str,
        prompt: str,
        max_tokens: int = 256,
        temperature: float = 0.7,
        grammar: str | None = None,
    ) -> tuple[str, int]:
        """Non-streaming session turn — returns full response.

        Collects raw output from the streaming generator (with raw=True
        to skip the legacy regex delimiter stripping), then runs the
        CRF-based _strip_delimiter on the full text. This gives the CRF
        complete context for accurate phase labeling, and avoids the
        double-stripping bug where regex-stripped output was fed to the
        CRF a second time.

        Handles two model behaviors:
          A) thinking <delimiter> response  → return response, capture thinking
          B) response <delimiter>           → return response (delimiter is end-of-turn)

        Returns:
            Tuple of (generated_text, approximate_token_count)
        """
        raw_parts: list[str] = []
        async for chunk in self.session_turn(
            session_id, prompt, max_tokens, temperature, grammar,
            raw=True,
        ):
            raw_parts.append(chunk)

        raw_text = "".join(raw_parts)

        # Capture raw output for training before any post-processing
        from core.interaction_logger import log_raw_generation
        log_raw_generation(
            raw_text=raw_text,
            tokens_generated=max(1, len(raw_text) // 4),
            stop_reason="session_turn",
            prompt_text=prompt,
        )

        config = get_config()
        delim = _get_format_renderer(config.model.family).delimiter_pattern()
        if not delim:
            text = raw_text.strip()
            token_count = max(1, len(text) // 4)
            return text, token_count

        # CRF-based delimiter stripping on the full raw output.
        # The CRF labels each token as D/T/C/E and extracts only
        # the content phase. Thinking is captured via GenerationTracker.
        from core.inference import _strip_delimiter
        text = _strip_delimiter(raw_text)

        log.info(
            "Session %s turn complete: raw=%d chars → content=%d chars",
            session_id,
            len(raw_text),
            len(text) if text else 0,
        )

        token_count = max(1, len(text) // 4) if text else 0

        # Log the session interaction so it appears in interactions.jsonl.
        from core.interaction_logger import log_interaction
        log_interaction(
            prompt=prompt,
            response=text,
            mode=f"session:{session_id}:turn{session.turn_count if (session := self._sessions.get(session_id)) else '?'}",
            extra={
                "raw_text": raw_text,
                "raw_length": len(raw_text),
                "extracted_length": len(text),
                "token_count": token_count,
                "delimiter_configured": bool(delim),
            },
        )

        return text, token_count

    async def end_session(self, session_id: str) -> bool:
        """Release the pinned instance and clean up."""
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False

        # Cancel TTL timer
        task = self._expiry_tasks.pop(session_id, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Release instance back to pool
        await self._backend.release_instance(session.instance)

        log.info(
            "📌 Session %s ended (turns=%d, duration=%.1fs)",
            session_id,
            session.turn_count,
            time.monotonic() - session.created_at,
        )
        return True

    async def register_listener(self, session_id: str) -> Optional[asyncio.Queue]:
        """Register a listener queue for session events (TTL expiry)."""
        session = self._sessions.get(session_id)
        if session is None:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        session.listener = queue
        return queue

    async def _ttl_monitor(self, session_id: str, ttl: int) -> None:
        """Monitor session TTL. Notify if listener exists, else silent cleanup."""
        try:
            while True:
                await asyncio.sleep(ttl)
                session = self._sessions.get(session_id)
                if session is None:
                    return

                elapsed = time.monotonic() - session.last_turn_at
                if elapsed >= ttl:
                    log.warning(
                        "⏰ Session %s expired (TTL=%ds, idle=%.1fs, turns=%d)",
                        session_id,
                        ttl,
                        elapsed,
                        session.turn_count,
                    )
                    if session.listener is not None:
                        event = SessionEvent(
                            session_id=session_id,
                            event_type="expired",
                            message=f"Session expired after {ttl}s inactivity",
                        )
                        await session.listener.put(event)
                    await self.end_session(session_id)
                    return
        except asyncio.CancelledError:
            return

    async def shutdown(self) -> None:
        """End all active sessions during server shutdown."""
        for sid in list(self._sessions.keys()):
            await self.end_session(sid)
