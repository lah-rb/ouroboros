#!/usr/bin/env python3
"""
Shared Inference Module

Core LLM inference logic shared between REST and GraphQL APIs.
This module provides a single source of truth for all LLM operations.

Now uses the pluggable backend system for backend-agnostic inference.
"""

import logging
import re
from typing import AsyncGenerator, List, Optional, Tuple

# Local imports
from core.config import get_config
from inference.backends.factory import get_backend, initialize_backend_async
from preprocessing.static_tokens import manager as static_tokens_manager
from inference.tokenizer import (
    get_cached_tokenizer,
    tokenize_segments,
    build_full_prompt,
)
from core.interaction_logger import log_interaction
from formats.registry import get_renderer as _get_format_renderer

# Set up logging
config = get_config()
log = logging.getLogger("llm-mvp")


def _get_delimiter() -> str:
    """Get the delimiter pattern from the format schema."""
    return _get_format_renderer(config.model.family).delimiter_pattern()


def _render_dynamic_prompt(user_prompt: str) -> str:
    """Render the dynamic portion of a prompt (user turn + generation prompt)."""
    renderer = _get_format_renderer(config.model.family)
    return renderer.render_user(user_prompt) + renderer.render_generation_prompt()


def _compile_delimiter_pattern(delim: str) -> "re.Pattern | None":
    """Compile a delimiter string into a regex pattern.

    Supports glob-style ``*`` wildcards so that a delimiter like
    ``<|start|>assistant<|channel|>*<|message|>`` matches regardless
    of what the model inserts between the fixed parts (e.g.
    ``<|channel|>final <|constrain|>json<|message|>``).

    Returns ``None`` if *delim* is empty.  The compiled pattern is
    suitable for ``re.search()`` / ``re.finditer()`` against model
    output.

    IMPORTANT: When the output contains multiple channel transitions,
    callers should use the **last** match (``list(pattern.finditer(text))[-1]``)
    to find the final channel, not ``pattern.search()`` which returns
    the first.
    """
    import re

    if not delim:
        return None
    # Escape everything except our glob wildcard
    parts = delim.split("*")
    regex = ".*?".join(re.escape(p) for p in parts)
    return re.compile(regex)


def _find_delimiter(pattern: "re.Pattern", text: str) -> "re.Match | None":
    """Find the last occurrence of a delimiter pattern in text.

    Uses the last match because models with multiple channels (e.g.
    analysis → final) emit several ``<|start|>assistant<|channel|>``
    transitions, and the response content follows the *last* one.
    """
    matches = list(pattern.finditer(text))
    return matches[-1] if matches else None


# Note: _compile_delimiter_pattern and _find_delimiter above are still
# used by session_manager.session_turn() for streaming delimiter detection.
# They are NOT used by _strip_delimiter, which uses the FSM labeller.

# Module-level FSM family cache — determined once from config at first use.
_fsm_family: "str | None" = None


def _get_fsm_family() -> str:
    """Return the model family name to drive FSM phase transitions.

    Reads from ``config.model.family`` once on first call. The family
    determines the FSM's initial phase (Harmony/ChatML start in DELIM
    and transition on channel markers; Tekken/Mistral start directly in
    CONTENT since their generation stream has no thinking markers).
    """
    global _fsm_family
    if _fsm_family is None:
        _fsm_family = config.model.family
    return _fsm_family


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
    # Gemma-4 emits a channel reasoning preamble (<|channel>thought ... <channel|>)
    # that the atom-based FSM does not model. The real content reliably follows
    # the last <channel|>, so split on it directly. (Full Gemma-4 channel/tool
    # support is a follow-up; this just strips the preamble from extracted text.)
    if _get_fsm_family() == "gemma":
        marker = "<channel|>"
        if marker in text:
            return text.rsplit(marker, 1)[-1].strip()
        return text.strip()

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


async def _get_backend():
    """
    Get or initialize the backend instance.

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
    backend = get_backend()
    if backend is None:
        log.warning(
            "⚠️ Backend not yet initialized — triggering async "
            "initialization from inference layer (should only happen "
            "in tests or unusual startup sequences)"
        )
        backend = await initialize_backend_async(config)
    return backend


async def run_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Run a non-streaming completion.

    Args:
        prompt: User prompt text
        max_tokens: Maximum tokens to generate (uses config default if None)
        temperature: Sampling temperature (uses config default if None)

    Returns:
        Tuple of (generated_text, approximate_token_count)

    Raises:
        RuntimeError: If static buffer not loaded
        ValueError: If prompt is empty or exceeds context window
    """
    if not prompt:
        raise ValueError("`prompt` must be a non-empty string")

    max_tokens = max_tokens or config.generation.max_tokens_default or 256
    temperature = temperature or config.generation.temperature_default or 0.7
    # Global per-model temperature floor — a refusal to sample below the
    # configured value for ANY request kind (see GenerationConfig).
    from core.session_manager import _global_temperature_floor

    _floored = _global_temperature_floor(temperature, config.generation)
    if _floored != temperature:
        log.info(
            "🌡️ Completion: global temperature floor %.2f -> %.2f",
            temperature,
            _floored,
        )
        temperature = _floored

    static_tokens = static_tokens_manager.get_static_tokens()

    # Build complete prompt BEFORE acquiring instance to minimize pool hold time
    tokenizer = get_cached_tokenizer()
    dynamic_ids = build_full_prompt(prompt, tokenizer)
    total_len = len(static_tokens) + len(dynamic_ids)

    if total_len > config.model.n_ctx:
        raise ValueError(
            f"Combined prompt length ({total_len}) exceeds the model's "
            f"context window of {config.model.n_ctx} tokens."
        )

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

        # Use backend's async generation
        answer = await backend.generate_async(
            instance=instance or backend,
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

        # Approximate token count
        tokens_generated = _approximate_token_count(answer)

        # Log interaction (non-streaming)
        log_interaction(prompt=prompt, response=answer, mode="non-stream")
        return answer, tokens_generated

    finally:
        if backend.capabilities.manual_pooling and instance is not None:
            await backend.release_instance(instance)


async def run_raw_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Run a non-streaming completion that returns raw model output.

    Identical to run_completion but SKIPS delimiter stripping.
    Returns the full model output including channel markers, thinking
    text, and delimiter tokens. Used for training data collection.

    Args:
        prompt: User prompt text
        max_tokens: Maximum tokens to generate
        temperature: Sampling temperature

    Returns:
        Tuple of (raw_text, approximate_token_count)
    """
    if not prompt:
        raise ValueError("`prompt` must be a non-empty string")

    max_tokens = max_tokens or config.generation.max_tokens_default or 256
    temperature = temperature or config.generation.temperature_default or 0.7
    # Global per-model temperature floor — a refusal to sample below the
    # configured value for ANY request kind (see GenerationConfig).
    from core.session_manager import _global_temperature_floor

    _floored = _global_temperature_floor(temperature, config.generation)
    if _floored != temperature:
        log.info(
            "🌡️ Completion: global temperature floor %.2f -> %.2f",
            temperature,
            _floored,
        )
        temperature = _floored

    static_tokens = static_tokens_manager.get_static_tokens()

    tokenizer = get_cached_tokenizer()
    dynamic_ids = build_full_prompt(prompt, tokenizer)
    total_len = len(static_tokens) + len(dynamic_ids)

    if total_len > config.model.n_ctx:
        raise ValueError(
            f"Combined prompt length ({total_len}) exceeds the model's "
            f"context window of {config.model.n_ctx} tokens."
        )

    full_prompt = list(static_tokens) + dynamic_ids

    backend = await _get_backend()
    instance = None

    if backend.capabilities.manual_pooling:
        instance = await backend.acquire_instance()

    try:
        gen_kwargs = {}
        if grammar:
            gen_kwargs["grammar"] = grammar

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

    max_tokens = max_tokens or config.generation.max_tokens_default or 256
    temperature = temperature or config.generation.temperature_default or 0.7
    # Global per-model temperature floor — a refusal to sample below the
    # configured value for ANY request kind (see GenerationConfig).
    from core.session_manager import _global_temperature_floor

    _floored = _global_temperature_floor(temperature, config.generation)
    if _floored != temperature:
        log.info(
            "🌡️ Completion: global temperature floor %.2f -> %.2f",
            temperature,
            _floored,
        )
        temperature = _floored

    static_tokens = static_tokens_manager.get_static_tokens()

    # Build complete prompt BEFORE acquiring instance to minimize pool hold time
    tokenizer = get_cached_tokenizer()
    dynamic_ids = build_full_prompt(prompt, tokenizer)
    total_len = len(static_tokens) + len(dynamic_ids)

    if total_len > config.model.n_ctx:
        raise ValueError(
            f"Combined prompt length ({total_len}) exceeds the model's "
            f"context window of {config.model.n_ctx} tokens."
        )

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

        # Signal completion
        if captured_chunks:
            log_interaction(
                prompt=prompt,
                response="".join(captured_chunks),
                mode="stream",
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


async def run_tool_completion(
    prompt: str,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    grammar: Optional[str] = None,
) -> Tuple[str, int]:
    """
    Run a completion with tool-call support.

    If the model emits ``<tool_call>...</tool_call>`` in its response the
    server executes the tool, injects the result, and re-prompts the model.
    Loops up to ``config.tools.max_iterations`` times.

    Falls back to plain ``run_completion()`` when tools are disabled.
    """
    from tools.protocol import parse_tool_call, has_tool_call, format_tool_result
    from tools.registry import get_registry

    tools_cfg = config.tools
    if not tools_cfg.enabled:
        return await run_completion(prompt, max_tokens, temperature)

    max_tokens = max_tokens or config.generation.max_tokens_default or 256
    temperature = temperature or config.generation.temperature_default or 0.7
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

        if len(full_prompt) > config.model.n_ctx:
            raise ValueError(
                f"Combined prompt length ({len(full_prompt)}) exceeds "
                f"context window ({config.model.n_ctx})."
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
            return answer, total_tokens

        tc = parse_tool_call(answer)
        if tc is None:
            # Malformed tool call — return as-is
            log_interaction(prompt=prompt, response=answer, mode="tool")
            return answer, total_tokens

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
    return answer, total_tokens


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

    max_tokens = max_tokens or config.generation.max_tokens_default or 256
    temperature = temperature or config.generation.temperature_default or 0.7
    registry = get_registry()

    # Conversation turns for multi-round tool use
    messages: list = [{"role": "user", "content": prompt}]

    for iteration in range(tools_cfg.max_iterations):
        # Build token sequence
        static_tokens = static_tokens_manager.get_static_tokens()
        tokenizer = get_cached_tokenizer()
        dynamic_ids = _build_messages_tokens(messages, tokenizer)
        full_prompt = list(static_tokens) + dynamic_ids

        if len(full_prompt) > config.model.n_ctx:
            raise ValueError(
                f"Combined prompt length ({len(full_prompt)}) exceeds "
                f"context window ({config.model.n_ctx})."
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
            "pool_size": config.resources.max_concurrent_requests,
            "available_instances": 0,
        }

    return backend.get_health_status()
