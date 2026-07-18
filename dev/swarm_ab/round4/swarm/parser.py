"""Parse raw player input into a structured command dictionary.

The module provides a single public function :func:`parse_command` which
converts a line of user input into a normalized command dictionary.
It contains no top‑level side effects, making it safe to import in any
context (including doctests).

Typical usage::

    from parser import parse_command

    cmd = parse_command("go north")
    # {'action': 'move', 'direction': 'north'}

"""

from __future__ import annotations

import re
from typing import Any, Dict

__all__ = ["parse_command"]


def parse_command(raw: str) -> Dict[str, Any]:
    """Convert a line of user input into a normalized command dict.

    The function recognises movement, inventory actions, combat,
    interaction and meta commands. Keys are fixed so downstream dispatch
    can rely on them.

    Args:
        raw: The exact line entered by the player.

    Returns:
        A dictionary with at least an ``action`` key and additional
        parameters required for that action.

    Raises:
        ValueError: If the input cannot be matched to any known command.

    Examples:
        >>> parse_command("go north")
        {'action': 'move', 'direction': 'north'}
        >>> parse_command("attack goblin")
        {'action': 'attack', 'target_id': 'goblin'}
        >>> parse_command("take ancient_amulet")
        {'action': 'take', 'item_id': 'ancient_amulet'}
    """
    raw = raw.strip().lower()

    # Movement
    if m := re.match(r"^go\s+(north|south|east|west)$", raw):
        return {"action": "move", "direction": m.group(1)}

    # Combat actions
    if m := re.match(r"^attack\s+(\w+)$", raw):
        return {"action": "attack", "target_id": m.group(1)}

    # Inventory manipulation
    if m := re.match(r"^take\s+(\w+)$", raw):
        return {"action": "take", "item_id": m.group(1)}
    if m := re.match(r"^drop\s+(\w+)$", raw):
        return {"action": "drop", "item_id": m.group(1)}
    if m := re.match(r"^use\s+(\w+)$", raw):
        return {"action": "use", "item_id": m.group(1)}

    # Interaction
    if m := re.match(r"^examine\s+(\w+)$", raw):
        return {"action": "examine", "target_id": m.group(1)}
    if m := re.match(r"^talk\s+to\s+(\w+)$", raw):
        return {"action": "talk", "npc_id": m.group(1)}

    # Simple meta commands
    if raw in {"look", "l"}:
        return {"action": "look"}
    if raw in {"status", "stats"}:
        return {"action": "status"}
    if raw == "help":
        return {"action": "help"}
    if raw == "quit":
        return {"action": "quit"}
    if raw == "flee":
        return {"action": "flee"}

    # Save / load commands with optional filename
    if raw.startswith("save"):
        parts = raw.split()
        filename = parts[1] if len(parts) > 1 else "savegame.json"
        return {"action": "save", "filename": filename}
    if raw.startswith("load"):
        parts = raw.split()
        filename = parts[1] if len(parts) > 1 else "savegame.json"
        return {"action": "load", "filename": filename}

    raise ValueError(f"Unrecognized command: {raw}")


if __name__ == "__main__":
    # Simple manual test harness; runs only when the module is executed
    # directly, never on import.
    import sys

    if len(sys.argv) > 1:
        user_input = " ".join(sys.argv[1:])
        try:
            result = parse_command(user_input)
            print(result)
        except ValueError as exc:
            print(f"Error: {exc}")
    else:
        print("Usage: python parser.py <command>")
