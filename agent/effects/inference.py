"""Inference via LLMVP GraphQL API.

Pure HTTP client that constructs GraphQL queries, sends them to the LLMVP
endpoint via httpx.AsyncClient, and parses responses. Ouroboros does NOT
import anything from LLMVP.

Also handles relative temperature resolution: "t*0.5" style values are
parsed and resolved against a configured model default temperature.
"""

from __future__ import annotations

import logging
import asyncio
import os
import re
import uuid
from typing import Any

import httpx

from agent.effects.protocol import InferenceResult

logger = logging.getLogger(__name__)


def _reasoning_off() -> bool:
    """Global reasoning kill-switch, enforced at the effect choke point.

    OURO_REASONING_OFF=1 must restore pre-feature behavior EXACTLY. The
    router honors it, but action-level config dicts (e.g. deep_search's
    _CONDENSE_CFG) reach here without passing through resolve_reasoning —
    without this guard they leak reasoning levels into baseline A/B arms.
    """
    return os.environ.get("OURO_REASONING_OFF", "") == "1"


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
        reasoningTokens
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

# Watchdog variant: adds the fields that let the watchdog know WHOSE generation
# it is looking at, plus the server's advisory expectedEvalSeconds (worst-case
# prefill estimate for the in-flight prompt). Kept SEPARATE from HEALTH_QUERY
# because an older server rejects unknown fields with an empty ``data`` — the
# watchdog detects that and downgrades to HEALTH_QUERY; other health consumers
# never need the fields and stay on the legacy query. That downgrade is also
# what selects fallback mode: a server that cannot name the request is a server
# whose verdicts we cannot use, so the old heuristic takes over.
HEALTH_QUERY_WATCHDOG = """
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
        expectedEvalSeconds
        requestId
        thinkingComplete
        decodeMode
        engineActiveStreams
    }
}
"""

# Pool-sizing variant: the facts the swarm admission gate needs
# (kvPoolTokens = the server's real KV token budget; decodeMode tells
# shared-pool from per-instance semantics). Kept SEPARATE from HEALTH_QUERY
# for the same reason as HEALTH_QUERY_WATCHDOG: an older server rejects
# unknown fields with GraphQL errors — pool_health() treats that as "server
# doesn't report" and callers fall back to their static budget.
POOL_HEALTH_QUERY = """
query Health {
    health {
        status
        decodeMode
        kvPoolTokens
        poolSize
        availableInstances
    }
}
"""

# The CACHE/FEATURE register. Everything here lives ONLY in health and was
# never persisted into a run artifact, so a finished run could not answer the
# questions it was most useful for: did the flow cache actually fire, what
# strategy did this model land on, and did a context refresh wipe the caches
# mid-run (which makes two runs incomparable).
#
# The strategy triple is the load-bearing part: `resident_seq_cache` is a
# REQUEST that memory_can_shift() can refuse, so the effective strategy is
# knowable only from here — which is exactly what OPEN_TASKS §12 needs to tag
# a tier arm with the substrate it actually ran on.
#
# SEPARATE query for the same reason as the two above: an older server rejects
# unknown fields outright, and a telemetry nicety must never fail a run.
CACHE_HEALTH_QUERY = """
query Health {
    health {
        status
        decodeMode
        sessionStrategy
        sessionCanShift
        residentRequested
        residentActive
        nCtxSeq
        flowBuilds
        flowHits
        flowEvicts
        flowFallbacks
        flowCacheEntries
        contextRefreshes
        runawayCaptures
        decodeTpsRecent
        prefillTpsRecent
        memSystemWiredMb
        kvPoolTokens
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
        reasoningTokens
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

RUNAWAY_CAPTURE_QUERY = """
query RunawayCapture($requestId: String!) {
    runawayCapture(requestId: $requestId) {
        found
        reason
        text
        tokensGenerated
        elidedBytes
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


# Markers that identify a server-side degeneration verdict inside a GraphQL
# error message. Each is the leading text of a reason string LLMVP's guards
# produce (llmvp/inference/repetition.py::RepetitionGuard.observe and
# token_pipeline.py::TokenPipeline.feed), carried out as a
# DegenerateGenerationError. Matching on the reason's own words rather than on
# an exception class name keeps this a plain HTTP client — Ouroboros imports
# nothing from LLMVP.
_DEGENERATE_MARKERS = (
    "run-length ",
    "cycle period ",
    "long-cycle",
    "detokenization failed",
)


def _request_identity(request_body: dict) -> str:
    """The id the server will publish on health for this request.

    Two shapes, because the server derives the id differently per path:

    * **Session turns** are already identified by their session id — turns are
      sequential and only one driver issues them, so LLMVP labels the
      generation with the session id rather than inventing a second key. We
      must match on the SAME value, so read it out rather than stamping.
    * **Stateless completions** carry no natural key, so mint one and stamp it
      into the request variables as ``requestId``.

    Returns "" if the body has neither shape (nothing to match on, so the
    watchdog stays in fallback mode rather than matching on a guess).
    """
    variables = (request_body or {}).get("variables") or {}
    request_vars = variables.get("request")
    if not isinstance(request_vars, dict):
        return ""
    session_id = request_vars.get("sessionId")
    if session_id:
        return str(session_id)
    request_id = f"ouro-{uuid.uuid4().hex[:16]}"
    request_vars["requestId"] = request_id
    return request_id


def _degenerate_reason(error_msg: str) -> str:
    """The server's degeneration reason inside an error message, else "".

    Returns the reason from its marker onward — "long-cycle repetition:
    12/2048 distinct 24B n-grams …" — so what reaches the flow is what the
    guard actually saw, not a generic "inference failed".
    """
    if not error_msg:
        return ""
    lowered = error_msg.lower()
    hits = [lowered.find(m) for m in _DEGENERATE_MARKERS]
    starts = [i for i in hits if i >= 0]
    return error_msg[min(starts) :].strip() if starts else ""


# The server appends "(aborted after N generated tokens)" to a degenerate
# abort. Those tokens were really produced and really cost wall clock, but the
# call returns no text, so without recovering N the trace records the whole
# generation as 0 and every tokens-per-output figure flatters the models that
# degenerate most. Absent on an older server → None, never 0: "we did not
# measure" and "nothing was generated" are the two readings this must not merge.
_DEGENERATE_TOKENS = re.compile(r"aborted after (\d+) generated tokens")


def _degenerate_tokens(error_msg: str) -> int | None:
    m = _DEGENERATE_TOKENS.search(error_msg or "")
    return int(m.group(1)) if m else None


# FALLBACK-ONLY ceilings on tokens a single generation may produce before the
# health watchdog cancels it as a runaway. They bound the Qwen3-Next repetition
# bug (unclamped Gated-DeltaNet decay) which otherwise generates to max_tokens
# (262k) — observed as both a ~79-min session hang AND a ~20-min completion
# (whole-file rewrite) runaway. The stall watchdog can't catch those: a
# repetition loop keeps tokens *advancing*, so only a token ceiling stops it.
#
# Two tiers because legitimate output sizes differ:
#   - session turns (diagnosis menus ~64 tok, conclude ~1-2k, AST symbol edits
#     a few k) never approach 32k.
#   - completions (whole-file rewrites, multi-file scaffolds) legitimately reach
#     ~10-24k, so the ceiling sits higher with headroom — still far below 262k.
#
# WHY FALLBACK-ONLY (2026-07-28). LLMVP now detects degeneration itself, and far
# better than a token count can: RepetitionGuard aborts token-level collapse
# within tens of tokens, and the long-cycle guard catches paragraph-scale orbits
# every ~2k tokens — the latter built for THIS exact Qwen3-Next failure, whose
# post-mortem records "130k+ tokens, 43 watchdog cancellations, text discarded".
# Both name a reason and dump the specimen. A blind token count cannot tell
# 46k tokens of chain-of-thought from 46k tokens of loop, and on 2026-07-28 it
# guessed wrong: a qwen3.6-35b batch turn was cancelled at 49,987 tokens, 200s
# before the server delivered a complete, correct eleven-file artifact.
#
# So when health names the request (LLMVP, tracker-served) the server owns the
# degeneration verdict and these are advisory only. They still CANCEL where no
# verdict is available — an older server, or a remote-provider passthrough that
# never touches the tracker — which is the case they were written for anyway.
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
        model: Optional LLMVP registry model name for ALL completions
            from this effect (a remote provider entry or the active
            local config). Per-call config_overrides["model"] wins.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:8008/graphql",
        model_default_temperature: float = 0.7,
        model: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._model_default_temperature = model_default_temperature
        self._model = model
        self._client: httpx.AsyncClient | None = None
        # Session ids this client currently holds open. A memoryful session
        # CHECKS OUT its backend instance and keeps it across turns (seq 0
        # stays resident so the next turn skips prefill) — see the checkout
        # comment in llama_cpp_backend.acquire_instance. So while this set is
        # non-empty we are holding a seat, and a "busy" verdict on any other
        # call may be us blocking ourselves. Used by _is_self_deadlock.
        self._open_sessions: set[str] = set()
        # Health-watchdog timing. INSTANCE attributes, not function-local
        # constants, so tests can shrink them (TESTING.md: "timing knobs used
        # by drains/settles should be instance attributes"). The watchdog is
        # the ONLY bound on a request — inference runs with httpx
        # timeout=None — so its decisions have to be exercisable, and with
        # 60s/30s baked in nothing could reach them in a test.
        self._watchdog_grace_s: float = 60  # don't poll during the first 60s
        self._watchdog_poll_s: float = 30  # health check interval
        self._watchdog_stall_s: float = 60  # cancel after this long with no tokens
        # Explicit override for the eval-phase ceiling. None keeps the shipped
        # behavior: OURO_EVAL_STUCK_S (default 300s), raised to 2x the
        # server-advertised expectedEvalSeconds when it offers one.
        self._watchdog_eval_stuck_s: float | None = None
        # Retries for TRANSIENT infrastructure failures (capacity/connection).
        # Instance attribute so tests can drive the loop without waiting on the
        # real backoff, same rule as the watchdog knobs above.
        self._transient_retries: int = int(
            os.environ.get("OURO_TRANSIENT_RETRIES", "3")
        )

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

    async def cache_health(self) -> dict:
        """The cache/feature register, or {} if the server does not report it.

        Best-effort by construction: an older server rejects the unknown
        fields with GraphQL errors, and a timeout or a transport blip must
        never fail a run for a telemetry read. Callers treat {} as "not
        reported" and record nothing rather than recording a zero — a zero
        here would be indistinguishable from "the flow cache never fired",
        which is the exact question this exists to answer.
        """
        try:
            client = await self._get_client()
            response = await client.post(
                self._endpoint,
                json={"query": CACHE_HEALTH_QUERY},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                return {}
            return data.get("data", {}).get("health", {}) or {}
        except Exception:  # noqa: BLE001 — telemetry never breaks a run
            return {}

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

    async def pool_health(self) -> dict:
        """Fetch the pool-sizing health facts (kvPoolTokens, decodeMode,
        poolSize).

        Returns {} on ANY failure — connection error, GraphQL errors (an
        older server rejects the unknown fields with errors), or empty
        data — so callers fall back to their static budget without
        exception handling. A sizing hint must never fail a fan-out.
        """
        try:
            client = await self._get_client()
            response = await client.post(
                self._endpoint,
                json={"query": POOL_HEALTH_QUERY},
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                return {}
            return (data.get("data") or {}).get("health") or {}
        except Exception:  # noqa: BLE001 — downgrade to "not reported"
            return {}

    async def token_count(
        self, texts: list[str], model: str = "", timeout: float = 15.0
    ) -> list[int]:
        """Exact token counts from the SERVING model's own tokenizer.

        Returns [] on any failure — an empty result means "size it the old
        way", so a caller degrades to its estimate instead of blocking on
        a sizing hint. Batched because the question is usually asked about
        a whole fan-out at once.
        """
        query = (
            "query TokenCount($texts:[String!]!,$model:String!)"
            "{ tokenCount(texts:$texts, model:$model)"
            "{ counts total nVocab error } }"
        )
        try:
            client = await self._get_client()
            response = await client.post(
                self._endpoint,
                json={
                    "query": query,
                    "variables": {"texts": list(texts), "model": model},
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                return []
            tc = (data.get("data") or {}).get("tokenCount") or {}
            if tc.get("error"):
                logger.debug("token_count server error: %s", tc["error"])
                return []
            counts = tc.get("counts") or []
            return [int(c) for c in counts]
        except Exception as e:  # noqa: BLE001 — a sizing hint never fails a caller
            logger.debug("token_count failed: %s", e)
            return []

    async def raw_graphql(self, query: str, timeout: float = 10.0) -> dict:
        """POST a query and return the decoded envelope verbatim.

        Unlike pool_health this does NOT swallow `errors`: a caller that
        needs to tell "the server refused this field" (a version gap worth
        degrading over) from "the server is unreachable" (a transient worth
        retrying) cannot do it from an empty dict. Raises on transport
        failure for the same reason.
        """
        client = await self._get_client()
        response = await client.post(
            self._endpoint, json={"query": query}, timeout=timeout
        )
        response.raise_for_status()
        return response.json()

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

    async def fetch_runaway_capture(self, request_id: str) -> dict | None:
        """Fetch the server's partial-text capture of an aborted generation.

        When the server kills a generation as degenerate it discards the
        stream but dumps the partial text to its runaway-capture log,
        keyed by the correlation id we minted. The batch slicer uses this
        to salvage completed FILE blocks out of an aborted mega-turn
        instead of regenerating everything serially.

        Returns {"text", "reason", "tokens_generated", "elided_bytes"} or
        None when no capture matched (older server, capture rotation, or
        a dump that failed server-side). Never raises.
        """
        if not request_id:
            return None
        client = await self._get_client()
        try:
            response = await client.post(
                self._endpoint,
                json={
                    "query": RUNAWAY_CAPTURE_QUERY,
                    "variables": {"requestId": request_id},
                },
                timeout=15.0,
            )
            response.raise_for_status()
            data = response.json()
            if "errors" in data:
                logger.debug("Runaway capture query errors: %s", data["errors"])
                return None
            cap = (data.get("data") or {}).get("runawayCapture") or {}
            if not cap.get("found"):
                return None
            return {
                "text": cap.get("text", "") or "",
                "reason": cap.get("reason", "") or "",
                "tokens_generated": int(cap.get("tokensGenerated") or 0),
                "elided_bytes": int(cap.get("elidedBytes") or 0),
            }
        except Exception as e:  # noqa: BLE001 — salvage is best-effort
            logger.debug("Failed to fetch runaway capture: %s", e)
            return None

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
            if config_overrides.get("reasoning") and not _reasoning_off():
                request_vars["reasoning"] = str(config_overrides["reasoning"])

        # Registry model routing (multi-model Phase 4): per-call override
        # beats the effect-level default; absent -> the server's resident
        # model, exactly as before.
        model = (config_overrides or {}).get("model") or self._model
        if model:
            request_vars["model"] = str(model)

        request_body = {
            "query": COMPLETION_QUERY,
            "variables": {"request": request_vars},
        }

        # Use the watchdog-backed request for non-session inference. The
        # completion ceiling is the FALLBACK bound for servers that cannot name
        # the request; where LLMVP can, its repetition and long-cycle guards
        # own runaways and the watchdog only checks liveness.
        return await self._request_with_health_watchdog(
            client,
            request_body,
            runaway_token_ceiling=COMPLETION_RUNAWAY_TOKEN_CEILING,
        )

    # Errors that mean "the infrastructure could not take this request right
    # now", as opposed to "the model produced nothing". ONLY these retry.
    # Deliberately NOT included: timeouts (the server may still be working, and
    # retrying stacks load — that is how the 07-26 orphan prefill was made),
    # watchdog aborts (already a considered decision), and GraphQL schema
    # errors (a bug, not weather).
    _TRANSIENT_ERROR_MARKERS = (
        "instances are busy",
        "connection error",
    )
    # Backoff between attempts. The server has ALREADY waited backend_timeout
    # (180s) before reporting busy, so an instant retry is pointless — these
    # are chosen to ride out a long generation finishing on the other seat.
    _TRANSIENT_BACKOFF_S = (5.0, 15.0, 45.0)

    def _is_transient(self, result: InferenceResult) -> bool:
        err = (result.error or "").lower()
        return bool(err) and any(m in err for m in self._TRANSIENT_ERROR_MARKERS)

    def _is_self_deadlock(self, result: InferenceResult) -> bool:
        """True when the seat we are waiting for is one we are holding.

        A memoryful session checks its instance out and keeps it BETWEEN
        turns, by design. When a stateless call is issued inside that
        session's lifetime and the pool is at its limit, the only seat is
        ours and no amount of waiting frees it: the mission blocks on itself.

        Live on 2026-08-24 (qwen3.8 polish run): a diagnosis session finished
        turn 4 and kept its pin; the fix path dispatched data_patch, whose
        translation is a STATELESS completion; it burned the whole 4-attempt
        ladder over ~10 minutes and gave up 52ms before the session's own
        turn 5 began. One in three data_patch dispatches that night.

        Deliberately keyed on capacity only ("busy"), never on connection
        errors — a dropped connection is real weather and must still retry.

        Slightly pessimistic when the pool has been scaled past one seat and
        the exhaustion is genuinely someone else's: we then skip a retry that
        might have worked. That trade is deliberate. The cost of failing fast
        is one cheap fallback (data_patch defers to the full rewrite, which
        already carries the repair); the cost of waiting on ourselves is
        minutes of wall-clock per occurrence and, under an event-driven wait
        rather than a bounded ladder, a hang with no fallback at all.
        """
        if not self._open_sessions:
            return False
        err = (result.error or "").lower()
        return "instances are busy" in err

    async def _request_with_health_watchdog(
        self,
        client: httpx.AsyncClient,
        request_body: dict,
        response_key: str = "completion",
        runaway_token_ceiling: int | None = None,
    ) -> InferenceResult:
        """Run the request, retrying TRANSIENT infrastructure failures.

        Why this exists (2026-07-26): a mistral boss run died five minutes in
        because a leaked prefill held the single seat, the request came back
        "All inference instances are busy", and design_initial's resolver
        cannot tell that from "the model generated nothing" — both have
        tokens_generated == 0, and its catch-all routes to a TERMINAL failure.
        A seven-hour mission ended on a condition that cleared by itself
        moments later. Five planning steps share that resolver shape.

        The retry belongs HERE rather than in each flow: capacity is
        infrastructure, not flow semantics, and asking every flow author to
        handle "busy" is how one gets missed. Each attempt carries its own
        watchdog, so a slow generation is still bounded normally.
        """
        attempts = self._transient_retries + 1
        result = None
        for i in range(attempts):
            result = await self._request_once_with_watchdog(
                client, request_body, response_key, runaway_token_ceiling
            )
            if not self._is_transient(result):
                return result
            if self._is_self_deadlock(result):
                # ERROR, not warning: this is a structural condition (a call
                # shape that cannot be served while we hold a session), not
                # capacity weather, and the caller's fallback is about to be
                # taken on the strength of it.
                logger.error(
                    "Inference refused and NOT retried: the pool is at its "
                    "limit and this client holds %d open session(s) (%s) — "
                    "the seat is ours, so waiting cannot free it. Failing "
                    "fast to the caller's fallback. %s",
                    len(self._open_sessions),
                    ", ".join(sorted(self._open_sessions))[:120],
                    result.error,
                )
                return result
            if i < attempts - 1:
                delay = self._TRANSIENT_BACKOFF_S[
                    min(i, len(self._TRANSIENT_BACKOFF_S) - 1)
                ]
                # WARNING, not debug: a silent retry is indistinguishable from
                # a healthy call, and this whole class of bug hides in that gap.
                logger.warning(
                    "Transient inference failure (attempt %d/%d): %s — "
                    "retrying in %.0fs",
                    i + 1,
                    attempts,
                    result.error,
                    delay,
                )
                await asyncio.sleep(delay)
        logger.error(
            "Transient inference failure persisted after %d attempts: %s",
            attempts,
            result.error if result else "no result",
        )
        return result

    async def _request_once_with_watchdog(
        self,
        client: httpx.AsyncClient,
        request_body: dict,
        response_key: str = "completion",
        runaway_token_ceiling: int | None = None,
    ) -> InferenceResult:
        """Execute an inference request with a health-polling LIVENESS watchdog.

        Strategy:
        1. Fire the request with no fixed timeout (httpx timeout=None).
        2. Concurrently run a watchdog that polls LLMVP health every 30s.
        3. Act ONLY on health that names this request (``requestId``). The
           tracker is a single-slot singleton, so an unmatched snapshot is
           somebody else's generation and says nothing about ours.
        4. Cancel on LIVENESS failures against our own numbers: tokens stalled
           past the threshold, prompt eval overrunning the server's own
           estimate, zero tokens and stalled. A wedged server cannot report its
           own death — this is the part no server-side detector can replace.
        5. Leave CONTENT judgement to the server. LLMVP's repetition and
           long-cycle guards abort degenerate generations themselves, name the
           reason and dump the specimen; the watchdog relays that verdict
           instead of guessing at it from a token count.
        6. Fall back to the historical token-ceiling behavior only where no
           identity is available (older server, remote-provider passthrough).

        ``response_key`` selects the GraphQL payload field — "completion" for
        normal completions, "sessionCompletion" for memoryful session turns.

        Productive long generations are never killed; stuck ones still are.
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
                    reason = _degenerate_reason(error_msg)
                    if reason:
                        # The server caught a loop and named it. This is the
                        # verdict the watchdog used to approximate with a token
                        # ceiling; log it as such rather than burying it in the
                        # generic GraphQL-error line.
                        logger.warning(
                            "Server aborted the generation as degenerate: %s",
                            reason,
                        )
                    else:
                        logger.error("GraphQL inference errors: %s", error_msg)
                    return InferenceResult(
                        text="",
                        tokens_generated=0,
                        finished=False,
                        error=f"GraphQL errors: {error_msg}",
                        degenerate=bool(reason),
                        degenerate_reason=reason,
                        # The generation produced no usable text but really did
                        # burn these tokens; keeping them out of the record made
                        # degenerating models look token-efficient.
                        degenerate_tokens=_degenerate_tokens(error_msg),
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
                    reasoning_tokens=completion.get("reasoningTokens", 0) or 0,
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

        import asyncio

        # Label this request so health can tell the watchdog whether the numbers
        # it is reading are ours. A server that ignores the label simply reports
        # no id, and the watchdog runs in fallback mode.
        request_id = _request_identity(request_body)

        # Run the request with the watchdog
        request_task = asyncio.create_task(_do_request())
        watchdog_task = asyncio.create_task(
            self._health_watchdog(request_task, runaway_token_ceiling, request_id)
        )

        try:
            result = await request_task
        except asyncio.CancelledError:
            # Watchdog cancelled us — the server stopped making progress on OUR
            # request. Note the server may still be decoding: cancelling drops
            # the consumer, not the generation.
            logger.error("Inference cancelled by health watchdog (no progress)")
            result = InferenceResult(
                text="",
                tokens_generated=0,
                finished=False,
                error="Generation aborted by watchdog (no progress)",
            )
        finally:
            watchdog_task.cancel()
            try:
                await watchdog_task
            except asyncio.CancelledError:
                pass

        # Stamp the correlation id on every outcome. On a degenerate abort
        # this is what lets the caller fetch the server's runaway capture
        # (partial text) for salvage — see fetch_runaway_capture.
        result.request_id = request_id
        return result

    async def _poll_health(self, query: str) -> dict:
        """One health poll. Its own method so tests can drive the watchdog's
        DECISIONS with scripted payloads instead of standing up an HTTP
        server — the health JSON is the only input the loop reads."""
        health_client = httpx.AsyncClient(timeout=10.0)
        try:
            resp = await health_client.post(self._endpoint, json={"query": query})
            body = resp.json()
            return (body.get("data") or {}).get("health") or {}
        finally:
            await health_client.aclose()

    def _identify(self, health: dict, request_id: str) -> tuple[bool, str]:
        """Whose generation does this health snapshot describe?

        Returns ``(usable, why)``:

        * ``(True, "mine")`` — health names our request. Its numbers are ours.
        * ``(True, "unidentified")`` — the server cannot attribute generations
          at all (no ``requestId`` field, or we never got an id to match on).
          Usable only in the historical, heuristic sense: this is the
          older-server / remote-passthrough case, and the caller keeps the
          token ceiling for it.
        * ``(False, …)`` — the snapshot is someone else's, or is a blend. Not
          evidence about our request.

        The blend case is real: under ``decode_mode: batched`` the tracker's
        single status slot is reset by every interleaved ``start()``, so with
        more than one stream live the token count belongs to no single request
        (see GenerationTracker.finish's note on ``quiet=True``). The batched
        engine reports truthful per-stream numbers at retirement, and owns
        abandonment and force-windowing per stream — so staying quiet there
        loses nothing.
        """
        if "requestId" not in health:
            return True, "unidentified"  # server predates identity — heuristic
        if not request_id:
            return True, "unidentified"  # nothing to match on — heuristic
        reported = health.get("requestId") or ""
        if reported != request_id:
            return False, f"requestId={reported or '(none)'}"
        if (health.get("decodeMode") or "") == "batched" and (
            health.get("engineActiveStreams") or 0
        ) > 1:
            return False, "batched blend (numbers span multiple streams)"
        return True, "mine"

    async def _health_watchdog(
        self,
        request_task: asyncio.Task,
        runaway_token_ceiling: int | None = None,
        request_id: str = "",
    ) -> None:
        """Monitor OUR generation's liveness and cancel if it stops progressing.

        Acts only on health snapshots that name ``request_id``. The generation
        tracker is a single-slot singleton, so an unmatched snapshot describes
        somebody else's work and is not evidence about ours — reading it as
        ours is how six requests were cancelled by an orphan's token count on
        2026-07-28, each having generated nothing at all.

        The grace and stall clocks therefore start when health first CONFIRMS
        our generation, not when the request was issued. A request queued behind
        a busy instance used to burn its grace period waiting and then judge
        itself on the first poll (the 60,027 ms cancels in that same run).

        When no identity is available — an older server, or a remote-provider
        passthrough that never touches the tracker — the loop falls back to the
        historical behavior including ``runaway_token_ceiling``.
        """
        import asyncio as _asyncio

        poll_interval = self._watchdog_poll_s
        stall_threshold = self._watchdog_stall_s

        await _asyncio.sleep(self._watchdog_grace_s)

        last_token_count = -1
        advisory_logged = False

        # Prefer the enriched health query: requestId (whose generation this
        # is), thinkingComplete, and the server's advisory expectedEvalSeconds
        # worst-case prefill estimate — model-speed knowledge stays server-side.
        # An older server rejects the unknown fields, which surfaces as an empty
        # ``data`` — downgrade to the legacy query ONCE rather than silently
        # polling a dead query for the whole run. That downgrade also SELECTS
        # fallback mode: no identity means no server verdict to trust.
        watchdog_query = HEALTH_QUERY_WATCHDOG

        while not request_task.done():
            try:
                health = await self._poll_health(watchdog_query)

                if not health and watchdog_query is not HEALTH_QUERY:
                    logger.info(
                        "Health watchdog: server lacks the enriched health "
                        "fields — falling back to legacy query (heuristic mode)"
                    )
                    watchdog_query = HEALTH_QUERY
                    continue

                gen_active = health.get("generationActive", False)
                tokens = health.get("tokensGenerated", 0)
                stall_secs = health.get("secondsSinceLastToken")
                phase = health.get("generationPhase", "unknown")
                prompt_toks = health.get("promptTokens", 0)
                elapsed = health.get("elapsedSeconds", 0)
                eval_dur = health.get("evalDuration")
                expected_eval = health.get("expectedEvalSeconds")

                identified, why = self._identify(health, request_id)
                if not identified:
                    # Not our numbers. Someone else holds the instance, or the
                    # server cannot attribute the generation. Either way there
                    # is nothing here to judge — no cancel branch is reached, so
                    # waiting in a queue is free however long it takes.
                    #
                    # Deliberately NOT resetting last_token_count: an
                    # interleaved foreign snapshot must not re-arm our stall
                    # tracking. Resetting it to -1 would make the next matched
                    # poll take the "tokens advancing" branch unconditionally
                    # (anything > -1) and skip the stall check — masking a real
                    # stall for as long as another request keeps appearing.
                    # Stall is decided by the server's secondsSinceLastToken
                    # anyway, which our polling gaps cannot distort.
                    logger.debug(
                        "Health watchdog: health is not ours (%s) — waiting", why
                    )
                    await _asyncio.sleep(poll_interval)
                    continue

                # Heuristic mode only: no server verdict is available for this
                # request, so the blind token ceiling is still the best bound
                # there is. When the server DOES own the verdict its repetition
                # and long-cycle guards abort the turn themselves, far earlier
                # and with the reason named.
                ceiling_applies = why == "unidentified" and runaway_token_ceiling

                if gen_active:
                    if ceiling_applies and tokens > runaway_token_ceiling:
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
                    if (
                        runaway_token_ceiling
                        and not ceiling_applies
                        and tokens > runaway_token_ceiling
                        and not advisory_logged
                    ):
                        # Past where the old ceiling would have fired. The
                        # server's guards have NOT called this degenerate, so it
                        # is a long generation, not a loop — keep the signal for
                        # post-run analysis and let it finish.
                        advisory_logged = True
                        logger.warning(
                            "Health watchdog: long generation — %d tokens past "
                            "the advisory ceiling %d (phase=%s, thinking_done=%s"
                            "). Server guards have not flagged it; NOT "
                            "cancelling.",
                            tokens,
                            runaway_token_ceiling,
                            phase,
                            health.get("thinkingComplete"),
                        )
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
                        # Cancel if eval takes unreasonably long. The
                        # floor is 300s (OURO_EVAL_STUCK_S overrides);
                        # when the server advertises expectedEvalSeconds
                        # (its measured worst-case prefill estimate for
                        # THIS prompt), honor it with 2x headroom — the
                        # server owns model-speed knowledge, we just
                        # consume it. Guards against the fixed-limit doom
                        # loop of 2026-07-23: dense mistral prefills
                        # ~45 tok/s, so a 15k prompt needs ~335s and a
                        # hardcoded 300s cancel discarded nine COMPLETED
                        # generations at 90%+ (each cancel also briefly
                        # wedges the single instance for the retry).
                        _eval_limit = float(os.environ.get("OURO_EVAL_STUCK_S", "300"))
                        if expected_eval:
                            _eval_limit = max(_eval_limit, 2.0 * float(expected_eval))
                        if elapsed and elapsed > _eval_limit:
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

            session_id = data["data"]["startSession"]["sessionId"]
            self._open_sessions.add(session_id)
            return session_id

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
            if config_overrides.get("reasoning") and not _reasoning_off():
                request_vars["reasoning"] = str(config_overrides["reasoning"])

        request_body = {
            "query": SESSION_COMPLETION_QUERY,
            "variables": {"request": request_vars},
        }

        # Route through the same health watchdog as completions. The session
        # path previously fired a raw POST on the timeout=None client with no
        # guard, so a stuck or runaway turn (e.g. the Qwen3-Next long-context
        # decay-clamp repetition loop) could hang the agent indefinitely until
        # an external kill. Stall detection bounds stuck instances; the ceiling
        # is the fallback bound where the server cannot name the request (see
        # its definition) — LLMVP's own guards own runaways otherwise. The
        # session id is what health publishes for these turns, so the watchdog
        # matches on it without anything extra being sent.
        result = await self._request_with_health_watchdog(
            client,
            request_body,
            response_key="sessionCompletion",
            runaway_token_ceiling=SESSION_RUNAWAY_TOKEN_CEILING,
        )
        # STALENESS ESCAPE. A session can die server-side without anyone
        # calling end_session — expiry, an eviction, a server bounce. The seat
        # went with it, so a claim we still hold is a lie, and _is_self_deadlock
        # would fail-fast every stateless call for the rest of the run on the
        # strength of it. The server's own vocabulary for a session it no
        # longer has (core/session_manager.py) is the signal.
        err = (result.error or "").lower()
        if err and ("not found" in err or "expired" in err or "unknown session" in err):
            self._open_sessions.discard(session_id)
        return result

    async def end_session(self, session_id: str) -> bool:
        """End a memoryful session via GraphQL mutation."""
        client = await self._get_client()

        # Drop the seat claim FIRST and unconditionally. If the mutation fails
        # we no longer know that we hold it, and a stale claim would make
        # _is_self_deadlock fail-fast every later stateless call for the rest
        # of the run — turning a lost teardown into a permanent capability
        # loss. Over-claiming costs one wrong fail-fast; under-claiming costs
        # the whole run.
        self._open_sessions.discard(session_id)

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
