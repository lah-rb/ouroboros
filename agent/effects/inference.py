"""Inference via LLMVP GraphQL API.

Pure HTTP client that constructs GraphQL queries, sends them to the LLMVP
endpoint via httpx.AsyncClient, and parses responses. Ouroboros does NOT
import anything from LLMVP.

Also handles relative temperature resolution: "t*0.5" style values are
parsed and resolved against a configured model default temperature.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from agent.effects.protocol import InferenceResult

logger = logging.getLogger(__name__)

# GraphQL query for non-streaming completion
COMPLETION_QUERY = """
query Completion($request: CompletionRequest!) {
    completion(request: $request) {
        text
        tokensGenerated
        finished
        truncated
        promptTokens
        cachedPrefixTokens
        freshPrefillTokens
        generatedTokens
        cacheHit
        flowKey
        prefillMs
        decodeMs
    }
}
"""

# GraphQL query for health check (includes generation progress + diagnostics)
HEALTH_QUERY = """
query Health {
    health {
        status
        poolSize
        availableInstances
        generationActive
        tokensGenerated
        elapsedSeconds
        secondsSinceLastToken
        generationPhase
        promptTokens
        evalDuration
    }
}
"""

# Session mutations and queries
START_SESSION_MUTATION = """
mutation StartSession($config: SessionConfig!) {
    startSession(config: $config) {
        sessionId
        instanceIndex
        ttlSeconds
    }
}
"""

END_SESSION_MUTATION = """
mutation EndSession($sessionId: String!) {
    endSession(sessionId: $sessionId)
}
"""

SESSION_SNAPSHOT_MUTATION = """
mutation SessionSnapshot($sessionId: String!, $key: String!) {
    sessionSnapshot(sessionId: $sessionId, key: $key) {
        key
        tokens
        resident
        turnCount
    }
}
"""

PURGE_SNAPSHOT_MUTATION = """
mutation PurgeSnapshot($key: String!) {
    purgeSnapshot(key: $key)
}
"""

SESSION_COMPLETION_QUERY = """
query SessionCompletion($request: SessionTurnRequest!) {
    sessionCompletion(request: $request) {
        text
        tokensGenerated
        finished
        truncated
        promptTokens
        cachedPrefixTokens
        freshPrefillTokens
        generatedTokens
        cacheHit
        flowKey
        prefillMs
        decodeMs
    }
}
"""

# GraphQL query for chain-of-thought content from thinking models
THINKING_QUERY = """
query Thinking($requestId: String) {
    thinking(requestId: $requestId) {
        requestId
        content
        complete
        active
    }
}
"""


class InferenceError(Exception):
    """Raised when an inference call fails."""

    pass


def resolve_temperature(
    value: Any,
    model_default: float = 0.7,
) -> float | None:
    """Resolve a temperature value, supporting relative notation.

    Args:
        value: Temperature value — either a float, a string like "t*0.5",
               or None.
        model_default: The model's default temperature (used as 't' in
                       relative expressions).

    Returns:
        Resolved float temperature, or None if value is None.

    Examples:
        >>> resolve_temperature(0.1)
        0.1
        >>> resolve_temperature("t*0.5", model_default=0.7)
        0.35
        >>> resolve_temperature("t*1.2", model_default=0.7)
        0.84
        >>> resolve_temperature(None)
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        # Match "t*{float}" pattern
        match = re.match(r"^t\*(\d+\.?\d*)$", value.strip())
        if match:
            multiplier = float(match.group(1))
            resolved = model_default * multiplier
            logger.debug(
                "Resolved relative temperature: t*%s = %s * %s = %s",
                multiplier,
                model_default,
                multiplier,
                resolved,
            )
            return resolved
        # Try parsing as a plain float string
        try:
            return float(value)
        except ValueError:
            raise InferenceError(
                f"Invalid temperature value: {value!r}. "
                f"Expected a float or 't*{{multiplier}}' format."
            )

    raise InferenceError(f"Invalid temperature type: {type(value).__name__}")


# Hard ceilings on tokens a single generation may produce before the health
# watchdog cancels it as a runaway. They bound the Qwen3-Next repetition bug
# (unclamped Gated-DeltaNet decay) which otherwise generates to max_tokens
# (262k) — observed as both a ~79-min session hang AND a ~20-min completion
# (whole-file rewrite) runaway. The stall watchdog can't catch these: a
# repetition loop keeps tokens *advancing*, so only a token ceiling stops it.
#
# Two tiers because legitimate output sizes differ:
#   - session turns (diagnosis menus ~64 tok, conclude ~1-2k, AST symbol edits
#     a few k) never approach 32k.
#   - completions (whole-file rewrites, multi-file scaffolds) legitimately reach
#     ~10-24k, so the ceiling sits higher with headroom — still far below 262k.
SESSION_RUNAWAY_TOKEN_CEILING = 32768
COMPLETION_RUNAWAY_TOKEN_CEILING = 49152
# Guard G1 — last-resort prompt-size backstop (~120k tokens). The per-source
# guards (scan skeleton G2, terminal/session bounds G3/G4) should keep every
# prompt far under this; if one slips through, the dynamic prompt is bounded and
# a LOUD warning is logged rather than crashing the server (the llama_decode
# code -1 / 29M-token overflow). Conservative so it protects the smallest context
# window (gpt-oss 131k); legitimate prompts never approach it after the guards.
PROMPT_CHAR_CEILING = 480_000


class InferenceEffect:
    """GraphQL client for LLMVP inference.

    Constructs GraphQL queries, sends HTTP requests to the LLMVP endpoint,
    and parses responses into InferenceResult.

    Args:
        endpoint: The LLMVP GraphQL endpoint URL.
        model_default_temperature: Default temperature for the model
            (used to resolve relative temperature values like "t*0.5").
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8008/graphql",
        model_default_temperature: float = 0.7,
    ) -> None:
        self._endpoint = endpoint
        self._model_default_temperature = model_default_temperature
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client.

        No fixed timeout — the health polling watchdog handles
        stall detection. This prevents killing productive long
        generations (e.g., 25K token multi-file output).
        """
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> dict:
        """Check LLMVP backend health.

        Returns:
            Health status dict with 'status', 'poolSize', 'availableInstances'.

        Raises:
            InferenceError: If the health check fails.
        """
        client = await self._get_client()
        try:
            response = await client.post(
                self._endpoint,
                json={"query": HEALTH_QUERY},
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                raise InferenceError(f"GraphQL health check errors: {data['errors']}")

            return data["data"]["health"]

        except httpx.ConnectError as e:
            raise InferenceError(
                f"Cannot connect to LLMVP at {self._endpoint}: {e}"
            ) from e
        except httpx.HTTPStatusError as e:
            raise InferenceError(f"LLMVP health check HTTP error: {e}") from e

    async def fetch_thinking(self, request_id: str = "") -> str:
        """Fetch chain-of-thought content from the last inference call.

        Returns the thinking/CoT content captured by the LLMVP generation
        tracker. Available when the model uses a thinking delimiter.

        Args:
            request_id: Optional correlation ID. If empty, returns thinking
                        from the most recent generation.

        Returns:
            The thinking content string, or empty string if unavailable.
        """
        client = await self._get_client()
        try:
            variables = {}
            if request_id:
                variables["requestId"] = request_id

            response = await client.post(
                self._endpoint,
                json={"query": THINKING_QUERY, "variables": variables},
                timeout=5.0,
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.debug("Thinking query errors: %s", data["errors"])
                return ""

            thinking = data.get("data", {}).get("thinking", {})
            content = thinking.get("content", "")
            return content

        except Exception as e:
            logger.debug("Failed to fetch thinking: %s", e)
            return ""

    @staticmethod
    def _guard_prompt_size(prompt: str, static_prefix: str | None) -> str:
        """Last-resort prompt-size backstop (Guard G1). The per-source guards keep
        prompts well under the context window; an over-ceiling prompt HERE means an
        upstream guard missed — so bound the dynamic tail (keep the cacheable static
        prefix intact) and LOG LOUDLY. Never a silent truncation: visibility is the
        point, because the model would otherwise misread a quietly-cut prompt."""
        budget = PROMPT_CHAR_CEILING - len(static_prefix or "")
        if budget <= 0 or len(prompt) <= budget:
            return prompt
        logger.warning(
            "PROMPT-SIZE BACKSTOP fired: static=%d + dynamic=%d chars exceeds the "
            "%d ceiling — an upstream context guard (G2 scan / G3 terminal / G4 "
            "session) missed this. Bounding the dynamic tail; investigate the source.",
            len(static_prefix or ""),
            len(prompt),
            PROMPT_CHAR_CEILING,
        )
        head = int(budget * 0.6)
        tail = budget - head
        omitted = len(prompt) - head - tail
        return (
            prompt[:head]
            + f"\n\n… [BACKSTOP: {omitted} chars bounded — an upstream guard missed "
            "this; re-query a narrower slice] …\n\n" + prompt[-tail:]
        )

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
    ) -> InferenceResult:
        """Send a completion request to LLMVP.

        Uses a health-polling watchdog for long-running generations:
        the HTTP request uses a moderate initial timeout, and if the
        generation is still actively producing tokens (verified via
        the health endpoint), the request is retried with an extended
        timeout. This avoids killing productive long generations while
        still catching stuck instances.

        Args:
            prompt: The prompt text.
            config_overrides: Optional dict with 'temperature', 'max_tokens', etc.

        Returns:
            InferenceResult with the model's response.
        """
        client = await self._get_client()

        # Last-resort prompt-size backstop (Guard G1) — keep the static prefix
        # intact, bound only the dynamic tail if an upstream guard missed.
        prompt = self._guard_prompt_size(prompt, static_prefix)

        # Build the request variables
        request_vars: dict[str, Any] = {"prompt": prompt}

        # Per-flow KV cache (opt-in server-side). Sent only when both are
        # present; the server ignores them unless flow_kv_cache is enabled.
        if static_prefix and flow_key:
            request_vars["staticPrefix"] = static_prefix
            request_vars["flowCacheKey"] = flow_key

        if config_overrides:
            if "temperature" in config_overrides:
                temp = resolve_temperature(
                    config_overrides["temperature"],
                    self._model_default_temperature,
                )
                if temp is not None:
                    request_vars["temperature"] = temp

            if "max_tokens" in config_overrides:
                mt = config_overrides["max_tokens"]
                if mt is not None:
                    request_vars["maxTokens"] = int(mt)

            if "grammar" in config_overrides:
                grammar = config_overrides["grammar"]
                if grammar is not None:
                    request_vars["grammar"] = grammar
            # Reasoning HEAD-SWAP level for stateless completions — the server
            # installs the level's pinned head for this request only (resident
            # path, config.model.reasoning_head_swap). None/absent → default.
            if config_overrides.get("reasoning"):
                request_vars["reasoning"] = str(config_overrides["reasoning"])

        request_body = {
            "query": COMPLETION_QUERY,
            "variables": {"request": request_vars},
        }

        # Use the watchdog-backed request for non-session inference. The
        # completion ceiling bounds a runaway whole-file/scaffold generation
        # (the stall watchdog misses it — a repetition loop keeps emitting).
        return await self._request_with_health_watchdog(
            client,
            request_body,
            runaway_token_ceiling=COMPLETION_RUNAWAY_TOKEN_CEILING,
        )

    async def _request_with_health_watchdog(
        self,
        client: httpx.AsyncClient,
        request_body: dict,
        response_key: str = "completion",
        runaway_token_ceiling: int | None = None,
    ) -> InferenceResult:
        """Execute an inference request with health-polling watchdog.

        Strategy:
        1. Fire the request with no fixed timeout (httpx timeout=None).
        2. Concurrently run a watchdog that polls LLMVP health every 30s.
        3. If health shows tokens stalled for 60s+ (two consecutive polls
           with no token increase), cancel the request — the model is stuck.
        4. If a ``runaway_token_ceiling`` is set and the live token count
           crosses it, cancel — the model is looping/runaway even though it's
           still "productively" emitting tokens (the stall check alone misses
           this; it's how the Qwen3-Next decay-clamp repetition hangs).
        5. If health shows tokens increasing under the ceiling, the watchdog
           stays quiet and lets the request complete naturally.

        ``response_key`` selects the GraphQL payload field — "completion" for
        normal completions, "sessionCompletion" for memoryful session turns.

        This means productive long generations (large files) are never
        killed prematurely, but stuck *and* runaway generations are caught.
        """

        async def _do_request() -> InferenceResult:
            """The actual HTTP request."""
            try:
                response = await client.post(self._endpoint, json=request_body)
                response.raise_for_status()
                data = response.json()

                if "errors" in data:
                    error_msg = "; ".join(
                        e.get("message", str(e)) for e in data["errors"]
                    )
                    logger.error("GraphQL inference errors: %s", error_msg)
                    return InferenceResult(
                        text="",
                        tokens_generated=0,
                        finished=False,
                        error=f"GraphQL errors: {error_msg}",
                    )

                completion = data["data"][response_key]
                return InferenceResult(
                    text=completion["text"],
                    tokens_generated=completion["tokensGenerated"],
                    finished=completion["finished"],
                    truncated=completion.get("truncated", False),
                    # Cache-aware counts — .get with defaults so a server that
                    # doesn't yet return these degrades to whitespace fallback.
                    prompt_tokens=completion.get("promptTokens", 0) or 0,
                    cached_prefix_tokens=completion.get("cachedPrefixTokens", 0) or 0,
                    fresh_prefill_tokens=completion.get("freshPrefillTokens", 0) or 0,
                    generated_tokens=completion.get("generatedTokens", 0) or 0,
                    cache_hit=bool(completion.get("cacheHit", False)),
                    flow_key=completion.get("flowKey", "") or "",
                    prefill_ms=completion.get("prefillMs", 0.0) or 0.0,
                    decode_ms=completion.get("decodeMs", 0.0) or 0.0,
                )

            except httpx.ConnectError as e:
                logger.error("Cannot connect to LLMVP at %s: %s", self._endpoint, e)
                return InferenceResult(
                    text="",
                    tokens_generated=0,
                    finished=False,
                    error=f"Connection error: {e}",
                )
            except httpx.TimeoutException as e:
                logger.error("LLMVP inference timed out: %s", e)
                return InferenceResult(
                    text="",
                    tokens_generated=0,
                    finished=False,
                    error=f"Timeout: {e}",
                )
            except httpx.HTTPStatusError as e:
                logger.error("LLMVP HTTP error: %s", e)
                return InferenceResult(
                    text="",
                    tokens_generated=0,
                    finished=False,
                    error=f"HTTP error: {e}",
                )

        async def _health_watchdog(request_task: asyncio.Task) -> None:
            """Monitor generation health and cancel if stalled.

            Starts polling after an initial grace period (60s).
            Cancels the request if tokens stop advancing for 60s.
            """
            import asyncio as _asyncio

            grace_period = 60  # Don't poll during the first 60s
            poll_interval = 30  # Check health every 30s
            stall_threshold = 60  # Cancel after 60s of no new tokens

            await _asyncio.sleep(grace_period)

            last_token_count = -1

            while not request_task.done():
                try:
                    # Use a short-timeout client for health checks
                    health_client = httpx.AsyncClient(timeout=10.0)
                    try:
                        resp = await health_client.post(
                            self._endpoint, json={"query": HEALTH_QUERY}
                        )
                        health = resp.json().get("data", {}).get("health", {})
                    finally:
                        await health_client.aclose()

                    gen_active = health.get("generationActive", False)
                    tokens = health.get("tokensGenerated", 0)
                    stall_secs = health.get("secondsSinceLastToken")
                    phase = health.get("generationPhase", "unknown")
                    prompt_toks = health.get("promptTokens", 0)
                    elapsed = health.get("elapsedSeconds", 0)
                    eval_dur = health.get("evalDuration")

                    if gen_active:
                        if runaway_token_ceiling and tokens > runaway_token_ceiling:
                            # Tokens still advancing, but past the sane ceiling
                            # for this request type — a runaway/repetition loop
                            # (stall detection alone never fires on these).
                            logger.warning(
                                "Health watchdog: runaway generation — %d tokens "
                                "exceeds ceiling %d (phase=%s) — cancelling request",
                                tokens,
                                runaway_token_ceiling,
                                phase,
                            )
                            request_task.cancel()
                            return
                        if tokens > last_token_count:
                            # Model is actively generating — reset stall tracking
                            last_token_count = tokens
                            logger.info(
                                "Health watchdog: phase=%s, %d tokens generated, "
                                "%.0fs elapsed, prompt=%d tok",
                                phase,
                                tokens,
                                elapsed or 0,
                                prompt_toks,
                            )
                        elif phase == "eval":
                            # Still evaluating prompt — log but don't cancel yet
                            logger.info(
                                "Health watchdog: still in eval phase, "
                                "%.0fs elapsed, prompt=%d tok, eval_dur=%s",
                                elapsed or 0,
                                prompt_toks,
                                eval_dur,
                            )
                            # Cancel if eval takes unreasonably long (>300s)
                            if elapsed and elapsed > 300:
                                logger.warning(
                                    "Health watchdog: eval phase stuck for %.0fs "
                                    "— cancelling request",
                                    elapsed,
                                )
                                request_task.cancel()
                                return
                        elif stall_secs is not None and stall_secs > stall_threshold:
                            # Tokens haven't advanced and LLMVP confirms stall
                            logger.warning(
                                "Health watchdog: generation stalled for %.0fs "
                                "at %d tokens (phase=%s) — cancelling request",
                                stall_secs,
                                tokens,
                                phase,
                            )
                            request_task.cancel()
                            return
                        elif (
                            tokens == 0
                            and stall_secs is not None
                            and stall_secs > stall_threshold
                        ):
                            # 0 tokens generated and stalled — model never started
                            logger.warning(
                                "Health watchdog: 0 tokens after %.0fs "
                                "(phase=%s) — cancelling request",
                                elapsed or 0,
                                phase,
                            )
                            request_task.cancel()
                            return
                    elif not gen_active and last_token_count > 0:
                        # Generation ended — request should complete soon
                        logger.info("Health watchdog: generation finished")
                        return

                except Exception as e:
                    # Health check failed — don't kill the request over a health check error
                    logger.debug("Health watchdog poll failed: %s", e)

                await _asyncio.sleep(poll_interval)

        import asyncio

        # Run the request with the watchdog
        request_task = asyncio.create_task(_do_request())
        watchdog_task = asyncio.create_task(_health_watchdog(request_task))

        try:
            result = await request_task
        except asyncio.CancelledError:
            # Watchdog cancelled us — stalled or runaway
            logger.error("Inference cancelled by health watchdog (stalled or runaway)")
            result = InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error="Generation aborted by watchdog (stall or runaway)",
            )
        finally:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        return result

    # ── Memoryful session methods ─────────────────────────────────

    async def start_session(
        self,
        config: dict | None = None,
        static_prefix: str | None = None,
        flow_key: str | None = None,
        from_snapshot: str | None = None,
    ) -> str:
        """Start a memoryful session via GraphQL mutation.

        ``static_prefix`` + ``flow_key`` opt the session into the resident
        cross-session flow-fork (server flag ``resident_session_flow_fork``): the
        invariant persona head named by ``flow_key`` is pinned once per instance
        and reused on every later session of the same flow, so only the first
        user message prefills instead of re-prefilling the preamble. Both must be
        present to take effect; absent → today's plain session (no-op).

        ``from_snapshot`` forks the session from a pinned semi-permanent
        snapshot (see session_snapshot) — unknown keys error server-side
        before any state is touched.

        Returns:
            session_id string.
        """
        client = await self._get_client()
        ttl = (config or {}).get("ttl_seconds", 300)
        cfg: dict[str, Any] = {"ttlSeconds": ttl}
        if static_prefix and flow_key:
            cfg["staticPrefix"] = static_prefix
            cfg["flowCacheKey"] = flow_key
        if from_snapshot:
            cfg["fromSnapshot"] = from_snapshot
        # Named persona (multi-persona pooling): the session pins a seat
        # carrying that persona's static head (SOUL) — e.g. "user_sim",
        # "tau_boss". Absent/None → the default persona, exactly as before.
        if (config or {}).get("persona"):
            cfg["persona"] = str(config["persona"])

        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": START_SESSION_MUTATION,
                    "variables": {"config": cfg},
                },
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                error_msg = "; ".join(e.get("message", str(e)) for e in data["errors"])
                raise InferenceError(f"Start session failed: {error_msg}")

            return data["data"]["startSession"]["sessionId"]

        except httpx.ConnectError as e:
            raise InferenceError(f"Cannot connect to LLMVP: {e}") from e

    async def session_turn(
        self,
        session_id: str,
        prompt: str,
        config_overrides: dict | None = None,
    ) -> InferenceResult:
        """Run a turn within a memoryful session via GraphQL query.

        Returns:
            InferenceResult with the model's response.
        """
        client = await self._get_client()

        request_vars: dict[str, Any] = {
            "sessionId": session_id,
            "prompt": prompt,
        }

        if config_overrides:
            if "temperature" in config_overrides:
                temp = resolve_temperature(
                    config_overrides["temperature"],
                    self._model_default_temperature,
                )
                if temp is not None:
                    request_vars["temperature"] = temp
            if "max_tokens" in config_overrides:
                mt = config_overrides["max_tokens"]
                if mt is not None:
                    request_vars["maxTokens"] = int(mt)
            if "grammar" in config_overrides:
                grammar = config_overrides["grammar"]
                if grammar is not None:
                    request_vars["grammar"] = grammar
            # Reasoning HEAD-SWAP level (low/medium/high) — the server installs the
            # level's pinned head at turn 0 and SPLICES it mid-session
            # (config.model.reasoning_head_swap). Stateless completions carry the
            # same field (see the completion builder above). None/absent → default.
            if config_overrides.get("reasoning"):
                request_vars["reasoning"] = str(config_overrides["reasoning"])

        request_body = {
            "query": SESSION_COMPLETION_QUERY,
            "variables": {"request": request_vars},
        }

        # Route through the same health watchdog as completions. The session
        # path previously fired a raw POST on the timeout=None client with no
        # guard, so a stuck or runaway turn (e.g. the Qwen3-Next long-context
        # decay-clamp repetition loop) could hang the agent indefinitely until
        # an external kill. The ceiling bounds runaways; stall detection bounds
        # stuck instances — protecting all diagnosis reasoning + AST edits.
        return await self._request_with_health_watchdog(
            client,
            request_body,
            response_key="sessionCompletion",
            runaway_token_ceiling=SESSION_RUNAWAY_TOKEN_CEILING,
        )

    async def end_session(self, session_id: str) -> bool:
        """End a memoryful session via GraphQL mutation."""
        client = await self._get_client()

        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": END_SESSION_MUTATION,
                    "variables": {"sessionId": session_id},
                },
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                logger.error("End session errors: %s", data["errors"])
                return False

            return data["data"]["endSession"]

        except Exception as e:
            logger.error("End session error: %s", e)
            return False

    async def session_snapshot(self, session_id: str, key: str) -> dict:
        """Pin the session's current context as a semi-permanent snapshot.

        Raises InferenceError on server rejection (capacity/budget/duplicate)
        — the caller must know the snapshot does NOT exist rather than
        proceeding to fork from a phantom.
        """
        client = await self._get_client()
        response = await client.post(
            self._endpoint,
            json={
                "query": SESSION_SNAPSHOT_MUTATION,
                "variables": {"sessionId": session_id, "key": key},
            },
        )
        response.raise_for_status()
        data = response.json()
        if "errors" in data:
            error_msg = "; ".join(e.get("message", str(e)) for e in data["errors"])
            raise InferenceError(f"Session snapshot failed: {error_msg}")
        snap = data["data"]["sessionSnapshot"]
        return {
            "key": snap["key"],
            "tokens": snap["tokens"],
            "resident": snap["resident"],
            "turn_count": snap["turnCount"],
        }

    async def purge_snapshot(self, key: str) -> bool:
        """Free a pinned semi-permanent snapshot. False = didn't exist."""
        client = await self._get_client()
        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": PURGE_SNAPSHOT_MUTATION,
                    "variables": {"key": key},
                },
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                logger.error("Purge snapshot errors: %s", data["errors"])
                return False
            return bool(data["data"]["purgeSnapshot"])
        except Exception as e:
            logger.error("Purge snapshot error: %s", e)
            return False
