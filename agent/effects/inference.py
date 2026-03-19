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

# GraphQL query for health check
HEALTH_QUERY = """
query Health {
    health {
        status
        poolSize
        availableInstances
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

        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": COMPLETION_QUERY,
                    "variables": {"request": request_vars},
                },
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                error_msg = "; ".join(e.get("message", str(e)) for e in data["errors"])
                logger.error("GraphQL inference errors: %s", error_msg)
                return InferenceResult(
                    text="",
                    tokens_generated=0,
                    finished=False,
                    error=f"GraphQL errors: {error_msg}",
                )

            completion = data["data"]["completion"]
            # Strip trailing partial stop tokens (e.g. "<" from truncated "<|im_end|>")
            text = completion["text"]
            if text.endswith("\n<"):
                text = text[:-2]
            elif text.endswith("<"):
                text = text[:-1]
            return InferenceResult(
                text=text,
                tokens_generated=completion["tokensGenerated"],
                finished=completion["finished"],
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
        except Exception as e:
            logger.error("Unexpected inference error: %s", e)
            return InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error=f"Unexpected error: {e}",
            )
