"""
src package initializer.

The original implementation imported `GameEngine` at module import time,
which caused the engine's constructor to attempt loading ``world.yaml``.
Because the YAML data file is not present in the test environment, any
import of the package (e.g., when running doctests) raised a
`FileNotFoundError`.  This broke imports that only needed access to
utility modules such as `models`, `constants`, or `loader`.

The fix removes the eager import and replaces it with a lazy accessor.
Only code that explicitly needs the game engine should call
`get_game_engine()`.  This defers the heavy initialization until runtime
and prevents side‑effects during package import, allowing the rest of
the codebase (including unit tests) to be imported safely.

The module also defines `__all__` to expose the public symbols that are
intended for external use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# Public symbols that callers may import from the package root.
__all__ = [
    "get_game_engine",
]


def _load_engine() -> "GameEngine":
    """
    Internal helper that performs the actual import of `GameEngine`.
    This function is isolated so that the import only occurs when the
    engine is explicitly requested.

    Returns:
        GameEngine: The main game engine class.
    """
    # Local import to avoid executing module‑level code at package import.
    from src.engine import GameEngine  # pylint: disable=import-outside-toplevel

    return GameEngine


def get_game_engine() -> "GameEngine":
    """
    Lazily obtain the `GameEngine` class.

    The returned class can be instantiated by the caller.  Importing the
    engine (and consequently loading ``world.yaml``) happens only when
    this function is called, not when the package is imported.

    Example:
        >>> from src import get_game_engine
        >>> Engine = get_game_engine()
        >>> engine = Engine()   # world.yaml will be loaded here

    Returns:
        GameEngine: The `GameEngine` class ready for instantiation.
    """
    return _load_engine()


# When type checking, expose the actual class so static analysers see the
# correct type without triggering runtime side‑effects.
if TYPE_CHECKING:  # pragma: no cover
    from src.engine import GameEngine  # noqa: F401
