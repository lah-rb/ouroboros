#!/usr/bin/env python3
"""
GraphQL API Module

Strawberry GraphQL application for the LLMvp server.
Provides queries, mutations, and subscriptions for LLM inference.
Uses shared inference logic from core.inference.
"""

import json
import logging
import time
from typing import AsyncGenerator, List, Optional

import strawberry
from strawberry.fastapi import GraphQLRouter
from strawberry.extensions import (
    DisableIntrospection,
    MaxAliasesLimiter,
    MaxTokensLimiter,
    QueryDepthLimiter,
)
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Local imports
from core.config import get_config
from core.inference import (
    resolve_max_tokens,
    run_completion,
    run_raw_completion,
    stream_completion,
    run_tool_completion,
    stream_tool_completion,
    get_health_status,
)
from core.lifecycle import initialize_server_async, shutdown_server_async
from core.session_manager import (
    SessionManager,
)

# Set up logging
config = get_config()
log = logging.getLogger("llm-mvp")

# --------------------------------------------------------------------
# 1️⃣ GraphQL Schema Types
# --------------------------------------------------------------------


@strawberry.type
class HealthStatus:
    """Health check response type."""

    status: str
    pool_size: int
    available_instances: int
    active_instances: int
    in_flight: int
    jit_enabled: bool
    # Generation progress (for health polling by clients)
    generation_active: bool = False
    tokens_generated: int = 0
    elapsed_seconds: Optional[float] = None
    seconds_since_last_token: Optional[float] = None
    # Diagnostic phase tracking
    generation_phase: str = "idle"  # idle, eval, generating, complete
    prompt_tokens: int = 0
    eval_duration: Optional[float] = None
    # Advisory worst-case eval estimate for the in-flight prompt, from
    # server-side measured cold-prefill rate (boot static eval seed + observed
    # minimums). Lets clients size stuck-eval timeouts without hardcoding
    # per-model speed knowledge; honoring it is the client's choice.
    expected_eval_seconds: Optional[float] = None
    # Deep-health: long-run degradation signals (pass 1). Memory residency
    # catches the Apple-Silicon unified-memory KV/weight eviction mode; the
    # flow-cache churn/fallback counters catch KV-cache instability under
    # pressure. All default-safe so a backend that omits them stays valid.
    mem_process_rss_mb: Optional[float] = None
    mem_system_used_percent: Optional[float] = None
    mem_system_available_mb: Optional[float] = None
    mem_system_wired_mb: Optional[float] = None
    flow_cache_entries: int = 0
    resident_active: bool = False
    flow_builds: int = 0
    flow_hits: int = 0
    flow_evicts: int = 0
    flow_fallbacks: int = 0
    runaway_captures: int = 0
    # In-process context-refresh telemetry: cumulative rebuilds + requests since the
    # last one. Drives/observes the periodic rot-clearing context rebuild.
    context_refreshes: int = 0
    requests_since_refresh: int = 0
    # Rolling latency/throughput trend (pass 2). throughput_drift = recent
    # decode tps / warm baseline; well under 1.0 flags generation slowdown.
    trend_samples: int = 0
    decode_tps_recent: Optional[float] = None
    prefill_tps_recent: Optional[float] = None
    ttft_recent_s: Optional[float] = None
    decode_tps_baseline: Optional[float] = None
    throughput_drift: Optional[float] = None
    # KV-budget + seat accounting. kv_pool_tokens is the real token budget
    # (model n_ctx: shared pool in batched mode, per-instance in pool mode)
    # — the field the swarm pool-fit gate sizes admission against; pool_size
    # above is only the SEAT count. checked_out vs in_flight separates
    # "seats held" from "GPU busy" (a leaked seat shows here); default-safe
    # for backends that omit them.
    kv_pool_tokens: Optional[int] = None
    # The OTHER context limit (the swarm/model split, 2026-07-24):
    # kv_pool_tokens is the SHARED cell allocation; model_max_context is the
    # trained per-stream ceiling no single prompt/session may exceed.
    model_max_context: Optional[int] = None
    decode_mode: str = ""
    checked_out: int = 0
    engine_active_streams: Optional[int] = None


@strawberry.type
class ThinkingResponse:
    """Chain-of-thought content from a thinking model.

    Available when the model family supports thinking (channel or inline-tag style).
    The thinking content is captured separately from the response
    and accessible through this dedicated endpoint.
    """

    request_id: str
    content: str
    complete: bool
    active: bool


@strawberry.type
class CompletionResponse:
    """Non-streaming completion response.

    ``truncated`` is a derived flag — set when ``tokens_generated`` meets
    or exceeds the effective ``max_tokens`` for the request (the caller's
    override if supplied, else the config default). It signals that the
    model's output was cut off by the token budget rather than ending on
    an EOS or stop sequence. A truncated response may contain a
    partially-completed reasoning chain, an unclosed channel/delimiter
    sequence, or silently empty content after FSM stripping.

    Callers log this for observability; they do not have to handle it
    specially. See the asymmetry fix in session_completion /
    session_turn (Apr 2026) — before that fix, session turns silently
    capped at 256 tokens regardless of the configured default, which
    produced silent empty responses on reasoning-heavy turns.
    """

    text: str
    tokens_generated: int
    finished: bool = True
    truncated: bool = False
    # Cache-aware token telemetry (read-only; 0 when not applicable). The
    # client requests these via the Completion / SessionCompletion queries and
    # folds them into the trace's finite token breakdown. generated_tokens is
    # the REAL completion count; cached_prefix = KV reused (skipped prefill);
    # fresh_prefill = tokens actually prefilled; cache_hit = flow-cache HIT.
    prompt_tokens: int = 0
    cached_prefix_tokens: int = 0
    fresh_prefill_tokens: int = 0
    generated_tokens: int = 0
    cache_hit: bool = False
    flow_key: str = ""
    # Precise phase timing (server-measured): prefill = prompt eval, decode =
    # generation. Lets the client trace split inference time per call.
    prefill_ms: float = 0.0
    decode_ms: float = 0.0


@strawberry.type
class RawCompletionResponse:
    """Raw completion response — no delimiter stripping applied.

    Returns the full model output including channel markers, thinking
    text, and delimiter tokens. Used for training data collection
    and debugging delimiter parsing.
    """

    raw_text: str
    tokens_generated: int
    finished: bool = True


@strawberry.type
class CompletionChunk:
    """Streaming completion chunk."""

    text: str
    is_complete: bool


@strawberry.input
class CompletionRequest:
    """Input type for completion requests."""

    prompt: str
    max_tokens: Optional[int] = strawberry.field(default=None)
    temperature: Optional[float] = strawberry.field(default=None)
    grammar: Optional[str] = strawberry.field(default=None)
    # Per-flow KV cache (opt-in: config.model.flow_kv_cache). static_prefix is
    # the invariant head that leads `prompt`; flow_cache_key keys its pinned KV.
    static_prefix: Optional[str] = strawberry.field(default=None)
    flow_cache_key: Optional[str] = strawberry.field(default=None)
    # Per-request reasoning HEAD-SWAP level (low/medium/high) for STATELESS
    # completions — the pinned level head replaces the static head for this
    # request only (config.model.reasoning_head_swap; resident path).
    # None -> default level. gpt-oss/harmony.
    reasoning: Optional[str] = strawberry.field(default=None)
    # Registry model name to serve this request. None/active-local -> the
    # resident model as always. A REMOTE entry (provider yaml) -> its
    # adapter. An inactive local config -> error (swapModel first).
    model: Optional[str] = strawberry.field(default=None)


@strawberry.type
class ToolInfo:
    """Metadata about a registered tool."""

    name: str
    description: str
    parameters_schema: str  # JSON string of the parameter schema


@strawberry.type
class ToolResult:
    """Result from executing a tool."""

    tool_name: str
    result: str  # JSON string of the tool output


# ── Session types ─────────────────────────────────────────────────


@strawberry.type
class SessionInfoGQL:
    """Returned when a memoryful session is created."""

    session_id: str
    instance_index: int
    ttl_seconds: int


@strawberry.input
class SessionConfig:
    """Configuration for a new memoryful session."""

    ttl_seconds: Optional[int] = strawberry.field(default=300)
    # Resident session flow-fork (model.resident_session_flow_fork): pin an invariant
    # per-flow preamble (static_prefix) above the global static and fork it onto the
    # live seq at turn 0, keyed by flow_cache_key. Ignored unless the flag is on.
    flow_cache_key: Optional[str] = strawberry.field(default=None)
    static_prefix: Optional[str] = strawberry.field(default=None)
    # Fork this session from a pinned semi-permanent snapshot (sessionSnapshot
    # mutation): hot = ~zero prefill; cold/replay = re-prefill from the stored
    # token stream. Unknown key errors before any state is touched.
    from_snapshot: Optional[str] = strawberry.field(default=None)
    # Multi-persona pooling: lease the pool slot carrying this persona's SOUL
    # (e.g. "user_sim" for the simulated-user participant). Session-scoped —
    # set at start, never per-turn. None → the default persona's slot.
    persona: Optional[str] = strawberry.field(default=None)


@strawberry.type
class SnapshotInfoGQL:
    """A pinned semi-permanent session snapshot."""

    key: str
    tokens: int
    resident: bool
    turn_count: int
    created_at: float


@strawberry.input
class SessionTurnRequest:
    """Input for a turn within a memoryful session."""

    session_id: str
    prompt: str
    max_tokens: Optional[int] = strawberry.field(default=None)
    temperature: Optional[float] = strawberry.field(default=None)
    grammar: Optional[str] = strawberry.field(default=None)
    # Per-request reasoning HEAD-SWAP level (low/medium/high). At turn 0 of a
    # non-flow session it forks the level's pinned system head onto the live seq
    # (config.model.reasoning_head_swap). None → default level. gpt-oss/harmony.
    reasoning: Optional[str] = strawberry.field(default=None)


@strawberry.type
class SessionEventGQL:
    """Push notification for session lifecycle events."""

    session_id: str
    event_type: str  # "expired" | "error"
    message: str


# Global session manager — initialized at startup
_session_manager: Optional[SessionManager] = None


@strawberry.type
class ModelInfoGQL:
    """One registry entry: local (swappable) or remote (always available)."""

    name: str
    family: str
    model_path: str
    gguf_size_gb: float
    weights_present: bool
    active: bool
    provider: str = "local_llama"
    error: Optional[str] = None


@strawberry.type
class SwapModelResult:
    """Outcome of a swapModel mutation (see core/model_swap.py)."""

    ok: bool
    name: str
    previous: str
    noop: bool = False
    rolled_back: bool = False
    drain_forced: bool = False
    expired_sessions: int = 0
    evicted_streams: int = 0
    teardown_ms: float = 0.0
    load_ms: float = 0.0
    total_ms: float = 0.0
    error: Optional[str] = None


def _get_session_manager() -> SessionManager:
    if _session_manager is None:
        raise RuntimeError("Session manager not initialized")
    return _session_manager


async def _serve_remote_if_routed(
    request: CompletionRequest,
) -> Optional[CompletionResponse]:
    """Serve via a remote provider when request.model names a remote
    registry entry; None means "serve locally as always". Raises on an
    inactive local config or an unknown name (see core/remote_router)."""
    from core.remote_router import remote_completion, resolve_route

    if resolve_route(request.model) is None:
        return None
    r = await remote_completion(
        request.model,
        request.prompt,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
    )
    return CompletionResponse(
        text=r.text,
        tokens_generated=r.output_tokens,
        finished=True,
        prompt_tokens=r.input_tokens,
        fresh_prefill_tokens=r.input_tokens,
        generated_tokens=r.output_tokens,
    )


# --------------------------------------------------------------------
# 2️⃣ GraphQL Resolvers
# --------------------------------------------------------------------


@strawberry.type
class Query:
    """GraphQL Query resolvers."""

    @strawberry.field
    def health(self) -> HealthStatus:
        """Health check endpoint.

        Includes generation progress information for health polling.
        Clients can monitor `tokens_generated` and `seconds_since_last_token`
        to distinguish active generation from stuck instances.
        """
        from core.generation_tracker import get_tracker

        status = get_health_status()
        tracker = get_tracker()
        tracker_status = tracker.get_status()
        trend_status = tracker.get_trend()

        return HealthStatus(
            status=status["status"],
            pool_size=status["pool_size"],
            available_instances=status["available_instances"],
            active_instances=status.get("active_instances", status["pool_size"]),
            in_flight=status.get("in_flight", 0),
            jit_enabled=status.get("jit_enabled", False),
            generation_active=tracker_status.get("generation_active", False),
            tokens_generated=tracker_status.get("tokens_generated", 0),
            elapsed_seconds=tracker_status.get("elapsed_seconds"),
            seconds_since_last_token=tracker_status.get("seconds_since_last_token"),
            generation_phase=tracker_status.get("phase", "idle"),
            prompt_tokens=tracker_status.get("prompt_tokens", 0),
            eval_duration=tracker_status.get("eval_duration"),
            expected_eval_seconds=tracker_status.get("expected_eval_seconds"),
            mem_process_rss_mb=status.get("mem_process_rss_mb"),
            mem_system_used_percent=status.get("mem_system_used_percent"),
            mem_system_available_mb=status.get("mem_system_available_mb"),
            mem_system_wired_mb=status.get("mem_system_wired_mb"),
            flow_cache_entries=status.get("flow_cache_entries", 0),
            resident_active=status.get("resident_active", False),
            flow_builds=status.get("flow_builds", 0),
            flow_hits=status.get("flow_hits", 0),
            flow_evicts=status.get("flow_evicts", 0),
            flow_fallbacks=status.get("flow_fallbacks", 0),
            runaway_captures=status.get("runaway_captures", 0),
            context_refreshes=status.get("context_refreshes", 0),
            requests_since_refresh=status.get("requests_since_refresh", 0),
            kv_pool_tokens=status.get("kv_pool_tokens"),
            model_max_context=status.get("model_max_context"),
            decode_mode=status.get("decode_mode", ""),
            checked_out=status.get("checked_out", 0),
            engine_active_streams=(status.get("batched_engine") or {}).get(
                "active_streams"
            ),
            **{
                k: trend_status.get(k)
                for k in (
                    "trend_samples",
                    "decode_tps_recent",
                    "prefill_tps_recent",
                    "ttft_recent_s",
                    "decode_tps_baseline",
                    "throughput_drift",
                )
            },
        )

    @strawberry.field
    def thinking(self, request_id: Optional[str] = None) -> ThinkingResponse:
        """Access chain-of-thought content from a thinking model.

        Available when the model family uses thinking (channel or inline-tag).
        Returns the thinking content captured during generation, separate
        from the response body.

        Args:
            request_id: Optional correlation ID. If omitted, returns
                        thinking from the current or most recent generation.

        Returns:
            ThinkingResponse with the captured thinking content.
        """
        from core.generation_tracker import get_tracker

        data = get_tracker().get_thinking(request_id or "")
        return ThinkingResponse(
            request_id=data["request_id"],
            content=data["content"],
            complete=data["complete"],
            active=data["active"],
        )

    @strawberry.field
    async def session_completion(
        self, request: SessionTurnRequest
    ) -> CompletionResponse:
        """Non-streaming session turn — returns full response.

        The pinned instance retains KV cache state between turns.
        Each turn's prompt contains only NEW content — the model
        remembers prior turns via the preserved KV cache.

        ``max_tokens`` resolution mirrors the completion path (see
        core/inference.py):
          1. caller's explicit override, else
          2. ``config.generation.max_tokens_default``, else
          3. hard floor of 256.
        Previously this was hardcoded ``request.max_tokens or 256``,
        which silently capped session turns at 256 regardless of the
        configured default — causing reasoning-heavy turns to be cut
        off mid-analysis on Harmony-channel families (the model never
        transitioned to the final channel, so the FSM stripped the
        whole response and returned empty). Keep this in sync with
        session_turn below and with core/inference.py's fallback chain.
        """
        mgr = _get_session_manager()
        max_tokens = resolve_max_tokens(request.max_tokens)
        temperature = request.temperature or 0.7
        text, tokens, cache = await mgr.session_turn_complete(
            session_id=request.session_id,
            prompt=request.prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            grammar=request.grammar,
            reasoning=request.reasoning,
        )
        # Budget-derived OR engine-cut — see the completion path; a KV-pressure
        # force-window stops below the budget and the derivation misses it.
        truncated = tokens >= max_tokens or (
            cache.get("end_reason") in ("kv_pressure_truncated", "length")
        )
        return CompletionResponse(
            text=text,
            tokens_generated=tokens,
            finished=True,
            truncated=truncated,
            prompt_tokens=cache.get("prompt_tokens", 0),
            cached_prefix_tokens=cache.get("cached_prefix_tokens", 0),
            fresh_prefill_tokens=cache.get("fresh_prefill_tokens", 0),
            generated_tokens=cache.get("generated_tokens", 0),
            cache_hit=cache.get("cache_hit", False),
            flow_key=cache.get("flow_key", ""),
            prefill_ms=cache.get("prefill_ms", 0.0),
            decode_ms=cache.get("decode_ms", 0.0),
        )

    @strawberry.field
    async def completion(
        self,
        request: CompletionRequest,
        use_tools: bool = False,
    ) -> CompletionResponse:
        """
        Non-streaming completion query.

        Args:
            request: Completion request parameters
            use_tools: Enable tool-augmented inference (model can call tools)

        Returns:
            CompletionResponse with generated text.
            ``truncated`` is set when ``tokens_generated`` meets or exceeds
            the effective ``max_tokens`` the request resolved to (see
            core/inference.py:run_completion for the fallback chain).
            We recompute the effective value here to derive the flag —
            ``run_completion`` itself stays free of API-shape concerns.
        """
        remote = await _serve_remote_if_routed(request)
        if remote is not None:
            return remote
        run_fn = run_tool_completion if use_tools else run_completion
        effective_max = resolve_max_tokens(request.max_tokens)
        # Flow-cache fields only apply to the plain completion path.
        extra = (
            {}
            if use_tools
            else {
                "static_prefix": request.static_prefix,
                "flow_key": request.flow_cache_key,
                "reasoning": request.reasoning,
            }
        )
        outcome = await run_fn(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            grammar=request.grammar,
            **extra,
        )
        return CompletionResponse(
            text=outcome.text,
            tokens_generated=outcome.tokens_generated,
            finished=True,
            # Budget-derived OR engine-cut. The derivation alone cannot see a
            # KV-pressure force-window, which stops BELOW the budget — without
            # the second term a severed response reads as complete.
            truncated=(
                outcome.tokens_generated >= effective_max
                or outcome.truncated_by_engine
            ),
            prompt_tokens=outcome.prompt_tokens,
            cached_prefix_tokens=outcome.cached_prefix_tokens,
            fresh_prefill_tokens=outcome.fresh_prefill_tokens,
            generated_tokens=outcome.generated_tokens,
            cache_hit=outcome.cache_hit,
            flow_key=outcome.flow_key,
            prefill_ms=outcome.prefill_ms,
            decode_ms=outcome.decode_ms,
        )

    @strawberry.field
    async def raw_completion(
        self,
        request: CompletionRequest,
    ) -> RawCompletionResponse:
        """
        Raw completion query — returns unprocessed model output.

        Skips delimiter stripping entirely. The returned raw_text
        includes channel markers, thinking content, delimiter tokens,
        and any other structural tokens the model emitted.

        Used for:
          - Training data collection (--collect-training)
          - Debugging delimiter parsing issues
          - Inspecting actual model output structure

        Args:
            request: Completion request parameters

        Returns:
            RawCompletionResponse with unprocessed model output
        """
        raw_text, tokens_generated = await run_raw_completion(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            grammar=request.grammar,
            # Was silently DROPPED until 2026-07-26. The request type
            # advertises `reasoning`, so callers reasonably believed a level
            # was being applied; the raw path ignored it and returned
            # default-head output. Level A/Bs driven through rawCompletion
            # were comparing identical generations.
            reasoning=request.reasoning,
        )
        return RawCompletionResponse(
            raw_text=raw_text,
            tokens_generated=tokens_generated,
            # `finished` was hardcoded True, which made it worse than useless:
            # consumers used it as a truncation flag (a generation that hit the
            # token budget must read False), and a constant True reports "never
            # truncated" for every request ever made. Derive it.
            finished=tokens_generated < resolve_max_tokens(request.max_tokens),
        )

    @strawberry.field
    def tools(self) -> List[ToolInfo]:
        """List all registered tools with their metadata."""
        from tools.registry import get_registry

        registry = get_registry()
        return [
            ToolInfo(
                name=t.name,
                description=t.description,
                parameters_schema=json.dumps(t.parameters),
            )
            for t in registry.list_tools()
        ]

    @strawberry.field
    def snapshots(self) -> List[SnapshotInfoGQL]:
        """Pinned semi-permanent session snapshots (telemetry/harness view)."""
        mgr = _get_session_manager()
        return [
            SnapshotInfoGQL(
                key=s["key"],
                tokens=s["tokens"],
                resident=s["resident"],
                turn_count=s["turn_count"],
                created_at=s["created_at"],
            )
            for s in mgr.list_snapshots()
        ]

    @strawberry.field
    def tool_result(self, name: str, params: str) -> ToolResult:
        """
        Execute a registered tool by name.

        Args:
            name: Tool name (e.g. "keal_damage_bonus")
            params: JSON string of tool parameters

        Returns:
            ToolResult with the tool output
        """
        from tools.registry import get_registry

        registry = get_registry()
        try:
            parsed_params = json.loads(params)
        except json.JSONDecodeError as exc:
            return ToolResult(
                tool_name=name,
                result=json.dumps({"error": f"Invalid JSON params: {exc}"}),
            )
        try:
            result = registry.execute(name, parsed_params)
        except KeyError:
            result = json.dumps(
                {"error": f"Unknown tool: {name!r}", "available": registry.tool_names()}
            )
        return ToolResult(tool_name=name, result=result)

    @strawberry.field
    def models(self) -> List[ModelInfoGQL]:
        """Swappable model configs (the registry catalog) with the active
        one flagged. Targets for the swapModel mutation."""
        from core import model_registry

        return [
            ModelInfoGQL(
                name=e.name,
                family=e.family,
                model_path=e.model_path,
                gguf_size_gb=e.gguf_size_gb,
                weights_present=e.weights_present,
                active=e.active,
                provider=e.provider,
                error=e.error,
            )
            for e in model_registry.list_models()
        ]


@strawberry.type
class RefreshContextResult:
    """Result of an in-process llama.cpp context refresh."""

    status: str
    refreshed: int
    reason: str
    elapsed_s: Optional[float] = None
    total_refreshes: int = 0


@strawberry.type
class Mutation:
    """GraphQL Mutation resolvers."""

    @strawberry.mutation
    async def create_completion(self, request: CompletionRequest) -> CompletionResponse:
        """
        Non-streaming completion mutation.

        Args:
            request: Completion request parameters

        Returns:
            CompletionResponse with generated text
        """
        remote = await _serve_remote_if_routed(request)
        if remote is not None:
            return remote
        outcome = await run_completion(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            grammar=request.grammar,
        )
        return CompletionResponse(
            text=outcome.text,
            tokens_generated=outcome.tokens_generated,
            finished=True,
            prompt_tokens=outcome.prompt_tokens,
            cached_prefix_tokens=outcome.cached_prefix_tokens,
            fresh_prefill_tokens=outcome.fresh_prefill_tokens,
            generated_tokens=outcome.generated_tokens,
            cache_hit=outcome.cache_hit,
            flow_key=outcome.flow_key,
            prefill_ms=outcome.prefill_ms,
            decode_ms=outcome.decode_ms,
        )

    @strawberry.mutation
    async def start_session(
        self, config: Optional[SessionConfig] = None
    ) -> SessionInfoGQL:
        """Acquire a pool instance and pin it for memoryful inference."""
        from core.model_swap import ModelSwapInProgress, swap_in_progress

        state = swap_in_progress()
        if state is not None:
            # New sessions mid-swap would prolong the drain window forever;
            # reject retriably (existing sessions get the finish window).
            raise ModelSwapInProgress(
                f"model swap in progress ({state}) — retry shortly"
            )
        mgr = _get_session_manager()
        ttl = config.ttl_seconds if config and config.ttl_seconds else 300
        info = await mgr.start_session(
            ttl_seconds=ttl,
            flow_key=config.flow_cache_key if config else None,
            static_prefix=config.static_prefix if config else None,
            from_snapshot=config.from_snapshot if config else None,
            persona=config.persona if config else None,
        )
        return SessionInfoGQL(
            session_id=info.session_id,
            instance_index=info.instance_index,
            ttl_seconds=info.ttl_seconds,
        )

    @strawberry.mutation
    async def end_session(self, session_id: str) -> bool:
        """Release the pinned instance and clear session state."""
        mgr = _get_session_manager()
        return await mgr.end_session(session_id)

    @strawberry.mutation
    async def session_snapshot(self, session_id: str, key: str) -> SnapshotInfoGQL:
        """Pin the session's current context as a semi-permanent snapshot.

        Survives session end/TTL; freed only by purgeSnapshot. Later sessions
        fork from it via SessionConfig.fromSnapshot — pay the long-context
        prefill once, branch many passes."""
        mgr = _get_session_manager()
        info = await mgr.session_snapshot(session_id, key)
        return SnapshotInfoGQL(
            key=info["key"],
            tokens=info["tokens"],
            resident=info["resident"],
            turn_count=info["turn_count"],
            created_at=time.time(),
        )

    @strawberry.mutation
    async def purge_snapshot(self, key: str) -> bool:
        """Free a pinned semi-permanent snapshot (the explicit release)."""
        mgr = _get_session_manager()
        return await mgr.purge_snapshot(key)

    @strawberry.mutation
    async def refresh_context(self, reason: str = "manual") -> RefreshContextResult:
        """Drop + rebuild the inference context in-process (weights kept) to clear the
        LLMVP-process-level output rot ("souring") without a process restart/reboot."""
        from core.inference import refresh_context as _refresh

        r = await _refresh(reason)
        return RefreshContextResult(
            status=r.get("status", "unknown"),
            refreshed=r.get("refreshed", 0),
            reason=r.get("reason", reason),
            elapsed_s=r.get("elapsed_s"),
            total_refreshes=r.get("total_refreshes", 0),
        )

    @strawberry.mutation
    async def swap_model(self, name: str, drain_s: float = 60.0) -> SwapModelResult:
        """Hotswap the served model to the named registry config, in-process.

        Drains in-flight work, tears down the backend + session manager,
        and re-runs the canonical startup path against the new config.
        Requests arriving mid-swap get a retriable error. See
        core/model_swap.py for the full lifecycle contract.
        """
        global _session_manager
        from core import model_swap as _swap
        from inference.backends.factory import get_backend

        mgr = _session_manager
        _session_manager = None  # detached; swap_model drains + shuts it down
        try:
            r = await _swap.swap_model(name, drain_s=drain_s, session_manager=mgr)
        except Exception:
            # Raised = nothing was torn down (bad name/config, concurrent
            # swap) — the detached manager is still fully valid.
            _session_manager = mgr
            raise

        if r.get("noop"):
            _session_manager = mgr
        else:
            backend = get_backend()
            if backend is not None:
                _session_manager = SessionManager(backend)
                log.info("📌 Session manager rebuilt after model swap")

        return SwapModelResult(
            ok=r.get("ok", False),
            name=r.get("name", name),
            previous=r.get("previous", ""),
            noop=r.get("noop", False),
            rolled_back=r.get("rolled_back", False),
            drain_forced=r.get("forced", False),
            expired_sessions=r.get("expired_sessions", 0),
            evicted_streams=r.get("evicted_streams", 0),
            teardown_ms=r.get("teardown_ms", 0.0),
            load_ms=r.get("load_ms", 0.0),
            total_ms=r.get("total_ms", 0.0),
            error=r.get("error"),
        )


@strawberry.type
class Subscription:
    """GraphQL Subscription resolvers for streaming."""

    @strawberry.subscription
    async def stream_completion(
        self,
        request: CompletionRequest,
        use_tools: bool = False,
    ) -> AsyncGenerator[CompletionChunk, None]:
        """
        Streaming completion subscription.

        Yields tokens as they are generated by the model.

        Args:
            request: Completion request parameters
            use_tools: Enable tool-augmented inference

        Yields:
            CompletionChunk with text and completion status
        """
        stream_fn = stream_tool_completion if use_tools else stream_completion
        async for text, is_complete in stream_fn(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            grammar=request.grammar,
        ):
            yield CompletionChunk(text=text, is_complete=is_complete)

    @strawberry.subscription
    async def session_turn(
        self, request: SessionTurnRequest
    ) -> AsyncGenerator[CompletionChunk, None]:
        """Streaming completion within a memoryful session.

        The pinned instance retains KV cache state between turns.
        Each turn's prompt contains only NEW content — the model
        remembers prior turns via the preserved KV cache.

        See session_completion above for ``max_tokens`` resolution
        rationale — the same chain applies here (explicit override →
        config default → hard floor of 256). The streaming path does
        not return a truncation flag (the response is chunked; clients
        can measure their own received token count against max_tokens
        if they care).
        """
        mgr = _get_session_manager()
        max_tokens = resolve_max_tokens(request.max_tokens)
        temperature = request.temperature or 0.7
        async for chunk in mgr.session_turn(
            session_id=request.session_id,
            prompt=request.prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            grammar=request.grammar,
            # Parity with the non-streaming session_completion query — the
            # streaming path previously dropped the reasoning HEAD-SWAP level.
            reasoning=request.reasoning,
        ):
            yield CompletionChunk(text=chunk, is_complete=False)
        yield CompletionChunk(text="", is_complete=True)

    @strawberry.subscription
    async def session_events(
        self, session_id: str
    ) -> AsyncGenerator[SessionEventGQL, None]:
        """Push notifications for session lifecycle events.

        Fires when a session expires due to TTL timeout.
        """
        mgr = _get_session_manager()
        queue = await mgr.register_listener(session_id)
        if queue is None:
            yield SessionEventGQL(
                session_id=session_id,
                event_type="error",
                message="Session not found",
            )
            return

        while True:
            event = await queue.get()
            yield SessionEventGQL(
                session_id=event.session_id,
                event_type=event.event_type,
                message=event.message,
            )
            if event.event_type in ("expired", "closed", "error"):
                # Terminal events end the subscription — without this the
                # task parks on queue.get() for the websocket's lifetime
                # after a normal endSession (memory audit).
                return


# --------------------------------------------------------------------
# 3️⃣ Create Schema and FastAPI App
# --------------------------------------------------------------------

graphql_security = config.graphql
extensions = [
    QueryDepthLimiter(max_depth=graphql_security.max_query_depth),
    MaxTokensLimiter(max_token_count=graphql_security.max_tokens),
    MaxAliasesLimiter(max_alias_count=graphql_security.max_aliases),
]

if not graphql_security.introspection_enabled:
    extensions.append(DisableIntrospection())

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    subscription=Subscription,
    extensions=extensions,
)

# Create FastAPI app with GraphQL router
app = FastAPI(
    title="LLMvp GraphQL API - Concurrent local LLM server with static knowledge base"
)

# Configure CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

graphql_router = GraphQLRouter(
    schema,
    path="/graphql",
    graphql_ide="apollo-sandbox",  # Modern GraphQL IDE
    subscription_protocols=[GRAPHQL_TRANSPORT_WS_PROTOCOL],
)

app.include_router(graphql_router)

# Conditionally include OpenAI compatibility shim
if config.app.openai_shim:
    from .rest_api import router as openai_router

    app.include_router(openai_router, prefix="/v1")
    log.info("🔌 OpenAI compatibility shim enabled at /v1")


# --------------------------------------------------------------------
# 4️⃣ Application Startup/Shutdown
# --------------------------------------------------------------------


@app.on_event("startup")
async def startup_event() -> None:
    """Initialize resources on application startup.

    This is ``async def`` so that Starlette **awaits** it — the server
    will not start accepting HTTP connections until the backend pool is
    fully warmed up and the readiness gate is open.
    """
    await initialize_server_async()

    # Initialize session manager for memoryful inference
    global _session_manager
    from inference.backends.factory import get_backend

    backend = get_backend()
    if backend is not None:
        _session_manager = SessionManager(backend)
        log.info("📌 Session manager initialized")

    # Load tool modules — each auto-registers with the global ToolRegistry
    import tools.archetypal_interactions  # noqa: F401
    import tools.card_lookup  # noqa: F401

    from tools.registry import get_registry

    names = get_registry().tool_names()
    if names:
        log.info("🔧 Tools loaded: %s", ", ".join(names))

    log.info("🚀 GraphQL API started successfully")
    log.info(f"📊 GraphQL endpoint: http://{config.app.host}:{config.app.port}/graphql")
    if config.app.openai_shim:
        log.info(
            f"🔌 OpenAI shim endpoint: http://{config.app.host}:{config.app.port}/v1/completions"
        )


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """Clean up resources on application shutdown.

    ``async def`` so that Starlette **awaits** the full cleanup —
    all C-level resources (contexts, model weights) are freed before
    the process exits.
    """
    global _session_manager
    if _session_manager is not None:
        await _session_manager.shutdown()
        _session_manager = None
        log.info("📌 Session manager shutdown complete")

    await shutdown_server_async()
    log.info("🗑️ GraphQL API shutdown complete")
