"""Per-request remote-model dispatch (MULTI_MODEL_PLAN Phase 3).

A completion request naming a model routes here first:
  - name is a REMOTE registry entry  -> the provider adapter serves it
  - name is the ACTIVE local config  -> normal local path (caller falls
    through)
  - name is an INACTIVE local config -> explicit error telling the caller
    to swapModel first (a completion must never trigger a multi-minute
    weight load as a side effect)

Adapters are cached per entry name and invalidated by config-file mtime,
so editing a remote yaml takes effect on the next request — no restart,
matching the hotswap spirit of the local side.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from core import model_registry
from inference.providers import (
    ClaudeCliProvider,
    OpenAICompatProvider,
    RemoteCompletion,
)

log = logging.getLogger("llm-mvp")

_adapters: Dict[str, Tuple[float, Any]] = {}  # name -> (config mtime, adapter)


class InactiveLocalModel(RuntimeError):
    """Request named a local config that isn't the resident one."""


def _build_adapter(name: str):
    cfg = model_registry.remote_config(name)
    if cfg is None:
        return None
    if cfg.provider == "claude_cli":
        adapter = ClaudeCliProvider(
            cfg.model, claude_bin=cfg.claude_bin, timeout_s=cfg.timeout_s
        )
    else:
        adapter = OpenAICompatProvider(
            cfg.model,
            base_url=cfg.base_url or "",
            api_key_env=cfg.api_key_env,
            timeout_s=cfg.timeout_s,
        )
    return cfg, adapter


def get_adapter(name: str):
    """(RemoteModelConfig, adapter) for a remote entry, else None."""
    from core.config import resolve_config_path

    # resolve_config_path searches root + boss/ — the same lookup
    # remote_config uses. The old hardcoded root path never found the
    # boss/ entries, so the first live forced consult misrouted
    # 'boss-sonnet' to the inactive-local error instead of the provider.
    path = resolve_config_path(name, model_registry.CONFIGS_DIR)
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    cached = _adapters.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    built = _build_adapter(name)
    if built is None:
        return None
    _adapters[name] = (mtime, built)
    return built


def resolve_route(model: Optional[str]) -> Optional[Tuple[Any, Any]]:
    """Route a request's model name.

    Returns None for "serve locally" (no model named, or the active local
    config named). Returns (config, adapter) for a remote entry. Raises
    for anything else (inactive local, unknown).
    """
    if not model:
        return None
    remote = get_adapter(model)
    if remote is not None:
        return remote
    active = model_registry.active_name()
    if model == active:
        return None
    known = {e.name for e in model_registry.list_models()}
    if model in known:
        raise InactiveLocalModel(
            f"model {model!r} is a local config but {active!r} is resident — "
            f"run swapModel first (a completion never loads weights)"
        )
    raise KeyError(f"unknown model {model!r} — see the models query")


async def remote_completion(
    model: str,
    prompt: str,
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> RemoteCompletion:
    """Serve one completion via a remote entry's adapter, applying the
    entry's defaults and system_file persona."""
    routed = get_adapter(model)
    if routed is None:
        raise KeyError(f"{model!r} is not a remote entry")
    cfg, adapter = routed

    system = None
    if cfg.system_file:
        try:
            system = cfg.system_file.expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("remote %s: system_file unreadable (%s)", model, exc)

    result = await adapter.complete(
        prompt,
        system=system,
        max_tokens=max_tokens if max_tokens is not None else cfg.max_tokens_default,
        temperature=(
            temperature if temperature is not None else cfg.temperature_default
        ),
    )
    log.info(
        "🌐 remote completion [%s/%s]: in=%d out=%d",
        cfg.provider,
        cfg.model,
        result.input_tokens,
        result.output_tokens,
    )
    return result
