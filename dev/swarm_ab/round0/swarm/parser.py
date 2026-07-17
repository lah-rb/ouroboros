"""Parse raw player input into structured command objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass(frozen=True)
class Command:
    """A parsed player instruction.

    Attributes:
        verb: Primary action word (e.g., ``go``, ``take``).
        args: List of additional tokens (e.g., direction or item name).

    >>> cmd = Command(verb='go', args=['north'])
    >>> cmd.verb
    'go'
    >>> cmd.args
    ['north']
    """

    verb: str
    args: List[str]


def parse_command(raw_input: str) -> Command:
    """Convert a line of text entered by the player into a :class:`Command`.

    The parser is case‑insensitive, strips surrounding whitespace,
    and splits on spaces.  Quoted strings are not required for this demo.

    Args:
        raw_input: Exact line read from ``input()``.

    Returns:
        A :class:`Command` instance representing the intent.

    Raises:
        ValueError: If the input is empty after stripping.

    >>> parse_command('  Go North  ')
    Command(verb='go', args=['north'])
    >>> parse_command('take   sword')
    Command(verb='take', args=['sword'])
    """
    stripped = raw_input.strip()
    if not stripped:
        raise ValueError("Input cannot be empty after stripping.")
    parts = stripped.split()
    verb = parts[0].lower()
    args = [part.lower() for part in parts[1:]]
    return Command(verb=verb, args=args)
