#!/usr/bin/env python3
"""
GraphQL API Module

Strawberry GraphQL application for the LLMvp server.
Provides queries, mutations, and subscriptions for LLM inference.
Uses shared inference logic from core.inference.
"""

import json
import logging
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
    SessionInfo as _SessionInfo,
    SessionEvent as _SessionEvent,
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
    """Non-streaming completion response."""

    text: str
    tokens_generated: int
    finished: bool = True


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


@strawberry.input
class SessionTurnRequest:
    """Input for a turn within a memoryful session."""

    session_id: str
    prompt: str
    max_tokens: Optional[int] = strawberry.field(default=None)
    temperature: Optional[float] = strawberry.field(default=None)
    grammar: Optional[str] = strawberry.field(default=None)


@strawberry.type
class SessionEventGQL:
    """Push notification for session lifecycle events."""

    session_id: str
    event_type: str  # "expired" | "error"
    message: str


# Global session manager — initialized at startup
_session_manager: Optional[SessionManager] = None


def _get_session_manager() -> SessionManager:
    if _session_manager is None:
        raise RuntimeError("Session manager not initialized")
    return _session_manager


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
        tracker_status = get_tracker().get_status()

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
        """
        mgr = _get_session_manager()
        max_tokens = request.max_tokens or 256
        temperature = request.temperature or 0.7
        text, tokens = await mgr.session_turn_complete(
            session_id=request.session_id,
            prompt=request.prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            grammar=request.grammar,
        )
        return CompletionResponse(text=text, tokens_generated=tokens, finished=True)

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
            CompletionResponse with generated text
        """
        run_fn = run_tool_completion if use_tools else run_completion
        answer, tokens_generated = await run_fn(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            grammar=request.grammar,
        )
        return CompletionResponse(
            text=answer,
            tokens_generated=tokens_generated,
            finished=True,
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
        )
        return RawCompletionResponse(
            raw_text=raw_text,
            tokens_generated=tokens_generated,
            finished=True,
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
        answer, tokens_generated = await run_completion(
            prompt=request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            grammar=request.grammar,
        )
        return CompletionResponse(
            text=answer,
            tokens_generated=tokens_generated,
            finished=True,
        )

    @strawberry.mutation
    async def start_session(
        self, config: Optional[SessionConfig] = None
    ) -> SessionInfoGQL:
        """Acquire a pool instance and pin it for memoryful inference."""
        mgr = _get_session_manager()
        ttl = config.ttl_seconds if config and config.ttl_seconds else 300
        info = await mgr.start_session(ttl_seconds=ttl)
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
        """
        mgr = _get_session_manager()
        max_tokens = request.max_tokens or 256
        temperature = request.temperature or 0.7
        async for chunk in mgr.session_turn(
            session_id=request.session_id,
            prompt=request.prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            grammar=request.grammar,
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
            if event.event_type == "expired":
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
