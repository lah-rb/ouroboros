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
from typing import Optional

# Local imports
from core.config import get_config
from inference.backends.factory import (
    initialize_backend_async,
    shutdown_backend_async,
    initialize_backend,
    shutdown_backend,
    get_backend,
)
from preprocessing.static_tokens import manager as static_tokens_manager

log = logging.getLogger("llm-mvp")


async def initialize_server_async(skip_token_load: bool = False) -> None:
    """
    Initialize server resources on startup (async version).

    This coroutine **awaits** backend initialization so the pool
    is fully warmed up before the function returns — no requests
    can sneak through before the backend is ready.

    Args:
        skip_token_load: If True, skip loading static tokens (for testing)

    Raises:
        RuntimeError: If initialization fails
    """
    try:
        if not skip_token_load:
            try:
                static_tokens_manager.load_static_buffer()
                tokens = static_tokens_manager.get_static_tokens()
                log.info(f"✅ Loaded {len(tokens)} static tokens")
            except Exception as exc:
                log.warning(f"⚠️ Could not load static tokens: {exc}")
                log.info("📝 Running in lightweight mode without static knowledge")

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


def initialize_server(skip_token_load: bool = False) -> None:
    """
    Initialize server resources on startup (sync wrapper).

    Only safe to call when **no** event loop is running (e.g. from
    a CLI script or a test).  Under uvicorn, use
    ``initialize_server_async`` directly.

    Args:
        skip_token_load: If True, skip loading static tokens (for testing)

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
        loop.run_until_complete(initialize_server_async(skip_token_load))
    except RuntimeError as exc:
        if "no current event loop" in str(exc).lower():
            asyncio.run(initialize_server_async(skip_token_load))
        else:
            raise


async def shutdown_server_async(skip_token_load: bool = False) -> None:
    """
    Clean up server resources on shutdown (async version).

    Awaits backend shutdown so all C-level resources are freed
    before the function returns.

    Args:
        skip_token_load: If True, skip cleaning up static tokens
    """
    try:
        # Shutdown backend — awaits until cleanup is complete.
        await shutdown_backend_async()
        log.info("✅ Backend shutdown complete")

        if not skip_token_load:
            static_tokens_manager.cleanup()

        log.info("🗑️ Server shutdown complete")

    except Exception as exc:
        log.error(f"⚠️ Error during shutdown: {exc}")


def shutdown_server(skip_token_load: bool = False) -> None:
    """
    Clean up server resources on shutdown (sync wrapper).

    Only safe to call when **no** event loop is running.

    Args:
        skip_token_load: If True, skip cleaning up static tokens
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError(
                "shutdown_server() cannot be used when the event loop "
                "is already running — use shutdown_server_async() instead"
            )
        loop.run_until_complete(shutdown_server_async(skip_token_load))
    except RuntimeError as exc:
        if "no current event loop" in str(exc).lower():
            asyncio.run(shutdown_server_async(skip_token_load))
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
    config = get_config()
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
