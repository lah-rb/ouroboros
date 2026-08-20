"""Command parsing utilities.

The game loop reads raw input from the player and passes it to
``parse_command``.  The function returns a :class:`Command` instance that
encapsulates a canonical verb and any arguments supplied by the player.
All other modules (e.g. ``game.py``) should rely only on the ``verb`` and
``args`` attributes.

The parser is deliberately forgiving: it normalises case, trims whitespace,
recognises common synonyms, and never raises an exception for unknown input.
Instead it returns a ``Command`` with verb ``"unknown"`` (or ``"noop"`` for
empty input) so the caller can handle the situation gracefully.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


# ----------------------------------------------------------------------
# Public data structure
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Command:
    """A parsed player command.

    Attributes
    ----------
    verb: str
        The canonical action name (e.g. ``"move"``, ``"take"``, ``"help"``).
    args: List[str]
        Positional arguments following the verb.  For movement commands
        this is typically a single direction string; for item or NPC
        interactions it contains the target identifier(s).
    """

    verb: str
    args: List[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# Helper constants
# ----------------------------------------------------------------------
_DIRECTION_TOKENS = {
    "north",
    "n",
    "south",
    "s",
    "east",
    "e",
    "west",
    "w",
    "up",
    "u",
    "down",
    "d",
}

# Mapping of raw tokens to canonical verbs.
# Tokens that require an argument (e.g. ``take <item>``) are handled
# uniformly – the remaining tokens become ``args``.
_TOKEN_MAP: dict[str, str] = {
    # movement helpers (handled specially)
    # other commands
    "look": "look",
    "l": "look",
    "examine": "examine",
    "inspect": "examine",
    "describe": "examine",
    "inventory": "inventory",
    "inv": "inventory",
    "i": "inventory",
    "take": "take",
    "get": "take",
    "pick": "take",
    "pickup": "take",
    "drop": "drop",
    "discard": "drop",
    "equip": "equip",
    "wear": "equip",
    "unequip": "unequip",
    "remove": "unequip",
    "use": "use",
    "drink": "use",
    "consume": "use",
    "talk": "talk",
    "speak": "talk",
    "chat": "talk",
    "attack": "attack",
    "hit": "attack",
    "strike": "attack",
    "defend": "defend",
    "block": "defend",
    "flee": "flee",
    "run": "flee",
    "help": "help",
    "?": "help",
    "status": "status",
    "stats": "status",
    "save": "save",
    "load": "load",
    "quit": "quit",
    "exit": "quit",
    "q": "quit",
    "restart": "restart",
}


def _clean_token(token: str) -> str:
    """Strip surrounding punctuation from a token and lower‑case it."""
    return re.sub(r"^[^\w]+|[^\w]+$", "", token).lower()


def parse_command(raw: str) -> Command:
    """Parse a raw command string into a :class:`Command`.

    The parser normalises case, removes leading/trailing punctuation,
    and translates synonyms to a stable verb name.

    Parameters
    ----------
    raw: str
        The exact line entered by the player.

    Returns
    -------
    Command
        An object describing the intended action. ``verb`` will be one of
        the canonical verbs defined in this module, or ``"unknown"``
        (or ``"noop"`` for empty input) if the parser cannot recognise the
        command.
    """
    # Trim whitespace; treat empty input as a no‑op.
    stripped = raw.strip()
    if not stripped:
        return Command("noop", [])

    # Tokenise while preserving quoted substrings (e.g. item names with spaces).
    # For simplicity we split on whitespace and then clean punctuation.
    raw_tokens = stripped.split()
    tokens = [_clean_token(tok) for tok in raw_tokens if _clean_token(tok)]

    if not tokens:
        return Command("noop", [])

    first = tokens[0]

    # ------------------------------------------------------------------
    # Movement commands
    # ------------------------------------------------------------------
    if first in _DIRECTION_TOKENS:
        # Direct direction (e.g. "north" or "n")
        return Command("move", [first])

    if first in {"go", "move"} and len(tokens) > 1:
        second = tokens[1]
        if second in _DIRECTION_TOKENS:
            return Command("move", [second])
        # If the second token is not a direction we fall through to unknown.

    # ------------------------------------------------------------------
    # Other canonical commands
    # ------------------------------------------------------------------
    if first in _TOKEN_MAP:
        verb = _TOKEN_MAP[first]
        args = tokens[1:]  # remaining tokens become arguments (may be empty)
        return Command(verb, args)

    # ------------------------------------------------------------------
    # Fallback – unknown command
    # ------------------------------------------------------------------
    return Command("unknown", tokens)


__all__ = ["Command", "parse_command"]
