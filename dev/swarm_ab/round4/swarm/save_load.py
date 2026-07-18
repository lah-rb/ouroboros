"""Serializes and deserializes the full game state to JSON files."""

from __future__ import annotations
import json
from typing import Dict, Any



def save_game(state: Dict[str, Any], filename: str) -> None:
    """Write the current game state to ``filename`` as JSON.

    The ``state`` dictionary must contain exactly the keys:

    * ``player`` – a ``Player`` instance (serializable via its __dict__).
    * ``rooms`` – mapping room_id → dict with keys ``items``, ``monsters``,
      ``visited`` as defined in the engine's RoomState contract.
    * ``npcs`` – mapping npc_id → dict with key ``dialogue_index``.
    * ``monsters`` – mapping monster_id → dict with keys ``health``,
      ``alive``, and optional ``phase`` for bosses.

    Args:
        state: Full game state adhering to the SaveLoad contract.
        filename: Destination file path.

    Raises:
        OSError: If the file cannot be written.
    """
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(state, f, default=lambda o: o.__dict__, indent=2)


def load_game(filename: str) -> Dict[str, Any]:
    """Read a saved game JSON file and reconstruct the state dict.

    Returns:
        A dictionary matching the structure described in ``save_game``.
        All nested objects are plain ``dict`` instances; callers must
        re‑hydrate them into model objects as needed.

    Raises:
        FileNotFoundError: If ``filename`` does not exist.
        json.JSONDecodeError: If the file contents are not valid JSON.
    """
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)
