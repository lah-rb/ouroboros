#!/usr/bin/env python3
"""
Shared Lifecycle Management

Provides common startup and shutdown logic for both REST and GraphQL APIs.
Eliminates duplication between api/rest_api.py and api/graphql_api.py.

Now uses the pluggable backend system.

All public entry points come in ``async`` / ``sync`` pairs.  The async
variants are canonical; the sync wrappers exist only for CLI or test
callers that are *not* inside a running event loop.  Under uvicorn the
``async`` versions are always used so that ``await`` propagates
correctly — no more ``asyncio.create_task`` fire-and-forget.
"""

import asyncio
import logging

# Local imports
from core.config import get_config
from inference.backends.factory import (
    initialize_backend_async,
    shutdown_backend_async,
    get_backend,
)
from preprocessing.static_tokens import manager as static_tokens_manager

log = logging.getLogger("llm-mvp")

# Module-level flag — set by the CLI before uvicorn spawns the app.
# The startup event reads this since uvicorn doesn't provide a way
# to pass parameters to ASGI app startup events.
_skip_knowledge: bool = False


def set_skip_knowledge(skip: bool) -> None:
    """Set the skip-knowledge flag (called from CLI before server start).

    When active, the server builds a minimal static token prefix
    containing only the format template wrapper (system role, identity,
    reasoning mode) but without SOUL.md persona or tools. This lets
    ``--collect-training`` capture raw model output without persona
    contamination while retaining correct format structure.
    """
    global _skip_knowledge
    _skip_knowledge = skip


def _build_bare_static_tokens() -> list[int]:
    """Build static tokens with format structure but no persona/tools.

    Returns token IDs for the system prompt wrapper only —
    identity, cutoff, reasoning mode, channel directives — without
    SOUL.md content or tool definitions.
    """
    from formats.registry import get_renderer
    from formats.renderer import join_segments
    from inference.tokenizer import create_tokenizer, tokenize_segments
    from inference.metadata import read_metadata, set_model_metadata, log_metadata

    config = get_config()
    renderer = get_renderer(config.model.family)

    # Render system block with empty persona and tools, as framing/content segments.
    bare_segments = renderer.render_system_segments(persona="", tools="")
    log.info(
        "📝 Building bare static prefix (format only, no knowledge): " "%d chars",
        len(join_segments(bare_segments)),
    )

    # BOS behavior from GGUF metadata — the model file is authoritative.
    tokenizer = create_tokenizer()
    metadata = read_metadata(tokenizer)
    set_model_metadata(metadata)
    log_metadata(metadata)

    needs_bos = metadata.add_bos
    token_ids = tokenize_segments(tokenizer, bare_segments, add_bos=needs_bos)
    return token_ids


async def initialize_server_async(skip_knowledge: bool | None = None) -> None:
    """
    Initialize server resources on startup (async version).

    This coroutine **awaits** backend initialization so the pool
    is fully warmed up before the function returns — no requests
    can sneak through before the backend is ready.

    Args:
        skip_knowledge: If True, build minimal static tokens without
                        SOUL.md persona or tools. Defaults to the
                        module-level flag set by ``set_skip_knowledge()``.

    Raises:
        RuntimeError: If initialization fails
    """
    try:
        # First line of the boot record, because it is the one that says WHICH
        # config this is. load_config runs at module import — before
        # logging.basicConfig — so its own emission is swallowed; this is where
        # it actually reaches the log an operator reads.
        from core.config import describe_resolution

        if (_inherit := describe_resolution()) is not None:
            log.info(_inherit)

        skip = skip_knowledge if skip_knowledge is not None else _skip_knowledge

        if skip:
            # Build minimal static tokens in-memory — format wrapper only
            # (metadata is read and stored inside _build_bare_static_tokens)
            try:
                bare_tokens = _build_bare_static_tokens()
                static_tokens_manager._static_tokens_list = bare_tokens
                log.info(
                    "✅ Loaded bare static prefix: %d tokens " "(no persona/tools)",
                    len(bare_tokens),
                )
            except Exception as exc:
                log.warning("⚠️ Could not build bare static prefix: %s", exc)
                log.info("📝 Running with empty static context")
        else:
            try:
                static_tokens_manager.load_static_buffer()
                tokens = static_tokens_manager.get_static_tokens()
                log.info(f"✅ Loaded {len(tokens)} static tokens")
            except Exception as exc:
                log.warning(f"⚠️ Could not load static tokens: {exc}")
                log.info("📝 Running in lightweight mode without static knowledge")

            # Read and store GGUF metadata for runtime use.
            # In the skip path this happens inside _build_bare_static_tokens.
            try:
                from inference.metadata import (
                    read_metadata,
                    set_model_metadata,
                    get_model_metadata,
                    log_metadata,
                    log_metadata_vs_config,
                )

                if get_model_metadata() is None:
                    from inference.tokenizer import create_tokenizer

                    tokenizer = create_tokenizer()
                    metadata = read_metadata(tokenizer)
                    set_model_metadata(metadata)
                    log_metadata(metadata)
                    log_metadata_vs_config(metadata)
            except Exception as exc:
                log.warning("⚠️ Could not read GGUF metadata: %s", exc)

        # Initialize backend — awaits until pool is fully ready.
        backend = await initialize_backend_async(get_config())
        log.info(f"✅ Backend initialized: {backend.backend_name}")

        # Log backend capabilities
        caps = backend.capabilities
        log.info(
            f"🔧 Backend capabilities: streaming={caps.streaming}, "
            f"batching={caps.batching}, async_api={caps.async_api}"
        )

        log.info("🚀 Server initialized successfully")

    except Exception as exc:
        log.error(f"❌ Startup failed: {exc}")
        raise


def initialize_server(skip_knowledge: bool = False) -> None:
    """
    Initialize server resources on startup (sync wrapper).

    Only safe to call when **no** event loop is running (e.g. from
    a CLI script or a test).  Under uvicorn, use
    ``initialize_server_async`` directly.

    Args:
        skip_knowledge: If True, build bare static tokens without persona/tools

    Raises:
        RuntimeError: If initialization fails
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError(
                "initialize_server() cannot be used when the event loop "
                "is already running — use initialize_server_async() instead"
            )
        loop.run_until_complete(initialize_server_async(skip_knowledge))
    except RuntimeError as exc:
        if "no current event loop" in str(exc).lower():
            asyncio.run(initialize_server_async(skip_knowledge))
        else:
            raise


async def shutdown_server_async(skip_knowledge: bool | None = None) -> None:
    """
    Clean up server resources on shutdown (async version).

    Awaits backend shutdown so all C-level resources are freed
    before the function returns.

    Args:
        skip_knowledge: If True, skip cleaning up static tokens.
                        Defaults to the module-level flag.
    """
    skip = skip_knowledge if skip_knowledge is not None else _skip_knowledge
    try:
        # Shutdown backend — awaits until cleanup is complete.
        await shutdown_backend_async()
        log.info("✅ Backend shutdown complete")

        if not skip:
            static_tokens_manager.cleanup()

        log.info("🗑️ Server shutdown complete")

    except Exception as exc:
        log.error(f"⚠️ Error during shutdown: {exc}")


def shutdown_server(skip_knowledge: bool = False) -> None:
    """
    Clean up server resources on shutdown (sync wrapper).

    Only safe to call when **no** event loop is running.

    Args:
        skip_knowledge: If True, skip cleaning up static tokens
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError(
                "shutdown_server() cannot be used when the event loop "
                "is already running — use shutdown_server_async() instead"
            )
        loop.run_until_complete(shutdown_server_async(skip_knowledge))
    except RuntimeError as exc:
        if "no current event loop" in str(exc).lower():
            asyncio.run(shutdown_server_async(skip_knowledge))
        else:
            raise


def get_server_info(api_type: str, host: str, port: int) -> dict:
    """
    Get server information for logging.

    Args:
        api_type: Type of API ('REST' or 'GraphQL')
        host: Server host
        port: Server port

    Returns:
        dict with server information
    """
    backend = get_backend()
    backend_name = backend.backend_name if backend else "unknown"

    return {
        "api_type": api_type,
        "host": host,
        "port": port,
        "endpoint": f"http://{host}:{port}",
        "graphql_endpoint": (
            f"http://{host}:{port}/graphql" if api_type == "GraphQL" else None
        ),
        "backend": backend_name,
    }
