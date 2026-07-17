"""Utility helpers shared across the engine."""

from __future__ import annotations

import random
from typing import List, TypeVar

T = TypeVar("T")


def random_choice(seq: List[T]) -> T:
    """Return a randomly selected element from ``seq``.

    Args:
        seq: Non‑empty list of choices.

    Returns:
        One element from ``seq`` chosen uniformly at random.

    Raises:
        ValueError: If ``seq`` is empty.

    >>> random_choice([1, 2, 3]) in [1, 2, 3]
    True
    """
    if not seq:
        raise ValueError("random_choice() arg is an empty sequence")
    return random.choice(seq)


def format_text(text: str) -> str:
    """Normalize whitespace for display.

    Collapses consecutive spaces and strips leading/trailing whitespace.

    Args:
        text: Raw string.

    Returns:
        Cleaned string.

    >>> format_text("  Hello   world  ")
    'Hello world'
    """
    return " ".join(text.split())


def read_file(path: str) -> str:
    """Read the entire contents of a file using UTF‑8 encoding.

    Args:
        path: The filesystem path to the file.

    Returns:
        The full text content of the file.

    Raises:
        FileNotFoundError: If the file does not exist.
    """
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
