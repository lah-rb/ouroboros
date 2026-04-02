#!/usr/bin/env python3
"""
Backend Factory

Creates and manages the llama.cpp backend instance.
Simplified to llama.cpp only for reliability.

The ``initialize_backend_async`` coroutine is the canonical entry
point.  It **awaits** ``backend.initialize()`` and only sets the
global ``_backend_instance`` once the pool is fully ready, closing
the fire-and-forget gap that previously let requests arrive before
any inference instance existed.
"""

import asyncio
import logging
from typing import Any, Optional

from .base import BaseBackend
from .llama_cpp_backend import LlamaCppBackend

log = logging.getLogger("llm-mvp")

# Global backend instance
_backend_instance: Optional[LlamaCppBackend] = None


def create_backend(config: Any) -> LlamaCppBackend:
    """
    Create a llama.cpp backend instance.

    Args:
        config: The application configuration

    Returns:
        Configured LlamaCppBackend instance
    """
    log.info("🔧 Creating llama.cpp backend")
    return LlamaCppBackend(config)


def get_backend() -> Optional[LlamaCppBackend]:
    """
    Get the global backend instance.

    Returns:
        The current backend instance, or None if not initialized
    """
    return _backend_instance


def set_backend(backend: LlamaCppBackend) -> None:
    """
    Set the global backend instance.

    Args:
        backend: The backend instance to set as global
    """
    global _backend_instance
    _backend_instance = backend
    log.info(f"✅ Global backend set to: {backend.backend_name}")


async def initialize_backend_async(config: Any) -> LlamaCppBackend:
    """
    Initialize and set the global backend (async).

    This is the preferred entry point.  It **awaits**
    ``backend.initialize()`` so the pool is fully warmed up
    before the global reference is published and the server
    starts accepting traffic.

    Args:
        config: The application configuration

    Returns:
        The initialized backend instance
    """
    global _backend_instance

    # If backend already initialized, reuse it
    if _backend_instance is not None:
        log.info("✅ Backend already initialized, reusing existing instance")
        return _backend_instance

    # Create and initialize new backend
    backend = create_backend(config)

    # Await initialization — pool is fully ready when this returns.
    await backend.initialize()

    # Only publish the global reference *after* init succeeds.
    _backend_instance = backend
    return backend


def initialize_backend(config: Any) -> LlamaCppBackend:
    """
    Initialize and set the global backend (sync wrapper).

    Prefer ``initialize_backend_async`` from async contexts.
    This wrapper exists for the rare case where a sync caller
    (e.g. a CLI script) needs to bootstrap the backend.

    Args:
        config: The application configuration

    Returns:
        The initialized backend instance
    """
    global _backend_instance

    # If backend already initialized, reuse it
    if _backend_instance is not None:
        log.info("✅ Backend already initialized, reusing existing instance")
        return _backend_instance

    # Create and initialize new backend
    backend = create_backend(config)

    # Run initialization synchronously
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            raise RuntimeError(
                "initialize_backend() cannot be used when the event loop "
                "is already running — use initialize_backend_async() instead"
            )
        loop.run_until_complete(backend.initialize())
    except RuntimeError as exc:
        if "no current event loop" in str(exc).lower():
            asyncio.run(backend.initialize())
        else:
            raise

    _backend_instance = backend
    return backend


async def shutdown_backend_async() -> None:
    """
    Shutdown the global backend (async).

    Awaits the backend's ``shutdown()`` coroutine so that all
    C-level resources are freed before the function returns.
    """
    global _backend_instance

    if _backend_instance is not None:
        log.info(f"🧹 Shutting down backend: {_backend_instance.backend_name}")
        try:
            await _backend_instance.shutdown()
        except Exception as exc:
            log.error(f"❌ Error during backend shutdown: {exc}")
        finally:
            _backend_instance = None


def shutdown_backend() -> None:
    """
    Shutdown the global backend (sync wrapper).

    Prefer ``shutdown_backend_async`` from async contexts.
    """
    global _backend_instance

    if _backend_instance is not None:
        log.info(f"🧹 Shutting down backend: {_backend_instance.backend_name}")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                raise RuntimeError(
                    "shutdown_backend() cannot be used when the event loop "
                    "is already running — use shutdown_backend_async() instead"
                )
            loop.run_until_complete(_backend_instance.shutdown())
        except RuntimeError as exc:
            if "no current event loop" in str(exc).lower():
                asyncio.run(_backend_instance.shutdown())
            else:
                log.error(f"❌ Error during backend shutdown: {exc}")
        except Exception as exc:
            log.error(f"❌ Error during backend shutdown: {exc}")
        finally:
            _backend_instance = None
