#!/usr/bin/env python3
"""
Static Tokens Management

This module handles loading, memory-mapping, and management of
static knowledge base tokens — keyed by PERSONA for multi-persona
pooling (SOUL per pool slot). The "default" persona is the legacy
prompt.persona_file/knowledge.tokens_bin pair; named personas resolve
through the config's ``personas`` map to their own token bins.
"""

import logging
import mmap
from typing import Dict, List

# Local imports
from core.config import get_config

log = logging.getLogger("llm-mvp")


class StaticTokensManager:
    """
    Manages static token buffers, KEYED BY RESOLVED TOKENS-BIN PATH
    (Phase 2a): a persona name is resolved through the ACTIVE config at
    every access, so a model swap structurally selects the new model's
    buffers — a stale same-named persona from the previous model cannot
    be returned, with or without cleanup.

    Features:
    - Memory-mapped file access for efficiency
    - Lazy loading on first access, per persona
    - Clean shutdown handling
    """

    def __init__(self):
        self._views: Dict[str, memoryview] = {}
        self._mmaps: Dict[str, mmap.mmap] = {}
        self._tokens: Dict[str, List[int]] = {}

    @staticmethod
    def _key(persona: str) -> str:
        return str(get_config().resolve_persona(persona).tokens_bin)

    def load_static_buffer(self, persona: str = "default") -> None:
        """Load a persona's static token buffer from disk.

        Auto-builds the per-persona cache first if it is missing or stale
        (persona/knowledge/active-config changed since it was written), so
        editing SOUL.md — or a slot persona like USER_SIM.md — never
        silently runs the model against an outdated persona.
        """
        config = get_config()
        tokens_bin = config.resolve_persona(persona).tokens_bin

        try:
            from preprocessing.builder import build_and_write, cache_is_stale

            if cache_is_stale(config, persona):
                log.info(
                    "🧩 Static token cache missing or stale [%s] — rebuilding %s",
                    persona,
                    tokens_bin,
                )
                build_and_write(config, emit=log.info, persona=persona)
        except Exception as exc:
            # Don't fail startup on a build error; fall through and try to
            # load whatever is on disk (lifecycle degrades to lightweight
            # mode if that also fails).
            log.warning("⚠️ Static token auto-build skipped [%s]: %s", persona, exc)

        try:
            f = open(tokens_bin, "rb")
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            view = memoryview(mm).cast("I")

            key = str(tokens_bin)
            self._views[key] = view
            self._mmaps[key] = mm
            self._tokens[key] = list(view)

            print(
                f"✅ Loaded static token buffer [{persona}] ({len(view)} tokens) "
                f"from {tokens_bin}"
            )

        except Exception as exc:
            raise RuntimeError(f"❌ Failed to load static tokens [{persona}]: {exc}")

    def get_static_tokens(self, persona: str = "default") -> List[int]:
        """
        Get a persona's loaded static tokens (resolved against the ACTIVE
        config), lazily loading on first request for a named
        (non-default) persona.

        Returns an empty list if tokens were not loaded (e.g. when
        --skip-knowledge is active). Callers should handle the
        empty case gracefully — the model will operate without a
        system prompt prefix.
        """
        try:
            key = self._key(persona)
        except Exception:  # noqa: BLE001 — no config yet => no buffers
            return []
        if key not in self._tokens and persona != "default":
            # Named personas lazy-load; "default" keeps its legacy lifecycle
            # (loaded explicitly at startup, absent under --skip-knowledge).
            self.load_static_buffer(persona)
        return self._tokens.get(key) or []

    def cleanup(self) -> None:
        """Clean up memory-mapped resources for every persona."""
        self._views.clear()
        for mm in self._mmaps.values():
            try:
                mm.close()
            except Exception:
                pass
        self._mmaps.clear()
        self._tokens.clear()


# Global singleton manager
manager = StaticTokensManager()


def get_static_tokens(persona: str = "default") -> List[int]:
    """
    Get a persona's static tokens list.

    Returns:
        List[int]: Static token IDs
    """
    return manager.get_static_tokens(persona)
