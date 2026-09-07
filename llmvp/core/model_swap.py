"""Single-resident model hotswap orchestration (MULTI_MODEL_PLAN.md Phase 1).

A swap replaces the served model IN-PROCESS: drain in-flight work, tear
down the backend + model-bound globals, then run the CANONICAL startup
path (``initialize_server_async``) against the new config — so a swap and
a process restart are the same code path and cannot drift. P0.a validated
the foundation: mid-process teardown releases Metal wired memory exactly,
and page-cache-warm reloads take seconds.

Request handling during a swap: ``swap_in_progress()`` is checked by the
inference layer's ``_get_backend()``; new work is rejected with
``ModelSwapInProgress``, which reaches clients as a retriable GraphQL
error (agent dispatch retries unchanged — proven through KV-eviction and
full-restart windows).

Contract for callers (the GraphQL mutation): ``swap_model`` may RAISE only
before any teardown begins (unknown name, invalid config, concurrent
swap, no-op is a normal return). From teardown onward every failure is
reported in the returned dict (``ok``/``rolled_back``/``error``) — the
caller can therefore rebuild its session manager whenever ``ok`` or
``rolled_back`` is set, and keep its existing one when the call raises.

The model-bound globals reset here (the "swap ledger" — see the plan's
singleton table): backend, tokenizer cache, GGUF metadata, static-tokens
manager. The active Config global is replaced via ``set_config``; modules
that kept a live ``ActiveConfigView`` follow automatically.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional


from core import model_registry
from core.config import get_config, load_named_config, set_config
from inference.backends.factory import get_backend, shutdown_backend_async
from inference.metadata import reset_model_metadata
from inference.tokenizer import reset_tokenizer_cache
from preprocessing.static_tokens import manager as static_tokens_manager

log = logging.getLogger("llm-mvp")


class ModelSwapInProgress(RuntimeError):
    """A request arrived while the model is being swapped — retriable."""


_state: Optional[str] = None  # None | "draining" | "loading" | "rolling-back"
_lock = asyncio.Lock()

# Force-clear stragglers this long before the settle deadline gives up.
_FORCE_CLEAR_SETTLE_S = 30.0


def swap_in_progress() -> Optional[str]:
    """Current swap phase, or None when no swap is running."""
    return _state


def _reset_model_globals() -> None:
    """Reset every model-bound global except the backend itself."""
    reset_tokenizer_cache()
    reset_model_metadata()
    static_tokens_manager.cleanup()


def _busy_counts(backend: Any) -> tuple[int, int]:
    return (
        getattr(backend, "_checked_out", 0),
        getattr(backend, "_active_generations", 0),
    )


async def _drain(backend: Any, session_manager: Any, drain_s: float) -> Dict[str, Any]:
    """Give in-flight work a finish window, then force-clear stragglers.

    Mirrors the refresh drain's two phases: poll until the backend is
    quiet, and past the deadline expire sessions + evict batched streams
    (clients see the same retriable errors the KV-eviction incident
    proved they ride through)."""
    deadline = time.monotonic() + max(drain_s, 0.0)
    while time.monotonic() < deadline:
        checked_out, active = _busy_counts(backend)
        if checked_out == 0 and active == 0:
            return {"forced": False, "expired_sessions": 0, "evicted_streams": 0}
        await asyncio.sleep(1.0)

    expired = 0
    if session_manager is not None:
        try:
            expired = await session_manager.expire_all_sessions("model swap")
        except Exception:  # noqa: BLE001 — teardown proceeds regardless
            log.exception("⚠️ model swap: session force-expiry failed")

    evicted = 0
    engine = getattr(backend, "_engine", None)
    if engine is not None:
        try:
            evicted = engine.evict_all_streams("model swap")
        except Exception:  # noqa: BLE001 — teardown proceeds regardless
            log.exception("⚠️ model swap: stream eviction failed")

    settle_deadline = time.monotonic() + _FORCE_CLEAR_SETTLE_S
    while time.monotonic() < settle_deadline:
        checked_out, active = _busy_counts(backend)
        if checked_out == 0 and active == 0:
            break
        await asyncio.sleep(1.0)
    else:
        log.warning(
            "⚠️ model swap: backend still busy after force-clear "
            "(checked_out=%d, active=%d) — proceeding with teardown",
            *_busy_counts(backend),
        )
    return {"forced": True, "expired_sessions": expired, "evicted_streams": evicted}


async def swap_model(
    name: str,
    drain_s: float = 60.0,
    session_manager: Any = None,
) -> Dict[str, Any]:
    """Swap the served model to the config named ``name``.

    Args:
        name: Registry name (configs/{name}.yaml).
        drain_s: Finish window for in-flight work before force-clear.
        session_manager: The live SessionManager, if any — drained and
            shut down here; the caller rebuilds a fresh one afterwards.

    Returns:
        Result dict: ok, name, previous, noop, rolled_back, drain info,
        and per-phase timings. See the module docstring for the
        raise-vs-report contract.
    """
    global _state
    if _lock.locked():
        raise ModelSwapInProgress(f"a model swap is already running ({_state})")

    async with _lock:
        t_start = time.perf_counter()
        # KeyError with the known-model list if the name is not in the
        # registry — the path itself is re-resolved by the loader below.
        model_registry.resolve(name)
        if model_registry.remote_config(name) is not None:
            raise KeyError(
                f"{name!r} is a remote provider entry — always available, "
                "nothing to swap; address it per-request via the model field"
            )
        previous = model_registry.active_name() or "(unknown)"
        if name == previous:
            return {
                "ok": True,
                "noop": True,
                "name": name,
                "previous": previous,
                "total_ms": 0.0,
            }

        # Fail-fast validation BEFORE any teardown — a bad target config
        # must never interrupt service. Through the boot loader, so an
        # `extends:` child swaps and LLMVP_MODELS_ROOT / LLMVP_N_CTX apply
        # (the target becomes the PRIMARY, so the n_ctx override is right).
        target_config = load_named_config(
            name, apply_n_ctx=True, root=model_registry.CONFIGS_DIR
        )
        old_config = get_config()

        # Late import: lifecycle has no reverse import of this module, but
        # keeping it out of module scope avoids ordering surprises at startup.
        from core.lifecycle import initialize_server_async

        result: Dict[str, Any] = {
            "ok": False,
            "noop": False,
            "name": name,
            "previous": previous,
            "rolled_back": False,
            "forced": False,
            "expired_sessions": 0,
            "evicted_streams": 0,
        }
        log.info("🔁 Model swap: %s -> %s (drain %.0fs)", previous, name, drain_s)
        _state = "draining"
        try:
            backend = get_backend()
            if backend is not None:
                result.update(await _drain(backend, session_manager, drain_s))

            t0 = time.perf_counter()
            if session_manager is not None:
                try:
                    await session_manager.shutdown()
                except Exception:  # noqa: BLE001 — teardown proceeds regardless
                    log.exception("⚠️ model swap: session manager shutdown failed")
            await shutdown_backend_async()
            result["teardown_ms"] = (time.perf_counter() - t0) * 1000

            _state = "loading"
            set_config(target_config)
            _reset_model_globals()
            t0 = time.perf_counter()
            try:
                await initialize_server_async()
            except Exception as first_exc:  # noqa: BLE001 — reported, not raised
                _state = "rolling-back"
                log.exception(
                    "❌ model swap: init of %r failed — rolling back to %r",
                    name,
                    previous,
                )
                await shutdown_backend_async()  # clear any half-built backend
                set_config(old_config)
                _reset_model_globals()
                try:
                    await initialize_server_async()
                except Exception:  # noqa: BLE001 — reported, not raised
                    log.exception(
                        "💥 model swap: rollback init ALSO failed — server is "
                        "modelless until the next successful request-path init"
                    )
                    result["error"] = (
                        f"target init failed ({first_exc}) AND rollback failed; "
                        "server is modelless"
                    )
                    return result
                result["rolled_back"] = True
                result["error"] = f"target init failed, rolled back: {first_exc}"
                return result
            result["load_ms"] = (time.perf_counter() - t0) * 1000

            model_registry.write_pointer(name)
            result["ok"] = True
            log.info(
                "✅ Model swap complete: %s -> %s (%.1fs)",
                previous,
                name,
                time.perf_counter() - t_start,
            )
            return result
        finally:
            _state = None
            result["total_ms"] = (time.perf_counter() - t_start) * 1000
