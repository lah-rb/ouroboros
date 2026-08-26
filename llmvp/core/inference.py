#!/usr/bin/env python3
"""
Shared Inference Module

Core LLM inference logic shared between REST and GraphQL APIs.
This module provides a single source of truth for all LLM operations.

Now uses the pluggable backend system for backend-agnostic inference.
"""

import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, List, Optional, Tuple

# Local imports
from core.config import ActiveConfigView, get_config
from inference.backends.factory import get_backend, initialize_backend_async
from preprocessing.static_tokens import manager as static_tokens_manager
from inference.tokenizer import (
    get_cached_tokenizer,
    tokenize_segments,
    build_full_prompt,
    flow_head_tokens,
)
from core.interaction_logger import log_interaction
from formats.registry import get_renderer as _get_format_renderer

# Live view, not a snapshot — a snapshot here outlives a model swap.
config = ActiveConfigView()
log = logging.getLogger("llm-mvp")


@dataclass
class CompletionOutcome:
    """run_completion result with cache-aware token telemetry.

    ``generated_tokens`` is the REAL completion token count (the legacy
    ``tokens_generated`` was a char approximation). ``cached_prefix_tokens`` =
    KV the model skipped prefilling (global static + flow head for stateless;
    full restored occupancy for sessions); ``fresh_prefill_tokens`` = tokens
    actually prefilled this request. All read-only — surfaced from values the
    backend already computed. Defaults are zero so the tool path (no cache
    accounting) and any error path degrade cleanly."""

    text: str
    tokens_generated: int
    prompt_tokens: int = 0
    cached_prefix_tokens: int = 0
    fresh_prefill_tokens: int = 0
    generated_tokens: int = 0
    # Of `generated_tokens`, how many were chain-of-thought rather than the
    # answer the flow consumes. Tokenized from the FSM-extracted thinking span
    # with the model's own tokenizer, so it is comparable to the other counts.
    # 0 means UNKNOWN (no thinking extracted, or the count failed) — it does not
    # prove the model did not reason. content_tokens = generated - reasoning.
    reasoning_tokens: int = 0
    cache_hit: bool = False
    flow_key: str = ""
    # Precise phase timing (server-measured): prefill = prompt-eval (start ->
    # first token), decode = generation (first token -> end). Lets the trace
    # split inference time into prefill vs decode per call. 0 when unavailable.
    prefill_ms: float = 0.0
    decode_ms: float = 0.0
    # Why generation stopped. "" for an ordinary stop. Set to
    # "kv_pressure_truncated" when the batched engine force-windowed the stream
    # to relieve KV pressure — that cut lands BELOW max_tokens, so the derived
    # "tokens_generated >= max_tokens" truncation test cannot see it, and a
    # caller checking only that flag would treat a severed response as complete.
    end_reason: str = ""

    @property
    def truncated_by_engine(self) -> bool:
        """The response was cut short by the engine, not by the model.

        Covers BOTH engine-side cuts, because the caller cannot detect either
        one from token counts: the engine's budget may be lower than the
        caller's ``max_tokens`` (admission sizes against free KV cells), so a
        cut at that budget stops below the caller's number, and a KV-pressure
        force-window stops below it too.
        """
        return self.end_reason in ("kv_pressure_truncated", "length")


# Held back from the computed generation budget: the window must still admit
# the token being decoded plus a little slack for off-by-one in the prefill
# accounting. Small on purpose — this is a rounding guard, not a policy knob.
_GENERATION_SLACK = 64
# Below this a "generation" is not worth starting; the caller gets a clear
# error instead of a stream that emits two tokens and stops.
_MIN_GENERATION_TOKENS = 128


def resolve_max_tokens(requested: "int | None", prepopulated: int = 0) -> int:
    """Canonical request→config→256 max_tokens chain (single source of truth;
    previously copy-pasted at every completion entry point).

    ``prepopulated`` is the context the window already owes before a single
    token is generated — static prefix + rendered dynamic prompt. Pass it and
    the budget is clamped so PROMPT + GENERATION fits the per-stream ceiling.

    WHY THIS EXISTS. The prompt-length guard downstream checks that the prompt
    fits. NOTHING checked that prompt + max_tokens fits, so a config with
    ``max_tokens_default`` equal to ``n_ctx`` (which is what "no artificial
    cap" naturally produces) lets a request ask for more window than exists:
    on 2026-07-27 a 21,085-token prompt was granted a 65,536-token budget
    against a 65,536-token window. Generation then runs until the KV pool
    evicts the stream — and eviction DISCARDS EVERYTHING, so a batch that had
    already emitted 15 complete files returned nothing at all. Clamping turns
    that into an ordinary truncation, which keeps the work.

    Keyed off ``stream_context_limit``, never raw ``n_ctx`` — see the property's
    own docstring: a large pool must not admit a single stream beyond the
    model's trained range.

    NOT SUFFICIENT ALONE under batched decode, where ``n_ctx`` budgets the SUM
    of live streams: this bounds one stream against the whole window, but it
    cannot see the others. Admission-time clamping against live free cells is
    the companion fix.
    """
    want = requested or config.generation.max_tokens_default or 256
    if prepopulated <= 0:
        return want  # caller cannot measure the prompt — unchanged behaviour

    headroom = config.model.stream_context_limit - prepopulated - _GENERATION_SLACK
    if headroom < _MIN_GENERATION_TOKENS:
        raise ValueError(
            f"No room to generate: prompt occupies {prepopulated} of "
            f"{config.model.stream_context_limit} tokens, leaving {headroom} "
            f"after slack — below the {_MIN_GENERATION_TOKENS}-token floor."
        )
    if headroom < want:
        log.info(
            "✂️ max_tokens %d → %d (prompt %d of %d)",
            want,
            headroom,
            prepopulated,
            config.model.stream_context_limit,
        )
        return headroom
    return want


def resolve_temperature(requested: "float | None", label: str = "Completion") -> float:
    """Canonical temperature chain + the global per-model floor (a refusal to
    sample below the configured value for ANY request kind — see
    GenerationConfig). Logs when the floor engages.

    None-checked, not truthiness-checked: an explicit 0.0 means greedy
    and must survive (``requested or default`` silently swallowed it —
    found by the cross-process decode probe, whose local leg could never
    produce a deterministic baseline)."""
    from core.session_manager import _global_temperature_floor

    if requested is None:
        temperature = config.generation.temperature_default or 0.7
    else:
        temperature = requested
    floored = _global_temperature_floor(temperature, config.generation)
    if floored != temperature:
        log.info(
            "🌡️ %s: global temperature floor %.2f -> %.2f", label, temperature, floored
        )
    return floored


def _get_delimiter() -> str:
    """Get the delimiter pattern from the format schema."""
    return _get_format_renderer(config.model.family).delimiter_pattern()


def _render_dynamic_prompt(user_prompt: str) -> str:
    """Render the dynamic portion of a prompt (user turn + generation prompt)."""
    renderer = _get_format_renderer(config.model.family)
    return renderer.render_user(user_prompt) + renderer.render_generation_prompt()


def _resolve_think_hold(reasoning: "str | None") -> "dict | None":
    """Request-level think-hold payload (see inference/think_hold.py), or None.

    Best-effort by construction: a failure here must never fail a request.
    """
    try:
        from inference.think_hold import resolve_think_hold_kwargs
        from inference.tokenizer import get_cached_tokenizer

        return resolve_think_hold_kwargs(
            _get_format_renderer(config.model.family),
            get_cached_tokenizer(),
            reasoning,
        )
    except Exception:  # noqa: BLE001
        log.debug("think_hold resolution failed", exc_info=True)
        return None


def _get_fsm_family() -> str:
    """Return the model family name to drive FSM phase transitions.

    The family determines the FSM's initial phase (Harmony/ChatML start
    in DELIM and transition on channel markers; Tekken/Mistral start
    directly in CONTENT since their generation stream has no thinking
    markers). Read live, never cached — a model swap can change it.
    """
    return config.model.family


def _strip_delimiter(text: str) -> str:
    """Strip delimiter tokens from model output using the FSM labeller.

    The FSM walks the atom stream produced by ``training.featurizer``
    and emits a D/T/C/E label per atom based on the current phase.
    Content atoms (phase = CONTENT) are concatenated and returned;
    thinking atoms (phase = THINKING) are forwarded to the generation
    tracker side-channel.

    Unlike the previous CRF-based implementation, the FSM is:
      - Deterministic: same input always produces the same output.
      - Grammar-correct: <|end|> always resets to DELIM, so multi-turn
        rambling cannot leak analysis prose into content.
      - Zero-dependency at inference time: no model file to load,
        no training artifact to ship.

    Args:
        text: Raw model output.

    Returns:
        Cleaned response text (content phase only). Empty string if no
        content phase was detected.
    """
    # (The gemma rsplit bypass that lived here 2026-08-03→08-21 is gone: the
    # FSM now models the inverted channel form (_ThinkShape.INV_CHANNEL), so
    # gemma rides the generic path below like every other family — which is
    # also what forwards its CoT to the tracker and makes reasoningTokens
    # real for this family for the first time.)
    delim = _get_delimiter()
    if not delim:
        # No delimiter pattern configured for this family — nothing to
        # strip. Return as-is after basic whitespace trim.
        return text.strip()

    try:
        from core.fsm_labeller import fsm_extract_phases
        from core.generation_tracker import get_tracker

        family = _get_fsm_family()
        phases = fsm_extract_phases(text, family=family)
        content = phases.get("C", "").strip()
        thinking = phases.get("T", "").strip()

        log.debug(
            "FSM extraction: input=%r → content=%r thinking=%r",
            text[:80] if len(text) > 80 else text,
            content[:80] if len(content) > 80 else content,
            thinking[:40] if len(thinking) > 40 else thinking,
        )

        if thinking:
            tracker = get_tracker()
            # Dual-path: streaming inference calls append_thinking
            # during the stream (while ``active`` is True), then
            # finish() promotes thinking_content to _last_thinking.
            # Session inference yields raw chunks unchanged and
            # reaches this FSM extraction AFTER finish() has already
            # run, so _last_thinking was captured empty. promote_thinking
            # writes the FSM-extracted text directly to _last_thinking
            # so the GraphQL thinking endpoint returns it. We still
            # append here for live status queries that happen between
            # FSM extraction and the next generation's start() call.
            tracker.append_thinking(thinking)
            tracker.mark_thinking_complete()
            tracker.promote_thinking(thinking)

        return content

    except Exception as e:
        # FSM failures are unexpected — the labeller is deterministic
        # and shouldn't raise on well-formed or even malformed input.
        # If it does, log prominently and return raw text so the caller
        # can still see something rather than silently empty output.
        log.error("❌ FSM extraction failed: %s — returning raw text", e)
        return text.strip()


def _approximate_token_count(text: str) -> int:
    """
    Approximate token count from generated text.

    This is a rough estimate - actual token count depends on the tokenizer.

    Args:
        text: Generated text

    Returns:
        int: Approximate token count
    """
    # Rough approximation: ~4 characters per token on average
    return max(1, len(text) // 4)


async def _get_backend(model: Optional[str] = None):
    """
    Get or initialize the backend instance.

    ``model`` names a HOT SECONDARY (Phase 2b resident registry) to serve
    this request instead of the primary. Absent or naming the active config
    means the primary, which is every pre-existing caller — so the eight
    call sites that pass nothing keep their exact behaviour.

    Under normal operation the backend is already initialized by
    ``startup_event`` in the API layer.  This fallback exists only
    as a safety-net (e.g. during tests) — it properly **awaits**
    the async initializer so the pool is fully ready before
    returning.

    Returns:
        The backend instance

    Raises:
        RuntimeError: If initialization fails
    """
    from core.model_swap import ModelSwapInProgress, swap_in_progress

    state = swap_in_progress()
    if state is not None:
        # Reject retriably: agent-side dispatch retries failed inferences
        # unchanged (proven through the KV-eviction and restart windows),
        # so the request succeeds once the swap gate reopens.
        raise ModelSwapInProgress(f"model swap in progress ({state}) — retry shortly")

    if model:
        from core.remote_router import resolve_local_backend

        secondary = resolve_local_backend(model)
        if secondary is not None:
            return secondary

    backend = get_backend()
    if backend is None:
        log.warning(
            "⚠️ Backend not yet initialized — triggering async "
            "initialization from inference layer (should only happen "
            "in tests or unusual startup sequences)"
        )
        backend = await initialize_backend_async(get_config())
    return backend


async def run_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
    static_prefix: Optional[str] = None,
    flow_key: Optional[str] = None,
    reasoning: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Run a non-streaming completion.

    Args:
        prompt: User prompt text
        max_tokens: Maximum tokens to generate (uses config default if None)
        temperature: Sampling temperature (uses config default if None)
        request_id: Caller correlation id, published on the health endpoint
            for the life of this generation. The single-slot generation
            tracker cannot otherwise tell a polling client whose numbers it
            is reading. None -> "" (unknown), the pre-existing behavior.

    Returns:
        Tuple of (generated_text, approximate_token_count)

    Raises:
        RuntimeError: If static buffer not loaded
        ValueError: If prompt is empty or exceeds context window
    """
    if not prompt:
        raise ValueError("`prompt` must be a non-empty string")

    max_tokens = resolve_max_tokens(max_tokens)
    temperature = resolve_temperature(temperature)

    static_tokens = static_tokens_manager.get_static_tokens()

    # DOUBLE-INCLUDE GUARD (2026-07-30). The contract is: `static_prefix` is the
    # invariant head, `prompt` is the DYNAMIC TAIL ONLY — the server
    # concatenates. A client that also leads its prompt with the head text gets
    # the head TWICE: the flow cache then skips the pinned copy and dutifully
    # prefills the duplicate, so a "HIT" costs MORE than a cold call while
    # reporting cacheHit=true — measured live on the 2026-07-30 flow pilot
    # (hit fresh 3,906 ≈ cold 3,907, each hit 2.3s SLOWER, total_ctx 7,947 =
    # static 1,786 + head 2,255 + duplicated head+tail 3,906). Heal it loudly:
    # the model seeing the head twice is wrong on the UNCACHED path too.
    if static_prefix and prompt.startswith(static_prefix):
        log.warning(
            "🧩 static_prefix DUPLICATED at the head of `prompt` (%d chars) — "
            "stripping the copy. The contract: prompt is the dynamic tail only; "
            "the server prepends static_prefix. A duplicated head makes a flow "
            "HIT cost more than a cold call while reporting cacheHit=true.",
            len(static_prefix),
        )
        prompt = prompt[len(static_prefix) :]

    # Build complete prompt BEFORE acquiring instance to minimize pool hold time
    tokenizer = get_cached_tokenizer()
    flow_kwargs: dict = {}
    if getattr(config.model, "flow_kv_cache", False) and flow_key and static_prefix:
        # Render the full turn once — the token sequence is IDENTICAL to the
        # non-cached path, so output is unchanged; caching only splits where the
        # KV gets computed. Then find the stable token prefix determined solely
        # by static_prefix (not by what follows) via two probes with different
        # tails — robust to tokenizer boundary merges. That prefix (after the
        # global static buffer) is what the backend pins per flow_key.
        dynamic_ids = build_full_prompt(
            static_prefix + prompt, tokenizer, reasoning=reasoning
        )
        n = len(flow_head_tokens(static_prefix, tokenizer, confirm_with=dynamic_ids))
        if n > 0:
            flow_kwargs = {
                "flow_key": flow_key,
                "flow_prefix_len": len(static_tokens) + n,
            }
    else:
        # static_prefix carries the per-flow cache:true sections (instructions,
        # OUTPUT FORMAT, examples). The client sends it separately from `prompt`
        # UNCONDITIONALLY, expecting the server to prepend it. With flow_kv_cache
        # off (or no flow_key) we don't PIN its KV, but we must still INCLUDE it —
        # otherwise the model runs on the global static buffer + dynamic tail only
        # and never sees the per-flow instructions (e.g. design then invents a
        # schema because "there is no output format in the prompt"). Prepending it
        # uncached makes the token sequence identical to the cached path; only the
        # KV-reuse differs. (Regression introduced with the static-prefix split.)
        dynamic_ids = build_full_prompt(
            (static_prefix or "") + prompt, tokenizer, reasoning=reasoning
        )
    total_len = len(static_tokens) + len(dynamic_ids)

    if total_len > config.model.stream_context_limit:
        raise ValueError(
            f"Combined prompt length ({total_len}) exceeds the model's "
            f"per-stream context limit of {config.model.stream_context_limit} tokens."
        )

    # The prompt fits; now make the GENERATION fit alongside it. Re-resolving
    # is idempotent for the default chain and only ever lowers the budget.
    max_tokens = resolve_max_tokens(max_tokens, prepopulated=total_len)

    full_prompt = list(static_tokens) + dynamic_ids

    # Get backend and acquire instance (if backend uses manual pooling)
    backend = await _get_backend()
    instance = None

    if backend.capabilities.manual_pooling:
        instance = await backend.acquire_instance()

    try:
        # Build extra kwargs for grammar support
        gen_kwargs = {}
        if grammar:
            gen_kwargs["grammar"] = grammar
        gen_kwargs.update(flow_kwargs)
        if request_id:
            gen_kwargs["request_id"] = str(request_id)
        # Per-request reasoning HEAD-SWAP for stateless completions. Skipped
        # when a flow prefix is pinned this request — that KV was computed
        # above the DEFAULT head, so swapping under it would misalign.
        if reasoning and not flow_kwargs:
            gen_kwargs["reasoning"] = str(reasoning)

        # Think-hold (inference/think_hold.py): when this request's genprompt
        # prefills an advisory think opener (laguna), ban the close tag for
        # the first N tokens. No-op for every other family/level.
        _hold = _resolve_think_hold(reasoning)
        if _hold:
            gen_kwargs["think_hold"] = _hold

        # Use backend's async generation. gen_target is where the backend
        # stashes per-request cache telemetry (_last_*) — the pooled instance,
        # or the backend itself when not manually pooled.
        gen_target = instance or backend
        answer = await backend.generate_async(
            instance=gen_target,
            prompt_tokens=full_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **gen_kwargs,
        )

        # Capture raw output for training before any post-processing
        from core.interaction_logger import log_raw_generation

        log_raw_generation(
            raw_text=answer,
            tokens_generated=_approximate_token_count(answer),
            stop_reason="completion",
            prompt_text=prompt,
        )

        # Strip delimiter if configured
        # Right before _strip_delimiter
        log.info(
            "run_completion: raw answer len=%d, first100=%r", len(answer), answer[:100]
        )
        answer = _strip_delimiter(answer)
        log.info(
            "run_completion: after strip len=%d, first100=%r", len(answer), answer[:100]
        )

        # Cache-aware token telemetry from the backend stash (read-only). The
        # real generated count replaces the char approximation. Falls back to
        # the approximation if the stash is absent (e.g. a backend that doesn't
        # set _last_*), so behavior degrades cleanly.
        real_gen = len(getattr(gen_target, "_last_completion_tokens", []) or [])
        cached_prefix = int(getattr(gen_target, "_last_kv_base", 0) or 0)
        fresh_prefill = int(getattr(gen_target, "_last_dynamic_len", 0) or 0)
        tokens_generated = real_gen or _approximate_token_count(answer)

        # Precise phase timing from the generation tracker (just-finished gen).
        from core.generation_tracker import get_tracker

        _diag = get_tracker().get_last_diagnostics()

        # Log interaction (non-streaming). Attach the FSM-extracted thinking
        # (promoted to the tracker moments ago) — the stripped `answer` alone
        # lost every long CoT (OLMo's 21k-token design think, 2026-07-23).
        # Capped to bound file growth on marathon thinks.
        _think = (get_tracker().get_thinking() or {}).get("content", "") or ""
        log_interaction(
            prompt=prompt,
            response=answer,
            mode="non-stream",
            extra={"thinking": _think[:200_000]} if _think else None,
        )

        # ── CoT vs AGENT-TURN SPLIT ──────────────────────────────────
        # generated_tokens alone cannot distinguish a model that reasoned for
        # 12k tokens and answered in 300 from one that wrote 12k of content, and
        # on a heavy-thinking fleet that is the difference that matters. The
        # glm-4.7-flash arm spent 82% of its output on thought (471,573 raw
        # chars -> 85,746 content) and that had to be derived by hand from
        # server-log character counts after the fact.
        #
        # TOKENIZED, NOT ESTIMATED. `_approximate_token_count` is whitespace
        # splitting; mixing it into a register set that is otherwise exact
        # backend counts is how a figure gets quoted as measured when it is not.
        # Best-effort: any failure leaves 0, and the field is documented as
        # "0 = unknown", never "0 = no reasoning".
        reasoning_tokens = 0
        if _think:
            try:
                reasoning_tokens = len(backend.tokenize(_think, special=False))
            except Exception:  # noqa: BLE001 — telemetry must never fail a call
                log.debug("reasoning-token count unavailable", exc_info=True)
        if real_gen and reasoning_tokens:
            log.info(
                "🧠 CoT split: %d reasoning + %d content = %d generated (%.0f%% thought)",
                reasoning_tokens,
                max(0, real_gen - reasoning_tokens),
                real_gen,
                reasoning_tokens * 100.0 / real_gen,
            )

        return CompletionOutcome(
            text=answer,
            tokens_generated=tokens_generated,
            prompt_tokens=cached_prefix + fresh_prefill,
            cached_prefix_tokens=cached_prefix,
            fresh_prefill_tokens=fresh_prefill,
            generated_tokens=real_gen,
            reasoning_tokens=reasoning_tokens,
            cache_hit=bool(getattr(gen_target, "_last_cache_hit", False)),
            flow_key=str(getattr(gen_target, "_last_flow_key", "") or ""),
            end_reason=str(getattr(gen_target, "_last_end_reason", "") or ""),
            # Prefer the per-stream wall spans (batched seats stash them —
            # concurrency-accurate); fall back to the global tracker's
            # single-generation timing for the pool path.
            prefill_ms=round(
                (
                    getattr(gen_target, "_last_prefill_s", 0)
                    or _diag.get("eval_duration", 0)
                    or 0
                )
                * 1000,
                1,
            ),
            decode_ms=round(
                (
                    getattr(gen_target, "_last_decode_s", 0)
                    or _diag.get("generation_duration", 0)
                    or 0
                )
                * 1000,
                1,
            ),
        )

    finally:
        if backend.capabilities.manual_pooling and instance is not None:
            await backend.release_instance(instance)


async def run_raw_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
    reasoning: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Run a non-streaming completion that returns raw model output.

    Identical to run_completion but SKIPS delimiter stripping.
    Returns the full model output including channel markers, thinking
    text, and delimiter tokens. Used for training data collection.

    ``reasoning`` applies the same per-request head-swap run_completion does.
    It was MISSING here until 2026-07-26, and the omission was silent: the
    GraphQL CompletionRequest advertises a `reasoning` field, the raw resolver
    accepted it, and then dropped it on the floor. Anything driving level
    comparisons through rawCompletion therefore got N identical copies while
    believing it had N levels — which is exactly what happened to the
    adaptive_thinking counterfactual corpus regeneration (4,107 requests, three
    "levels", CoT lengths 764/779/781 = noise).

    Args:
        prompt: User prompt text
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature
        grammar: Optional grammar constraint
        reasoning: Reasoning level for the per-request head-swap

    Returns:
        Tuple of (raw_text, approximate_token_count)
    """
    if not prompt:
        raise ValueError("`prompt` must be a non-empty string")

    max_tokens = resolve_max_tokens(max_tokens)
    temperature = resolve_temperature(temperature)

    static_tokens = static_tokens_manager.get_static_tokens()

    tokenizer = get_cached_tokenizer()
    dynamic_ids = build_full_prompt(prompt, tokenizer)
    total_len = len(static_tokens) + len(dynamic_ids)

    if total_len > config.model.stream_context_limit:
        raise ValueError(
            f"Combined prompt length ({total_len}) exceeds the model's "
            f"per-stream context limit of {config.model.stream_context_limit} tokens."
        )

    # The prompt fits; now make the GENERATION fit alongside it. Re-resolving
    # is idempotent for the default chain and only ever lowers the budget.
    max_tokens = resolve_max_tokens(max_tokens, prepopulated=total_len)

    full_prompt = list(static_tokens) + dynamic_ids

    backend = await _get_backend()
    instance = None

    if backend.capabilities.manual_pooling:
        instance = await backend.acquire_instance()

    try:
        gen_kwargs = {}
        if grammar:
            gen_kwargs["grammar"] = grammar
        # Per-request reasoning HEAD-SWAP, mirroring run_completion. There is
        # no flow-prefix case on the raw path, so no pinned-KV guard is needed.
        if reasoning:
            gen_kwargs["reasoning"] = str(reasoning)
        _hold = _resolve_think_hold(reasoning)
        if _hold:
            gen_kwargs["think_hold"] = _hold

        answer = await backend.generate_async(
            instance=instance or backend,
            prompt_tokens=full_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **gen_kwargs,
        )

        # NO delimiter stripping — return raw output
        tokens_generated = _approximate_token_count(answer)

        log_interaction(prompt=prompt, response=answer, mode="raw-capture")
        return answer, tokens_generated

    finally:
        if backend.capabilities.manual_pooling and instance is not None:
            await backend.release_instance(instance)


async def stream_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
) -> AsyncGenerator[Tuple[str, bool], None]:
    """
    Run a streaming completion.

    Args:
        prompt: User prompt text
        max_tokens: Maximum tokens to generate (uses config default if None)
        temperature: Sampling temperature (uses config default if None)

    Yields:
        Tuple of (text_chunk, is_complete)

    Raises:
        RuntimeError: If static buffer not loaded
        ValueError: If prompt is empty or exceeds context window
    """
    if not prompt:
        raise ValueError("`prompt` must be a non-empty string")

    max_tokens = resolve_max_tokens(max_tokens)
    temperature = resolve_temperature(temperature)

    static_tokens = static_tokens_manager.get_static_tokens()

    # Build complete prompt BEFORE acquiring instance to minimize pool hold time
    tokenizer = get_cached_tokenizer()
    dynamic_ids = build_full_prompt(prompt, tokenizer)
    total_len = len(static_tokens) + len(dynamic_ids)

    if total_len > config.model.stream_context_limit:
        raise ValueError(
            f"Combined prompt length ({total_len}) exceeds the model's "
            f"per-stream context limit of {config.model.stream_context_limit} tokens."
        )

    # The prompt fits; now make the GENERATION fit alongside it. Re-resolving
    # is idempotent for the default chain and only ever lowers the budget.
    max_tokens = resolve_max_tokens(max_tokens, prepopulated=total_len)

    full_prompt = list(static_tokens) + dynamic_ids

    # Get backend and acquire instance (if backend uses manual pooling)
    backend = await _get_backend()
    instance = None

    if backend.capabilities.manual_pooling:
        instance = await backend.acquire_instance()

    try:
        delim = _get_delimiter()
        buffer = ""
        started = False
        captured_chunks: List[str] = []

        # Import tracker for thinking capture
        thinking_tracker = None
        if delim:
            from core.generation_tracker import get_tracker

            thinking_tracker = get_tracker()

        # Use backend's async streaming generation
        async for chunk in backend.generate_stream_async(
            instance=instance or backend,
            prompt_tokens=full_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            if not delim:
                # No delimiter, yield everything
                if chunk:
                    captured_chunks.append(chunk)
                yield (chunk, False)
                continue

            if not started:
                # Still looking for delimiter — accumulate as thinking
                buffer += chunk
                if thinking_tracker:
                    thinking_tracker.append_thinking(chunk)
                idx = buffer.find(delim)
                if idx != -1:
                    started = True
                    if thinking_tracker:
                        thinking_tracker.mark_thinking_complete()
                    after = buffer[idx + len(delim) :]
                    if after:
                        captured_chunks.append(after)
                        yield (after, False)
                    buffer = ""  # Clear buffer after finding delimiter
            else:
                # Delimiter found, yield everything
                if chunk:
                    captured_chunks.append(chunk)
                yield (chunk, False)

        # Handle case where delimiter was never found
        if delim and not started and buffer:
            log.warning(
                f"⚠️ Delimiter token {delim!r} never appeared during streaming."
            )
            captured_chunks.append(buffer.strip())
            yield (buffer.strip(), False)

        # Capture raw output for training (full text including thinking)
        from core.interaction_logger import log_raw_generation

        full_raw = (
            buffer + "".join(captured_chunks) if delim else "".join(captured_chunks)
        )
        if full_raw:
            log_raw_generation(
                raw_text=full_raw,
                stop_reason="stream",
                prompt_text=prompt,
            )

        # Signal completion. Thinking attached for the same reason as the
        # non-stream site: the joined chunks are post-strip, and the raw
        # capture lives in a separate file most analyses never open.
        if captured_chunks:
            from core.generation_tracker import get_tracker as _get_tracker

            _think = (_get_tracker().get_thinking() or {}).get("content", "") or ""
            log_interaction(
                prompt=prompt,
                response="".join(captured_chunks),
                mode="stream",
                extra={"thinking": _think[:200_000]} if _think else None,
            )
        yield ("", True)

    finally:
        if backend.capabilities.manual_pooling and instance is not None:
            await backend.release_instance(instance)


# ------------------------------------------------------------------
# Tool-augmented inference
# ------------------------------------------------------------------


def _build_messages_tokens(messages: list, tokenizer) -> list:
    """Tokenize a multi-turn message list using the format renderer.

    Built from (text, is_framing) segments so structural framing tokenizes as
    canonical special tokens while message content stays plain text.
    """
    renderer = _get_format_renderer(config.model.family)
    segments: list = []
    for msg in messages:
        role = msg.get("role", "user")
        content = str(msg.get("content", ""))
        if role == "system":
            segments += renderer.render_message_segments("system", content)
        elif role == "assistant":
            segments += renderer.render_assistant_history_segments(content)
        elif role == "tool":
            # Tool results rendered as user messages for simplicity
            segments += renderer.render_user_segments(content)
        else:
            segments += renderer.render_user_segments(content)
    segments += renderer.render_generation_prompt_segments()
    return tokenize_segments(tokenizer, segments)


async def run_chat_completion(
    messages: list,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
) -> Tuple[str, int]:
    """Run a non-streaming completion from an OpenAI-style ``messages`` list.

    Renders the full conversation (system/user/assistant/tool turns) through the
    model's format renderer — the same path as the multi-turn/tool loop — so
    external agents that speak OpenAI chat (e.g. terminal-bench's Terminus) can
    drive the model. Prepends the static-knowledge prefix like every other path;
    run the server with ``--skip-knowledge`` for a bare model (no SOUL.md
    persona) when the external scaffold supplies its own system prompt.

    Returns: (generated_text, approximate_token_count).
    """
    if not isinstance(messages, list) or not messages:
        raise ValueError("`messages` must be a non-empty list")

    max_tokens = resolve_max_tokens(max_tokens)
    temperature = resolve_temperature(temperature)
    temperature = resolve_temperature(temperature, label="ChatCompletion")

    # Build complete prompt BEFORE acquiring instance (mirror run_completion /
    # run_tool_completion: static knowledge prefix + rendered conversation).
    static_tokens = static_tokens_manager.get_static_tokens()
    tokenizer = get_cached_tokenizer()
    dynamic_ids = _build_messages_tokens(messages, tokenizer)
    full_prompt = list(static_tokens) + dynamic_ids
    if len(full_prompt) > config.model.stream_context_limit:
        raise ValueError(
            f"Combined prompt length ({len(full_prompt)}) exceeds the model's "
            f"per-stream context limit of {config.model.stream_context_limit} tokens."
        )

    backend = await _get_backend()
    instance = None
    if backend.capabilities.manual_pooling:
        instance = await backend.acquire_instance()
    try:
        gen_kwargs = {"grammar": grammar} if grammar else {}
        answer = await backend.generate_async(
            instance=instance or backend,
            prompt_tokens=full_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            **gen_kwargs,
        )
        answer = _strip_delimiter(answer)
        tokens_generated = _approximate_token_count(answer)
        log_interaction(prompt=str(messages)[:500], response=answer, mode="chat")
        return answer, tokens_generated
    finally:
        if backend.capabilities.manual_pooling and instance is not None:
            await backend.release_instance(instance)


async def run_tool_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Run a completion with tool-call support.

    If the model emits ``<tool_call>...</tool_call>`` in its response the
    server executes the tool, injects the result, and re-prompts the model.
    Loops up to ``config.tools.max_iterations`` times.

    Falls back to plain ``run_completion()`` when tools are disabled.

    ``request_id`` labels EVERY iteration of the loop — they are all the same
    logical request from the caller's side, so a polling client stays matched
    across the tool round-trips rather than losing its identity mid-turn.
    """
    from tools.protocol import parse_tool_call, has_tool_call, format_tool_result
    from tools.registry import get_registry

    tools_cfg = config.tools
    if not tools_cfg.enabled:
        return await run_completion(
            prompt, max_tokens, temperature, request_id=request_id
        )

    max_tokens = resolve_max_tokens(max_tokens)
    temperature = resolve_temperature(temperature)
    registry = get_registry()

    # Start with the user's original prompt
    messages: list = [{"role": "user", "content": prompt}]
    total_tokens = 0

    for iteration in range(tools_cfg.max_iterations):
        # Build full token sequence: static knowledge + conversation
        static_tokens = static_tokens_manager.get_static_tokens()
        tokenizer = get_cached_tokenizer()
        dynamic_ids = _build_messages_tokens(messages, tokenizer)
        full_prompt = list(static_tokens) + dynamic_ids

        if len(full_prompt) > config.model.stream_context_limit:
            raise ValueError(
                f"Combined prompt length ({len(full_prompt)}) exceeds "
                f"per-stream context limit ({config.model.stream_context_limit})."
            )

        backend = await _get_backend()
        instance = None
        if backend.capabilities.manual_pooling:
            instance = await backend.acquire_instance()

        try:
            answer = await backend.generate_async(
                instance=instance or backend,
                prompt_tokens=full_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                **({"request_id": str(request_id)} if request_id else {}),
            )
            answer = _strip_delimiter(answer)
            total_tokens += _approximate_token_count(answer)
        finally:
            if backend.capabilities.manual_pooling and instance is not None:
                await backend.release_instance(instance)

        # Check for a tool call in the output
        if not has_tool_call(answer):
            # No tool call — we're done
            log_interaction(prompt=prompt, response=answer, mode="tool")
            return CompletionOutcome(
                text=answer,
                tokens_generated=total_tokens,
                generated_tokens=total_tokens,
            )

        tc = parse_tool_call(answer)
        if tc is None:
            # Malformed tool call — return as-is
            log_interaction(prompt=prompt, response=answer, mode="tool")
            return CompletionOutcome(
                text=answer,
                tokens_generated=total_tokens,
                generated_tokens=total_tokens,
            )

        # Execute the tool
        log.info("🔧 Tool call [iter %d]: %s(%s)", iteration + 1, tc.name, tc.params)
        try:
            result_json = registry.execute(tc.name, tc.params)
        except KeyError:
            result_json = f'{{"error": "Unknown tool: {tc.name}"}}'
        except Exception as exc:
            result_json = f'{{"error": "{exc}"}}'

        # Build the next conversation turn
        # Strip everything after the tool call from the assistant message
        pre_call = answer[: answer.index(tc.raw_match)].strip()
        assistant_msg = f"{pre_call}\n{tc.raw_match}" if pre_call else tc.raw_match
        messages.append({"role": "assistant", "content": assistant_msg})
        messages.append(
            {"role": "user", "content": format_tool_result(tc.name, result_json)}
        )

    # Exhausted iterations — return last answer
    log.warning("⚠️ Tool loop hit max iterations (%d)", tools_cfg.max_iterations)
    log_interaction(prompt=prompt, response=answer, mode="tool-max-iter")
    # Same shape as every other exit — the GraphQL resolver reads .text, so a
    # bare tuple here crashed completion(use_tools=true) on iteration exhaustion.
    return CompletionOutcome(
        text=answer,
        tokens_generated=total_tokens,
        generated_tokens=total_tokens,
    )


async def stream_tool_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
) -> AsyncGenerator[Tuple[str, bool], None]:
    """
    Streaming completion with inline tool detection.

    Streams tokens to the client in real-time.  A parallel buffer is
    scanned for ``<tool_call>`` markers.  If one appears mid-stream:

    1. Tokens before the marker have already been yielded (natural UX).
    2. The marker + JSON payload are accumulated silently.
    3. The tool is executed server-side.
    4. A new generation is started with the tool result injected, and
       its tokens resume streaming to the client.

    Falls back to plain ``stream_completion()`` when tools are disabled.
    """
    from tools.protocol import (
        TOOL_CALL_START,
        TOOL_CALL_END,
        parse_tool_call,
        format_tool_result,
    )
    from tools.registry import get_registry

    tools_cfg = config.tools
    if not tools_cfg.enabled:
        async for chunk in stream_completion(prompt, max_tokens, temperature):
            yield chunk
        return

    max_tokens = resolve_max_tokens(max_tokens)
    temperature = resolve_temperature(temperature)
    registry = get_registry()

    # Conversation turns for multi-round tool use
    messages: list = [{"role": "user", "content": prompt}]

    for iteration in range(tools_cfg.max_iterations):
        # Build token sequence
        static_tokens = static_tokens_manager.get_static_tokens()
        tokenizer = get_cached_tokenizer()
        dynamic_ids = _build_messages_tokens(messages, tokenizer)
        full_prompt = list(static_tokens) + dynamic_ids

        if len(full_prompt) > config.model.stream_context_limit:
            raise ValueError(
                f"Combined prompt length ({len(full_prompt)}) exceeds "
                f"per-stream context limit ({config.model.stream_context_limit})."
            )

        backend = await _get_backend()
        instance = None
        if backend.capabilities.manual_pooling:
            instance = await backend.acquire_instance()

        try:
            accumulated = ""  # Full text for this generation
            yielded_up_to = 0  # How much we've already sent to client
            found_tool = False

            async for chunk in backend.generate_stream_async(
                instance=instance or backend,
                prompt_tokens=full_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            ):
                # Handle delimiter stripping on first chunk
                delim = _get_delimiter()
                if delim and not accumulated and chunk:
                    idx = chunk.find(delim)
                    if idx != -1:
                        chunk = chunk[idx + len(delim) :]

                accumulated += chunk

                # Check if we've started seeing a tool call marker
                start_idx = accumulated.find(TOOL_CALL_START)
                if start_idx != -1:
                    # Yield everything BEFORE the marker that hasn't
                    # been yielded yet
                    if start_idx > yielded_up_to:
                        pre = accumulated[yielded_up_to:start_idx]
                        if pre:
                            yield (pre, False)
                        yielded_up_to = start_idx

                    # Check if the closing marker has arrived
                    end_idx = accumulated.find(TOOL_CALL_END, start_idx)
                    if end_idx != -1:
                        # Full tool call captured — stop streaming
                        found_tool = True
                        break
                    # else: keep accumulating until </tool_call> arrives
                else:
                    # No tool call marker yet — safe to yield new text
                    if len(accumulated) > yielded_up_to:
                        new_text = accumulated[yielded_up_to:]
                        # Hold back a small buffer in case TOOL_CALL_START
                        # is being received across chunk boundaries
                        safety = len(TOOL_CALL_START)
                        if len(new_text) > safety:
                            emit = new_text[:-safety]
                            yield (emit, False)
                            yielded_up_to += len(emit)
        finally:
            if backend.capabilities.manual_pooling and instance is not None:
                await backend.release_instance(instance)

        if not found_tool:
            # No tool call — flush remaining buffer and finish
            if len(accumulated) > yielded_up_to:
                remaining = accumulated[yielded_up_to:].strip()
                if remaining:
                    yield (remaining, False)
            log_interaction(
                prompt=prompt, response=accumulated.strip(), mode="stream-tool"
            )
            yield ("", True)
            return

        # ── Tool call detected — execute it ──────────────────
        tc = parse_tool_call(accumulated)
        if tc is None:
            # Malformed — flush everything and finish
            if len(accumulated) > yielded_up_to:
                yield (accumulated[yielded_up_to:], False)
            log_interaction(
                prompt=prompt, response=accumulated.strip(), mode="stream-tool"
            )
            yield ("", True)
            return

        log.info(
            "🔧 Stream tool call [iter %d]: %s(%s)", iteration + 1, tc.name, tc.params
        )
        try:
            result_json = registry.execute(tc.name, tc.params)
        except KeyError:
            result_json = f'{{"error": "Unknown tool: {tc.name}"}}'
        except Exception as exc:
            result_json = f'{{"error": "{exc}"}}'

        # Build next conversation turn
        pre_call = accumulated[: accumulated.index(tc.raw_match)].strip()
        assistant_msg = f"{pre_call}\n{tc.raw_match}" if pre_call else tc.raw_match
        messages.append({"role": "assistant", "content": assistant_msg})
        messages.append(
            {"role": "user", "content": format_tool_result(tc.name, result_json)}
        )
        # Loop continues — next iteration streams the tool-augmented response

    # Exhausted iterations
    log.warning("⚠️ Stream tool loop hit max iterations (%d)", tools_cfg.max_iterations)
    yield ("", True)


def get_health_status() -> dict:
    """
    Get health status information.

    Returns:
        dict with status, pool_size, and available_instances
    """
    backend = get_backend()
    if backend is None:
        return {
            "status": "initializing",
            "pool_size": config.working_seats,
            "available_instances": 0,
        }

    return backend.get_health_status()


async def refresh_context(reason: str = "manual") -> dict:
    """Trigger an in-process llama.cpp context refresh.

    Drops + rebuilds the inference context (keeping weights loaded) to clear the
    LLMVP-process-level output rot ("souring") without a process restart or reboot.
    Returns the backend's status dict; a no-op on backends that don't support it.
    """
    backend = get_backend()
    if backend is None:
        return {"refreshed": 0, "reason": reason, "status": "not_initialized"}
    if not hasattr(backend, "refresh_context"):
        return {"refreshed": 0, "reason": reason, "status": "unsupported"}
    return await backend.refresh_context(reason)


# ══════════════════════════════════════════════════════════════════════
# Vision (mtmd) — a deliberately separate path
#
# This does NOT reuse _build_messages_tokens. That function flattens each
# message to `str(msg.get("content"))`, which turns a list-of-parts into
# "[{'type': 'text'...}]" with no error, and the whole text stack downstream
# is typed `prompt_tokens: List[int]` — image embeddings are not token ids and
# cannot cross that boundary. So vision hands `messages` to the mtmd handler,
# which builds its own prompt from the model's chat template.
#
# What that means, stated plainly so nobody looks for it later: the vision path
# has NO static prefix (SOUL.md), NO resident-seq cache, NO flow cache, and NO
# FSM extraction. Its response therefore reports no cache telemetry rather than
# reporting zeros that would read as cache misses.
# ══════════════════════════════════════════════════════════════════════


_VISION_BATCHED_SEM: Optional[Any] = None  # asyncio.Semaphore, lazy


def _vision_batched_semaphore(mcfg) -> Any:
    global _VISION_BATCHED_SEM
    if _VISION_BATCHED_SEM is None:
        import asyncio

        _VISION_BATCHED_SEM = asyncio.Semaphore(
            max(1, int(getattr(mcfg, "vision_batched_max_streams", 3) or 3))
        )
    return _VISION_BATCHED_SEM


async def _run_vision_batched(
    backend,
    mcfg,
    messages: list,
    max_tokens: Optional[int],
    temperature: Optional[float],
    reasoning: Optional[str],
    stops: list,
    resolved_max: int,
    resolved_temp: float,
):
    """The engine-resident vision path (model.vision_batched). Returns a
    VisionOutcome, or None to fall back to the dedicated pool path (the
    fallback is the CONTRACT: any install-side failure must cost this
    request a slower answer, never an error the pool path would not have
    produced). ImageIntakeError propagates — it is the shared 4xx shape.

    v1 scope: the primary model only, one user message (text + images),
    an optional system message. Anything else falls back."""
    import time as _time

    from fastapi.concurrency import run_in_threadpool

    from inference.vision_batched import (
        MtmdEncoder,
        VisionInstallError,
        install_multimodal_prefix,
        render_vision_prompt,
    )
    from inference.vision_images import resolve_image_part
    from inference.vision_text import clean as vision_clean

    engine = getattr(backend, "_engine", None)
    if engine is None:
        return None
    stats = getattr(backend, "_vision_batched_stats", None)
    if stats is None:
        stats = {"served": 0, "fallbacks": 0, "install_ms_last": 0.0, "active": 0}
        backend._vision_batched_stats = stats

    # ── shape check + intake (raw BYTES — the encoder wants buffers, not
    # data URIs). ImageIntakeError raises straight through.
    roots = list(getattr(mcfg, "vision_image_roots", []) or [])
    limit = int(getattr(mcfg, "vision_max_image_bytes", 33_554_432))
    system_text = ""
    user_text_parts: list = []
    images: list = []
    encoder = getattr(backend, "_vision_batched_encoder", None)
    if encoder is None:
        encoder = MtmdEncoder(
            mmproj_path=str(mcfg.mmproj_path),
            projector_device=getattr(mcfg, "vision_projector_device", None),
            use_gpu=bool(getattr(mcfg, "vision_projector_gpu", True)),
        )
        backend._vision_batched_encoder = encoder
    try:
        await run_in_threadpool(encoder.ensure, backend._primary_instance._model)
    except Exception:  # noqa: BLE001 — encoder init failure => pool path
        log.exception("batched vision: encoder init failed — pool fallback")
        stats["fallbacks"] += 1
        return None
    marker = encoder.marker

    user_seen = False
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system" and isinstance(content, str):
            system_text = content
            continue
        if role != "user":
            return None  # multi-turn / assistant history: pool path
        if user_seen:
            return None
        user_seen = True
        if isinstance(content, str):
            user_text_parts.append(content)
            continue
        for part in content or []:
            ptype = (part or {}).get("type")
            if ptype in ("image_url", "image_path"):
                images.append(resolve_image_part(part, roots, limit))
                user_text_parts.append(marker)
            elif ptype == "text":
                user_text_parts.append(str(part.get("text") or ""))
            else:
                return None
    if not images or not user_seen:
        return None

    sem = _vision_batched_semaphore(mcfg)
    async with sem:
        stats["active"] += 1
        instance = None
        t0 = _time.time()
        try:
            prompt_text = render_vision_prompt(
                mcfg.family,
                system_text,
                " ".join(user_text_parts),
                marker,
                len(images),
                reasoning=reasoning,
            )
            instance = await backend.acquire_instance(persona="vision")
            split = await run_in_threadpool(
                encoder.split_prompt,
                prompt_text,
                images,
                instance.n_tokens == 0,
            )
            try:
                n_embd_inp = backend._primary_instance._model.n_embd_inp()
                await install_multimodal_prefix(
                    engine,
                    encoder,
                    instance,
                    split,
                    n_embd_inp,
                    int(getattr(backend._primary_instance, "n_batch", 512)),
                )
                install_ms = (_time.time() - t0) * 1000.0
                stats["install_ms_last"] = round(install_ms, 1)
                prompt_tokens_total = instance.n_tokens + len(split.text2)
                answer = await backend.generate_async(
                    instance=instance,
                    prompt_tokens=list(split.text2),
                    max_tokens=resolved_max,
                    temperature=resolved_temp,
                    stop_texts=list(stops) if stops else None,
                    static_in_prompt=False,
                )
            finally:
                split.free()
            text = vision_clean(answer or "", mcfg.family)
            generated = len(getattr(instance, "_last_completion_tokens", None) or [])
            stats["served"] += 1
            return VisionOutcome(
                text=text,
                generated_tokens=generated,
                prompt_tokens=int(prompt_tokens_total),
                image_count=len(images),
                vision_model=mcfg.name,
                handler="batched-engine",
                decode_ms=round((_time.time() - t0) * 1000.0, 1),
            )
        except VisionInstallError as exc:
            log.warning("batched vision install failed (%s) — pool fallback", exc)
            stats["fallbacks"] += 1
            return None
        except Exception:  # noqa: BLE001 — never worse than the pool path
            log.exception("batched vision failed — pool fallback")
            stats["fallbacks"] += 1
            return None
        finally:
            stats["active"] -= 1
            if instance is not None:
                # A partial install leaves media KV on the seq; prepare_seat
                # through the engine strips it back to the bare vision head
                # before the seat returns to the pool.
                try:
                    import asyncio as _aio

                    await _aio.wrap_future(
                        engine.control(lambda: engine.prepare_seat(instance, "vision"))
                    )
                except Exception:  # noqa: BLE001
                    log.exception("batched vision: seat scrub failed")
                await backend.release_instance(instance)


@dataclass
class VisionOutcome:
    """Result of one vision completion. Deliberately narrower than
    CompletionOutcome — the fields it omits are ones the vision path cannot
    honestly populate."""

    text: str
    generated_tokens: int = 0
    prompt_tokens: int = 0
    image_count: int = 0
    vision_model: str = ""
    handler: str = ""
    decode_ms: float = 0.0


async def run_vision_completion(
    messages: list,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
) -> VisionOutcome:
    """Run one image+text completion through the mtmd handler.

    ``messages`` is the OpenAI shape with content PARTS; image parts may be a
    base64 data URI (``image_url``) or a local path (``image_path``) under a
    configured root — see inference/vision_images.resolve_image_part, which is
    also where the path allowlist is enforced.

    ``model`` routes to a HOT SECONDARY (Phase 2b). Vision is the path where
    that works with almost no surgery, and the reason is structural: unlike
    ``run_completion`` it never touches ``static_tokens_manager`` or
    ``get_cached_tokenizer`` — the mtmd handler builds the prompt from the
    model's OWN chat template. The text path resolves its tokenizer and static
    buffer through the ACTIVE config, so routing text to a secondary would
    tokenize with the primary's tokenizer and prepend the primary's static
    head: silently wrong tokens, no error. De-globalizing that is the Phase 2
    "de-globalize the request path" work and is NOT done here.
    """
    import time as _time
    from pathlib import Path

    from fastapi.concurrency import run_in_threadpool

    from inference.vision_images import (
        ImageIntakeError,
        resolve_image_part,
        to_data_uri,
    )
    from inference.vision_text import clean as vision_clean
    from inference.vision_text import stop_strings as vision_stop_strings

    # Resolve the SERVING backend first, then read its own config — not the
    # global ActiveConfigView, which is the primary's. Cheap for a resident
    # secondary (a dict lookup); the expensive lazy vision-context build is
    # still below every intake check, so bad input fails before Metal moves.
    backend = await _get_backend(model)
    serving = getattr(backend, "config", None) or config
    mcfg = serving.model
    if not getattr(mcfg, "mmproj_path", None):
        raise RuntimeError(
            f"vision is not configured for {mcfg.name!r}: set model.mmproj_path"
        )

    roots = list(getattr(mcfg, "vision_image_roots", []) or [])
    limit = int(getattr(mcfg, "vision_max_image_bytes", 33_554_432))

    # Normalise every image part to a data URI up front, so intake errors
    # surface as a clean 4xx-shaped failure BEFORE the model is touched.
    prepared: list = []
    image_count = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            prepared.append(msg)
            continue
        parts = []
        for part in content:
            ptype = (part or {}).get("type")
            if ptype in ("image_url", "image_path"):
                data = resolve_image_part(part, roots, limit)  # may raise
                parts.append(
                    {"type": "image_url", "image_url": {"url": to_data_uri(data)}}
                )
                image_count += 1
            else:
                parts.append(part)
        prepared.append({**msg, "content": parts})

    if image_count == 0:
        raise ImageIntakeError(
            "no image parts in the request — use the text endpoint for text-only"
        )

    # ── THE REASONING DIAL, VIA THE SYSTEM BLOCK ────────────────────────
    # The mtmd handler builds its prompt from the MODEL'S OWN chat template,
    # so the renderer's generation-prompt gate never runs here and a caller
    # had no way to ask for a depth at all — the vision path served whatever
    # the family's resting default was, silently. Every family that has a
    # dial already declares it as SYSTEM-BLOCK text (harmony's
    # `Reasoning strength: low`, qwen38's effort sentence via
    # reasoning_prefix), and the template does accept a system message, so
    # the level is delivered the way the family itself spells it.
    #
    # Segments [0] and [-1] are the ROLE WRAPPER, which the template will
    # supply itself; everything between is the system body. Slicing that way
    # rather than filtering on is_framing is deliberate — qwen38's effort
    # sentence is marked framing (it comes from reasoning_prefix), so a
    # content-only filter drops exactly the directive being requested.
    #
    # A family with no dial renders an empty body and is left alone.
    if reasoning:
        try:
            _r = _get_format_renderer(mcfg.family)
            # RENDER THE SYSTEM BLOCK THE WAY THE TEXT PATH DOES — persona and
            # tools included (preprocessing/builder.py). A stripped-down block
            # is not a smaller version of the same prompt, it is a DIFFERENT
            # prompt: measured 2026-08-23, qwen3.8 given only
            # "Reasoning effort is set to low…" plus a bare identity emitted a
            # stop token as its FIRST token (finish_reason='stop', raw_len=0)
            # on every budget from 300 to 4096, while the same model at high —
            # and the same model on the text path, where the persona is
            # present — answered normally.
            _persona_text = ""
            _pf = getattr(serving.prompt, "persona_file", "") or ""
            if _pf:
                _pp = Path(_pf).expanduser().resolve()
                if _pp.is_file():
                    _persona_text = _pp.read_text(encoding="utf-8")
            _tools_text = ""
            _tf = getattr(serving.prompt, "tools_file", "") or ""
            if _tf:
                _tp = Path(_tf).expanduser().resolve()
                if _tp.is_file():
                    _tools_text = _tp.read_text(encoding="utf-8")
            _segs = _r.render_system_segments(
                persona=_persona_text, reasoning=reasoning, tools=_tools_text
            )
            if len(_segs) > 2:
                _body = "".join(t for t, _fr in _segs[1:-1]).strip()
                if _body and not any(m.get("role") == "system" for m in prepared):
                    prepared = [{"role": "system", "content": _body}] + prepared
                    log.info(
                        "👁  vision reasoning=%s -> system block (%d chars)",
                        reasoning,
                        len(_body),
                    )
        except Exception:  # noqa: BLE001 — a dial failure must not kill the request
            log.debug("vision reasoning injection failed", exc_info=True)

    if not hasattr(backend, "acquire_vision_instance"):
        raise RuntimeError("the active backend does not support vision")

    # Budget defaults follow the SERVING model too — resolve_* read the active
    # config, which is the wrong model's generation block for a secondary.
    if max_tokens is None and serving is not config:
        resolved_max = int(serving.generation.max_tokens_default)
    else:
        resolved_max = resolve_max_tokens(max_tokens)
    if temperature is None and serving is not config:
        resolved_temp = float(serving.generation.temperature_default)
    else:
        resolved_temp = resolve_temperature(temperature)

    # SEAL THE TURN WITH THE FAMILY'S OWN TERMINATOR. The mtmd handler builds
    # its prompt from the model's chat template and never consults the FSM, so
    # without this a family that reopens the assistant role between messages
    # simply re-answers — measured on muse, which emitted
    # `<|start|>assistant to=user<|message|>` mid-answer and started again.
    # Honouring the terminator the model itself emits is what the text path
    # has always done; it is not a cap and costs nothing when the model stops
    # correctly on its own.
    stops = vision_stop_strings(mcfg.family)

    # ── BATCHED VISION (model.vision_batched) ───────────────────────────
    # The engine-resident path: encode once, decode as an ordinary stream
    # in the shared batched context. Primary model only (the engine is the
    # primary's); every failure inside falls back to the pool path below,
    # so a broken install costs latency, never a new error shape.
    if bool(getattr(mcfg, "vision_batched", False)) and not model:
        outcome = await _run_vision_batched(
            backend,
            mcfg,
            messages,
            max_tokens,
            temperature,
            reasoning,
            stops,
            resolved_max,
            resolved_temp,
        )
        if outcome is not None:
            return outcome

    # TWO DIFFERENT GUARDS, doing two different jobs — the comment here used
    # to say this "serializes with text generation", which is wrong and was
    # worth catching: generation_guard is a COUNTING guard. It holds off drains,
    # JIT scaling and swap teardown while GPU work is live, and deliberately
    # lets generations run CONCURRENTLY. It never excluded anything from this
    # call. The exclusion that matters — one owner per vision context, so
    # concurrent requests cannot race on the handler's token ledger — comes
    # from the checkout below.
    t0 = _time.time()
    async with backend.acquire_vision_instance() as inst:
        # Read while we still OWN the instance. Once the checkout closes the
        # context belongs to the next request, and reaching back into it for
        # telemetry is how a harmless-looking read becomes a race later.
        handler = type(getattr(inst, "chat_handler", None)).__name__
        async with backend.generation_guard():
            result = await run_in_threadpool(
                lambda: inst.create_chat_completion(
                    messages=prepared,
                    max_tokens=resolved_max,
                    temperature=resolved_temp,
                    **({"stop": stops} if stops else {}),
                )
            )
    decode_ms = (_time.time() - t0) * 1000.0

    choice = (result.get("choices") or [{}])[0]
    msg_out = choice.get("message") or {}
    # Then repair what still arrives: a stop string fires on an exact match,
    # and a family with two closers has more than one way to leak.
    text = vision_clean(msg_out.get("content") or "", mcfg.family)
    usage = result.get("usage") or {}
    generated = int(usage.get("completion_tokens") or 0)

    # A zero-token vision response is a BUDGET symptom on some models, not a
    # broken model: qwen3.6-35b-a3 returns nothing at max_tokens 1400 and
    # answers fine at 300 (measured 2026-08-12, dev/VL_BAKEOFF_2026-08-11.md).
    # Say so, because "" with no explanation sent the last diagnosis of this
    # exact symptom down a two-day wrong path.
    if generated == 0 and not text:
        log.warning(
            "vision returned ZERO tokens at max_tokens=%d — some models fail at "
            "large budgets and answer fine at smaller ones; try lowering it "
            "before suspecting the model or the projector",
            resolved_max,
        )

    return VisionOutcome(
        text=text,
        generated_tokens=generated,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        image_count=image_count,
        vision_model=mcfg.name,
        handler=handler,
        decode_ms=round(decode_ms, 1),
    )
