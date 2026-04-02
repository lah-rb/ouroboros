#!/usr/bin/env python3
"""
Static Tokens Management

This module handles loading, memory-mapping, and management of
static knowledge base tokens.
"""

import mmap
from pathlib import Path
from typing import List

# Local imports
from core.config import get_config


class StaticTokensManager:
    """
    Manages the static token buffer for the knowledge base.

    Features:
    - Memory-mapped file access for efficiency
    - Lazy loading on first access
    - Clean shutdown handling
    """

    def __init__(self):
        self._static_tokens_view = None
        self._static_mmap_obj = None
        self._static_tokens_list: List[int] = []

    def load_static_buffer(self) -> None:
        """Load the static token buffer from disk."""
        config = get_config()

        try:
            f = open(config.knowledge.tokens_bin, "rb")
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            view = memoryview(mm).cast("I")

            self._static_tokens_view = view
            self._static_mmap_obj = mm
            self._static_tokens_list = list(view)

            print(
                f"✅ Loaded static token buffer ({len(view)} tokens) "
                f"from {config.knowledge.tokens_bin}"
            )

        except Exception as exc:
            raise RuntimeError(f"❌ Failed to load static tokens: {exc}")

    def get_static_tokens(self) -> List[int]:
        """
        Get the loaded static tokens.

        Returns:
            List[int]: Static token IDs

        Raises:
            RuntimeError: If tokens not loaded
        """
        if self._static_tokens_list is None or not self._static_tokens_list:
            raise RuntimeError(
                "Static tokens not loaded. Call load_static_buffer() first."
            )

        return self._static_tokens_list

    def cleanup(self) -> None:
        """Clean up memory-mapped resources."""
        if self._static_tokens_view is not None:
            del self._static_tokens_view
            self._static_tokens_view = None

        if self._static_mmap_obj is not None:
            self._static_mmap_obj.close()
            self._static_mmap_obj = None

        self._static_tokens_list.clear()


# Global singleton manager
manager = StaticTokensManager()


def get_static_tokens() -> List[int]:
    """
    Get the global static tokens list.

    Returns:
        List[int]: Static token IDs
    """
    return manager.get_static_tokens()
