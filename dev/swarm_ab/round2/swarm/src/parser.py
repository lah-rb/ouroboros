"""Command parsing from raw player input."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class Command:
    """Structured representation of a player command.

    Args:
        verb: Primary action word (e.g. "go", "take").
        args: List of additional tokens (e.g. direction, item name).

    >>> cmd = Command(verb="go", args=["north"])
    >>> cmd.verb
    'go'
    >>> isinstance(cmd.args, list)
    True
    """

    verb: str
    args: List[str] = field(default_factory=list)


def parse_command(raw: str) -> Command:
    """Convert a raw input line into a :class:`Command`.

    Splits on whitespace, lower‑cases the verb, and preserves remaining tokens.

    Args:
        raw: User input string.

    Returns:
        A :class:`Command` instance.

    Raises:
        ValueError: If ``raw`` is empty after stripping.

    >>> parse_command("Go north")
    Command(verb='go', args=['north'])
    """
    parts = raw.strip().split()
    if not parts:
        raise ValueError("Empty command string")
    verb = parts[0].lower()
    args = parts[1:]
    return Command(verb=verb, args=args)
