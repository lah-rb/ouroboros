"""Serialization of full game state to and from JSON files."""

from __future__ import annotations

import json
from typing import Any

# Architecture‑declared imports
from src.models import GameState, Player, Room, NPC, Monster


def save_game(state: GameState, path: str) -> None:
    """Serialize a ``GameState`` instance to *path* as JSON matching the
    canonical GameState schema.

    The output structure uses the exact keys defined in the data contracts:
    ``player_location``, ``player``, ``rooms``, ``npcs``, ``monsters``,
    ``turn_counter``, ``game_over`` and ``victory``.

    Args:
        state: The current mutable game state to persist.
        path: Destination file path.

    Raises:
        OSError: If the file cannot be written.
    """

    def _to_serialisable(value: Any) -> Any:
        """Recursively convert model objects, dicts and iterables into JSON‑compatible structures."""
        # Primitive types are already serialisable.
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value

        # Model objects – turn their __dict__ into a serialisable mapping.
        if isinstance(value, (GameState, Player, Room, NPC, Monster)):
            return {k: _to_serialisable(v) for k, v in value.__dict__.items()}

        # Dictionaries – ensure keys are strings and values are serialised.
        if isinstance(value, dict):
            return {str(k): _to_serialisable(v) for k, v in value.items()}

        # Lists / tuples – serialise each element.
        if isinstance(value, (list, tuple)):
            return [_to_serialisable(item) for item in value]

        # Fallback – represent as string (should not occur for valid state).
        return str(value)

    # Build the contract‑compliant dictionary.
    data = {
        "player_location": _to_serialisable(state.player_location),
        "player": _to_serialisable(state.player),
        "rooms": {
            room_id: _to_serialisable(room) for room_id, room in state.rooms.items()
        },
        "npcs": {npc_id: _to_serialisable(npc) for npc_id, npc in state.npcs.items()},
        "monsters": {
            monster_id: _to_serialisable(monster)
            for monster_id, monster in state.monsters.items()
        },
        "turn_counter": _to_serialisable(state.turn_counter),
        "game_over": _to_serialisable(state.game_over),
        "victory": _to_serialisable(state.victory),
    }

    # Write the JSON representation to disk.
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_game(path: str) -> GameState:
    """Read a JSON file produced by :func:`save_game` and reconstruct a
    :class:`GameState`.

    The JSON must conform exactly to the canonical ``GameState`` schema
    defined in the data contracts.

    Example:
        >>> from tempfile import NamedTemporaryFile
        >>> # `state` should be an existing ``GameState`` instance.
        >>> with NamedTemporaryFile('w+', delete=False) as tmp:
        ...     save_game(state, tmp.name)
        ...     loaded_state = load_game(tmp.name)

    Args:
        path: Path to the saved JSON file.

    Returns:
        A fully rehydrated ``GameState`` instance.

    Raises:
        FileNotFoundError: If *path* does not exist.
        json.JSONDecodeError: If the file does not contain valid JSON.
        KeyError: If any required top‑level fields are missing.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Required top‑level keys according to the contract.
    required_keys = {
        "player_location",
        "player",
        "rooms",
        "npcs",
        "monsters",
        "turn_counter",
        "game_over",
        "victory",
    }
    missing = required_keys - data.keys()
    if missing:
        raise KeyError(f"Missing required GameState fields: {missing}")

    # Reconstruct model objects.
    player = Player(**data["player"])

    rooms = {room_id: Room(**room_data) for room_id, room_data in data["rooms"].items()}

    npcs = {npc_id: NPC(**npc_data) for npc_id, npc_data in data["npcs"].items()}

    monsters = {
        monster_id: Monster(**monster_data)
        for monster_id, monster_data in data["monsters"].items()
    }

    # Build the GameState instance using the exact contract fields.
    return GameState(
        player_location=data["player_location"],
        player=player,
        rooms=rooms,
        npcs=npcs,
        monsters=monsters,
        turn_counter=data["turn_counter"],
        game_over=data["game_over"],
        victory=data["victory"],
    )
