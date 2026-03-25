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
    }
}
"""

# GraphQL query for health check (includes generation progress)
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

SESSION_COMPLETION_QUERY = """
query SessionCompletion($request: SessionTurnRequest!) {
    sessionCompletion(request: $request) {
        text
        tokensGenerated
        finished
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


class InferenceEffect:
    """GraphQL client for LLMVP inference.

    Constructs GraphQL queries, sends HTTP requests to the LLMVP endpoint,
    and parses responses into InferenceResult.

    Args:
        endpoint: The LLMVP GraphQL endpoint URL.
        model_default_temperature: Default temperature for the model
            (used to resolve relative temperature values like "t*0.5").
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8000/graphql",
        model_default_temperature: float = 0.7,
        timeout: float = 600.0,
    ) -> None:
        self._endpoint = endpoint
        self._model_default_temperature = model_default_temperature
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
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

    async def run_inference(
        self,
        prompt: str,
        config_overrides: dict | None = None,
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

        # Build the request variables
        request_vars: dict[str, Any] = {"prompt": prompt}

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

        request_body = {
            "query": COMPLETION_QUERY,
            "variables": {"request": request_vars},
        }

        # Use the watchdog-backed request for non-session inference
        return await self._request_with_health_watchdog(client, request_body)

    async def _request_with_health_watchdog(
        self,
        client: httpx.AsyncClient,
        request_body: dict,
    ) -> InferenceResult:
        """Execute an inference request with health-polling watchdog.

        Strategy:
        1. Fire the request with a long timeout (self._timeout).
        2. Concurrently run a watchdog that polls LLMVP health every 30s.
        3. If health shows tokens stalled for 60s+ (two consecutive polls
           with no token increase), cancel the request — the model is stuck.
        4. If health shows tokens increasing, the watchdog stays quiet and
           lets the request complete naturally.

        This means productive long generations (large files) are never
        killed prematurely, but stuck generations are caught within ~90s.
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

                completion = data["data"]["completion"]
                return InferenceResult(
                    text=completion["text"],
                    tokens_generated=completion["tokensGenerated"],
                    finished=completion["finished"],
                )

            except httpx.ConnectError as e:
                logger.error("Cannot connect to LLMVP at %s: %s", self._endpoint, e)
                return InferenceResult(
                    text="", tokens_generated=0, finished=False,
                    error=f"Connection error: {e}",
                )
            except httpx.TimeoutException as e:
                logger.error("LLMVP inference timed out: %s", e)
                return InferenceResult(
                    text="", tokens_generated=0, finished=False,
                    error=f"Timeout: {e}",
                )
            except httpx.HTTPStatusError as e:
                logger.error("LLMVP HTTP error: %s", e)
                return InferenceResult(
                    text="", tokens_generated=0, finished=False,
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

                    if gen_active and tokens > 0:
                        if tokens > last_token_count:
                            # Model is actively generating — reset stall tracking
                            last_token_count = tokens
                            logger.debug(
                                "Health watchdog: generation active, %d tokens so far",
                                tokens,
                            )
                        elif stall_secs is not None and stall_secs > stall_threshold:
                            # Tokens haven't advanced and LLMVP confirms stall
                            logger.warning(
                                "Health watchdog: generation stalled for %.0fs "
                                "at %d tokens — cancelling request",
                                stall_secs,
                                tokens,
                            )
                            request_task.cancel()
                            return
                    elif not gen_active and last_token_count > 0:
                        # Generation ended — request should complete soon
                        logger.debug("Health watchdog: generation finished")
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
            # Watchdog cancelled us — return a stall error
            logger.error("Inference cancelled by health watchdog (generation stalled)")
            result = InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error="Generation stalled (no new tokens for 60s+)",
            )
        finally:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        return result

    # ── Memoryful session methods ─────────────────────────────────

    async def start_session(self, config: dict | None = None) -> str:
        """Start a memoryful session via GraphQL mutation.

        Returns:
            session_id string.
        """
        client = await self._get_client()
        ttl = (config or {}).get("ttl_seconds", 300)

        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": START_SESSION_MUTATION,
                    "variables": {"config": {"ttlSeconds": ttl}},
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

        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": SESSION_COMPLETION_QUERY,
                    "variables": {"request": request_vars},
                },
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                error_msg = "; ".join(e.get("message", str(e)) for e in data["errors"])
                logger.error("Session turn errors: %s", error_msg)
                return InferenceResult(
                    text="",
                    tokens_generated=0,
                    finished=False,
                    error=f"Session turn errors: {error_msg}",
                )

            completion = data["data"]["sessionCompletion"]
            return InferenceResult(
                text=completion["text"],
                tokens_generated=completion["tokensGenerated"],
                finished=completion["finished"],
            )

        except Exception as e:
            logger.error("Session turn error: %s", e)
            return InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error=f"Session turn error: {e}",
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
