"""
Serialization utilities for saving and loading the full game state.
"""

import json

from .models import GameState


def save_game(state: GameState, file_path: str) -> None:
    """
    Writes the current ``GameState`` to ``file_path`` as pretty‑printed JSON.
    """
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2)


def load_game(file_path: str) -> GameState:
    """
    Reads a JSON file created by ``save_game`` and reconstructs the ``GameState``.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return GameState.from_dict(data)
