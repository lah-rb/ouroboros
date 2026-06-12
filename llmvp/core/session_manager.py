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
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

from starlette.concurrency import run_in_threadpool

from core.config import get_config
from core.interaction_logger import log_interaction
from inference.tokenizer import get_cached_tokenizer, tokenize_segments, tokenize_text
from inference.repetition import DegenerateGenerationError
from formats.registry import get_renderer as _get_format_renderer

log = logging.getLogger("llm-mvp")


def _think_strip_enabled() -> bool:
    """Whether to strip prior-turn reasoning from session KV (Factor 4).

    On by default; set LLMVP_THINK_STRIP=0 to disable (e.g. A/B validation).
    """
    return os.environ.get("LLMVP_THINK_STRIP", "1") != "0"


def reasoning_span(
    family: str,
    thinking_enabled: bool,
    gen_tokens: list[int],
    gen_start_pos: int,
    tokenizer,
) -> tuple[int, str] | None:
    """Plan a truncate-and-replay reasoning strip for the just-finished turn.

    Returns ``(t0, replay_prefix)``: truncate the KV at position ``t0`` (dropping
    the turn's reasoning + raw answer) and re-eval ``replay_prefix`` + the clean
    answer there, leaving a canonical assistant turn with no reasoning. ``t0`` is
    chosen so the kept prefix (gen-prompt role framing) plus the replay yields the
    canonical form. Returns None when there's nothing to strip (non-thinking
    family/turn, or no reasoning marker — e.g. a truncated turn). Boundary
    markers are single special tokens thanks to canonical framing.

    ``gen_start_pos`` (P) is the KV position of the first generated token.
    """

    def sid(s: str) -> int | None:
        ids = tokenize_text(tokenizer, s, special=True)
        return ids[-1] if ids else None

    if family == "chatml":
        if not thinking_enabled:
            return None
        if sid("</think>") not in gen_tokens:
            return None  # no </think> (no thinking or truncated) → skip
        # Drop the injected "<think>\n" (last tokens of the gen-prompt) + all
        # generated; keep "<|im_start|>assistant\n". Replay = the clean answer.
        open_len = len(tokenize_text(tokenizer, "<think>\n", special=True))
        t0 = gen_start_pos - open_len
        return (t0, "") if t0 >= 0 else None

    if family == "harmony":
        chan_id = sid("<|channel|>")
        if chan_id is None or sum(1 for t in gen_tokens if t == chan_id) < 2:
            return None  # no analysis/commentary channel → nothing to strip
        # Keep the gen-prompt "<|start|>assistant"; drop ALL generated channels
        # (analysis/commentary/final), then replay the canonical final channel.
        return (gen_start_pos, "<|channel|>final<|message|>")

    if family == "gemma":
        if sid("<channel|>") not in gen_tokens:
            return None
        # Keep "<start_of_turn>model\n"; drop the generated preamble + answer,
        # then replay the clean answer.
        return (gen_start_pos, "")

    return None


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
    # Full-replay mode (model.session_full_replay): the exact dynamic
    # token sequence of every completed turn (turn segments + generated
    # tokens), re-prefilled on top of the pristine static snapshot each
    # turn instead of save/load state surgery. A degenerate turn simply
    # never enters the history.
    token_history: list = field(default_factory=list)


def _global_temperature_floor(requested: float, gen_cfg: Any) -> float:
    """Apply the per-model global temperature floor (any request kind).

    A refusal to sample below ``generation.temperature_floor``: requests
    under the floor are raised to it. Distinct from the session-depth
    floor below, which applies on top for deep turns.
    """
    floor = getattr(gen_cfg, "temperature_floor", None)
    if floor and requested < floor:
        return float(floor)
    return requested


def _effective_session_temperature(
    requested: float, turn_count: int, gen_cfg: Any
) -> float:
    """Apply the session temperature floor for deep turns.

    Deep multi-turn sessions are repetition attractors — low requested
    temperatures compound across accumulated KV until the sampler locks
    into a token cycle (live-observed at turn 5-6 even on models not
    otherwise predisposed; sparse MoEs hit it earliest). When the config
    declares ``session_temp_floor``, turns at depth >=
    ``session_temp_floor_after_turn`` (default 2 — the third turn
    onward) sample at no less than the floor. Shallow turns and plain
    completions keep the requested temperature untouched.
    """
    floor = getattr(gen_cfg, "session_temp_floor", None)
    if not floor:
        return requested
    after = getattr(gen_cfg, "session_temp_floor_after_turn", None)
    after = 2 if after is None else after
    if turn_count >= after and requested < floor:
        return float(floor)
    return requested


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

    def _generation_guard(self):
        """The backend's GPU-work guard, or a no-op for backends/fakes
        that don't implement it (e.g. test doubles).

        Session state operations (load_state/save_state) and turns are
        GPU work — they must never run concurrently with a JIT scaling
        operation's spawn/warm-up/teardown.
        """
        guard = getattr(self._backend, "generation_guard", None)
        if guard is None:
            return contextlib.nullcontext()
        return guard()

    async def start_session(self, ttl_seconds: int = 300) -> SessionInfo:
        """Acquire instance, save initial state, return session info."""
        instance = await self._backend.acquire_instance()
        session_id = _generate_session_id()

        # The instance already has the static snapshot restored
        # (from acquire_instance). Save this as the session's
        # initial state — it becomes turn 0. save_state is a multi-GB
        # GPU memcpy — guard it like any other GPU work.
        async with self._generation_guard():
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

        All delimiter/thinking extraction is handled by the FSM labeller
        in session_turn_complete(), which collects the full raw output
        and runs FSM-based phase labeling on the complete text.
        This streaming generator always yields raw chunks — no
        inline delimiter detection.
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

        # The entire turn — load_state, generation, reasoning strip,
        # save_state — is one continuous span of GPU work. Hold ONE
        # generation guard around all of it (the backend's generate
        # wrapper re-enters the guard; nested entries are counter-only)
        # so a JIT scaling operation can neither interleave with the
        # turn nor start mid-turn.
        async with self._generation_guard():
            config = get_config()
            # Hybrid/recurrent policy: per-turn save/load round-trips and
            # tail seq_rm are unsound for recurrent state (it cannot be
            # partially rolled back). Full-replay sessions restore the
            # PRISTINE static snapshot — the one whole-state op the
            # architecture supports — and re-prefill the accumulated
            # token history below.
            full_replay = bool(getattr(config.model, "session_full_replay", False))
            if full_replay:
                static = getattr(self._backend, "static_state", None)
                if static is not None:
                    await run_in_threadpool(instance.load_state, static)
                else:
                    await run_in_threadpool(instance.reset)
            else:
                # Restore session state (includes all prior turns)
                await run_in_threadpool(instance.load_state, session.current_state)

            # Build turn tokens — different paths for first turn vs continuation
            renderer = _get_format_renderer(config.model.family)

            # Global per-model floor first (any request kind), then the
            # session-depth floor on top — deep turns are repetition
            # attractors; see _effective_session_temperature.
            gen_cfg = getattr(config, "generation", None)
            globally_floored = _global_temperature_floor(temperature, gen_cfg)
            if globally_floored != temperature:
                log.info(
                    "🌡️ Session %s turn %d: global floor %.2f -> %.2f",
                    session_id,
                    session.turn_count + 1,
                    temperature,
                    globally_floored,
                )
                temperature = globally_floored
            floored = _effective_session_temperature(
                temperature, session.turn_count, gen_cfg
            )
            if floored != temperature:
                # NB: this module's logger is `log`, not `logger` — the
                # original NameError here detonated only when the floor
                # first APPLIED (gpt-oss's sub-floor turn temps), killed
                # the turn mid-guard, and leaked the pinned session:
                # active=1/limit=1 deadlocked every session flow after.
                log.info(
                    "🌡️ Session %s turn %d: temperature floored %.2f -> %.2f",
                    session_id,
                    session.turn_count + 1,
                    temperature,
                    floored,
                )
                temperature = floored

            # Build the turn as (text, is_framing) segments so structural framing
            # tokenizes as canonical special tokens while the user prompt stays
            # plain text. Continuation turns (turn_count > 0) first emit the
            # transition that closes the previous assistant turn in the KV cache;
            # the first turn has no prior assistant output to close.
            segments: list = []
            if session.turn_count > 0:
                segments += renderer.render_turn_transition_segments()
            segments += renderer.render_user_segments(prompt)
            segments += renderer.render_generation_prompt_segments()

            tokenizer = get_cached_tokenizer()
            turn_tokens = tokenize_segments(tokenizer, segments)
            if full_replay:
                # Re-prefill everything this session has ever evaluated,
                # then this turn — identical token stream to what the KV
                # would have held under state splicing, rebuilt exactly.
                turn_only = turn_tokens
                turn_tokens = list(session.token_history) + turn_only

            # Build generation kwargs
            gen_kwargs = {}
            if grammar:
                gen_kwargs["grammar"] = grammar

            # Session-mode stops: include the fake-assistant-turn opener
            # (e.g. <|start|>assistant for Harmony) so the model stops if it
            # tries to continue generating a second turn after <|end|>. This
            # is the critical guard against the multi-turn rambling pathology
            # that motivated the FSM labeller.
            gen_kwargs["stop_texts"] = renderer.stop_tokens(mode="session")

            # turn_tokens are PURELY incremental — the restored KV already holds
            # the static prefix + prior turns. Without this flag the backend's
            # completion-path slice skipped the first n_static tokens of the TURN
            # whenever the turn was longer than the static prefix, silently
            # amputating the prompt head (the model saw only the tail).
            gen_kwargs["static_in_prompt"] = False

            # We already hold this turn's generation guard — the wrapper's
            # own guard entry must only balance the counter, not re-wait
            # the scaling gate (deadlock against a draining scaler).
            gen_kwargs["_nested_guard"] = True

            # Collect generated text for next turn's assistant prefix
            generated_parts: list[str] = []

            # Generate (KV cache has full prior context from load_state). If the
            # turn degenerates (e.g. the Gemma-4 repetition collapse), the backend
            # raises DegenerateGenerationError — we PURGE and re-raise so the failure
            # surfaces cleanly (GraphQL error → no_answer → mission_control) instead
            # of hanging until max_tokens.
            try:
                async for chunk in self._backend.generate_stream_async(
                    instance=instance,
                    prompt_tokens=turn_tokens,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    **gen_kwargs,
                ):
                    generated_parts.append(chunk)
                    yield chunk

                if full_replay:
                    # No state surgery of any kind: extend the history with
                    # this turn's exact tokens (turn segments + generated ids
                    # exposed by the backend) — the next turn re-prefills it.
                    # strip_reasoning (tail seq_rm) is skipped by design: the
                    # operation is unsound on recurrent state, and the family
                    # configs using full replay are non-thinking.
                    gen_ids = list(
                        getattr(instance, "_last_completion_tokens", None) or []
                    )
                    session.token_history.extend(turn_only)
                    session.token_history.extend(gen_ids)
                else:
                    # Factor 4: strip THIS turn's reasoning from the KV cache
                    # before snapshotting, so prior-turn chain-of-thought never
                    # accumulates across the session (canonical multi-turn: keep
                    # prior answers, drop prior CoT). Truncate-and-replay (tail
                    # seq_rm + re-eval the clean answer). Skips non-thinking
                    # models/turns and truncated turns.
                    if _think_strip_enabled():
                        from core.inference import _strip_delimiter

                        content = _strip_delimiter("".join(generated_parts))
                        await self._maybe_strip_reasoning(instance, content)

                    # Save post-generation state for next turn
                    session.current_state = await run_in_threadpool(instance.save_state)
                session.last_assistant_text = "".join(generated_parts)
                session.last_turn_at = time.monotonic()
                session.turn_count += 1

                log.info(
                    "Session %s turn %d complete (%d chars generated)",
                    session_id,
                    session.turn_count,
                    len(session.last_assistant_text),
                )
            except DegenerateGenerationError as e:
                if full_replay:
                    # Nothing to purge: the history was never extended, so
                    # the degenerate span simply doesn't exist as far as the
                    # next turn's re-prefill is concerned.
                    log.warning(
                        "🛑 Session %s degenerate generation (%s) — full-replay "
                        "mode, degenerate turn dropped from history",
                        session_id,
                        e.reason,
                    )
                    raise
                # PURGE: restore the pre-turn KV. session.current_state was never
                # overwritten (save_state above is skipped on raise), so it still
                # holds the pre-turn snapshot; reloading it discards the degenerate
                # span from the live instance. turn_count/current_state stay as they
                # were before this turn.
                await run_in_threadpool(instance.load_state, session.current_state)
                log.warning(
                    "🛑 Session %s degenerate generation (%s) — purged KV, "
                    "restored pre-turn state",
                    session_id,
                    e.reason,
                )
                raise

    async def _maybe_strip_reasoning(self, instance: Any, content: str) -> None:
        """Strip this turn's reasoning from the KV via truncate-and-replay
        (Factor 4): drop the reasoning + raw answer from ``t0`` to the end, then
        re-eval the canonical clean answer there. No-op when there's nothing to
        strip (non-thinking model/turn, or a truncated turn with no marker).
        """
        gen_tokens = getattr(instance, "_last_completion_tokens", None)
        p = getattr(instance, "_last_gen_start_pos", None)
        if not gen_tokens or p is None:
            return
        config = get_config()
        span = reasoning_span(
            config.model.family,
            config.model.thinking,
            gen_tokens,
            p,
            get_cached_tokenizer(),
        )
        if span is None:
            return
        t0, prefix = span
        replay = tokenize_segments(
            get_cached_tokenizer(), [(prefix, True), (content or "", False)]
        )
        ok = await run_in_threadpool(
            self._backend.strip_reasoning_replay, instance, t0, replay
        )
        if ok:
            log.info(
                "🧹 reasoning strip: truncate@%d, replay %d answer tokens",
                t0,
                len(replay),
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

        Collects raw output from the streaming generator, then runs
        the FSM-based _strip_delimiter on the full text. This gives
        the labeller complete context for accurate phase labeling.

        Handles two model behaviors:
          A) thinking <delimiter> response  → return response, capture thinking
          B) response <delimiter>           → return response (delimiter is end-of-turn)

        Returns:
            Tuple of (generated_text, generated_token_count). The token
            count is the number of chunks yielded by the streaming
            backend — each chunk corresponds to one generated token
            before FSM/delimiter stripping. This is the right signal
            for the caller to detect truncation (count >= max_tokens
            means the generation budget was reached), distinct from
            the post-strip character-based approximation that previous
            versions of this method returned.
        """
        raw_parts: list[str] = []
        async for chunk in self.session_turn(
            session_id,
            prompt,
            max_tokens,
            temperature,
            grammar,
        ):
            raw_parts.append(chunk)

        raw_text = "".join(raw_parts)
        generated_tokens = len(raw_parts)

        # Capture raw output for training before any post-processing
        from core.interaction_logger import log_raw_generation

        log_raw_generation(
            raw_text=raw_text,
            tokens_generated=generated_tokens,
            stop_reason="session_turn",
            prompt_text=prompt,
        )

        config = get_config()
        delim = _get_format_renderer(config.model.family).delimiter_pattern()
        if not delim:
            text = raw_text.strip()
            return text, generated_tokens

        # FSM-based delimiter stripping on the full raw output.
        # The FSM labels each atom as D/T/C/E based on current phase
        # and extracts only the content phase. Thinking is forwarded to
        # the GenerationTracker side-channel.
        from core.inference import _strip_delimiter

        text = _strip_delimiter(raw_text)

        log.info(
            "Session %s turn complete: raw=%d chars / %d tokens → content=%d chars",
            session_id,
            len(raw_text),
            generated_tokens,
            len(text) if text else 0,
        )

        # Derived truncation flag: the generation loop in
        # inference/backends/llama_cpp_backend.py:generate_stream_sync
        # breaks when ``len(completion_tokens) >= effective_max`` (the
        # max_tokens budget). We count chunks yielded as an equivalent
        # proxy. This is the signal that distinguishes "empty response
        # because the model chose to say nothing" from "empty response
        # because the budget ran out mid-analysis channel and FSM
        # stripped the unfinished reasoning" — the bug class that
        # motivated this observability work.
        truncated = generated_tokens >= max_tokens

        # Log the session interaction so it appears in interactions.jsonl.

        log_interaction(
            prompt=prompt,
            response=text,
            mode=f"session:{session_id}:turn{session.turn_count if (session := self._sessions.get(session_id)) else '?'}",
            extra={
                "raw_text": raw_text,
                "raw_length": len(raw_text),
                "extracted_length": len(text),
                "generated_tokens": generated_tokens,
                "max_tokens": max_tokens,
                "truncated": truncated,
                "delimiter_configured": bool(delim),
            },
        )

        return text, generated_tokens

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
