"""
Parse raw command strings into structured Command objects.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import List


@dataclass
class Command:
    """Simple representation of a player command."""

    action: str  # canonical verb, e.g., 'go', 'take'
    args: List[str] = None  # list of arguments (may be empty)


def parse_command(raw: str) -> Command:
    """
    Convert a raw input string into a Command.
    Recognises synonyms, shortcuts, and multi‑word verbs such as "pick up".
    """
    tokens = raw.strip().lower().split()
    if not tokens:
        return Command(action="empty", args=[])

    # Direct shortcuts for movement (single‑word directions)
    dirs = {"north": "go", "south": "go", "east": "go", "west": "go"}
    if tokens[0] in dirs and len(tokens) == 1:
        return Command(action="go", args=[tokens[0]])

    # Handle the multi‑word verb "pick up" → canonical "take"
    if len(tokens) >= 2 and tokens[0] == "pick" and tokens[1] == "up":
        return Command(action="take", args=tokens[2:])

    verb = tokens[0]

    # Mapping of synonyms to canonical actions
    synonym_map = {
        "move": "go",
        "walk": "go",
        "run": "go",
        "look": "look",
        "examine": "examine",
        "inspect": "examine",
        "status": "status",
        "stats": "status",
        "inventory": "inventory",
        "inv": "inventory",
        "take": "take",
        "get": "take",
        "pick": "take",
        "drop": "drop",
        "use": "use",
        "equip": "equip",
        "talk": "talk",
        "speak": "talk",
        "attack": "attack",
        "hit": "attack",
        "fight": "attack",
        "flee": "flee",
        "runaway": "flee",
        "help": "help",
        "quit": "quit",
        "exit": "quit",
    }

    action = synonym_map.get(verb, verb)
    args = tokens[1:] if len(tokens) > 1 else []

    return Command(action=action, args=args)
