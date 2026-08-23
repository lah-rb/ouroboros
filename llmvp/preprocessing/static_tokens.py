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
    def _key(persona: str, config=None) -> str:
        cfg = config if config is not None else get_config()
        return str(cfg.resolve_persona(persona).tokens_bin)

    @staticmethod
    def _read_token_file(tokens_bin) -> tuple:
        """Open + validate a tokens.bin. Returns (mm, view, ids) or raises
        ValueError on a provenance failure (bad magic, count mismatch, id
        outside the stamped vocab)."""
        from preprocessing.builder import TOKENS_BIN_MAGIC, TOKENS_BIN_HEADER_LEN

        f = open(tokens_bin, "rb")
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        view = None
        try:
            # mmap slicing returns plain bytes — no exported pointers yet.
            if mm[: len(TOKENS_BIN_MAGIC)] != TOKENS_BIN_MAGIC:
                raise ValueError("no provenance header (legacy or foreign file)")
            import struct

            n_vocab, count = struct.unpack(
                "<II", mm[len(TOKENS_BIN_MAGIC) : TOKENS_BIN_HEADER_LEN]
            )
            view = memoryview(mm)[TOKENS_BIN_HEADER_LEN:].cast("I")
            if len(view) != count:
                raise ValueError(f"header says {count} ids, file holds {len(view)}")
            ids = list(view)
            if ids and n_vocab and max(ids) >= n_vocab:
                raise ValueError(f"id {max(ids)} outside the stamped vocab ({n_vocab})")
            return mm, view, ids
        except Exception:
            # Release the view BEFORE closing — an mmap with exported
            # pointers refuses to close.
            if view is not None:
                view.release()
            mm.close()
            raise

    def load_static_buffer(self, persona: str = "default", config=None) -> None:
        """Load a persona's static token buffer from disk, resolved against
        ``config`` (the ACTIVE config when None).

        Auto-builds the per-persona cache first if it is missing or stale
        (persona/knowledge/active-config changed since it was written), so
        editing SOUL.md — or a slot persona like USER_SIM.md — never
        silently runs the model against an outdated persona.
        """
        cfg = config if config is not None else get_config()
        tokens_bin = cfg.resolve_persona(persona).tokens_bin

        try:
            from preprocessing.builder import build_and_write, cache_is_stale

            if cache_is_stale(cfg, persona):
                log.info(
                    "🧩 Static token cache missing or stale [%s] — rebuilding %s",
                    persona,
                    tokens_bin,
                )
                build_and_write(cfg, emit=log.info, persona=persona)
        except Exception as exc:
            # Don't fail startup on a build error; fall through and try to
            # load whatever is on disk (lifecycle degrades to lightweight
            # mode if that also fails).
            log.warning("⚠️ Static token auto-build skipped [%s]: %s", persona, exc)

        try:
            try:
                mm, view, ids = self._read_token_file(tokens_bin)
            except ValueError as bad:
                # Provenance failure — rebuild once, then trust the result.
                log.warning(
                    "⚠️ tokens.bin failed validation [%s]: %s — rebuilding",
                    persona,
                    bad,
                )
                from preprocessing.builder import build_and_write

                build_and_write(cfg, emit=log.info, persona=persona)
                mm, view, ids = self._read_token_file(tokens_bin)

            key = str(tokens_bin)
            self._views[key] = view
            self._mmaps[key] = mm
            self._tokens[key] = ids

            print(
                f"✅ Loaded static token buffer [{persona}] ({len(view)} tokens) "
                f"from {tokens_bin}"
            )

        except Exception as exc:
            raise RuntimeError(f"❌ Failed to load static tokens [{persona}]: {exc}")

    def get_static_tokens(self, persona: str = "default", config=None) -> List[int]:
        """
        Get a persona's loaded static tokens (resolved against ``config``,
        the ACTIVE config when None), lazily loading on first request for a
        named (non-default) persona or a non-active config.

        Returns an empty list if tokens were not loaded (e.g. when
        --skip-knowledge is active). Callers should handle the
        empty case gracefully — the model will operate without a
        system prompt prefix.
        """
        try:
            key = self._key(persona, config)
        except Exception:  # noqa: BLE001 — no config yet => no buffers
            return []
        if key not in self._tokens and (persona != "default" or config is not None):
            # Named personas (and non-active configs) lazy-load; the active
            # "default" keeps its legacy lifecycle (loaded explicitly at
            # startup). Under --skip-knowledge nothing lazy-loads: the
            # server runs bare-template by construction.
            try:
                from core import lifecycle

                if getattr(lifecycle, "_skip_knowledge", False):
                    return []
            except Exception:  # noqa: BLE001 — lifecycle absent in tests
                pass
            self.load_static_buffer(persona, config)
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


def get_static_tokens(persona: str = "default", config=None) -> List[int]:
    """
    Get a persona's static tokens list, resolved against ``config``
    (the ACTIVE config when None).

    Returns:
        List[int]: Static token IDs
    """
    return manager.get_static_tokens(persona, config)
