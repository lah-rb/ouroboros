"""Save and load utilities for the adventure game.

The module provides two functions that operate on the JSON representation
defined by the ``save_file_json`` contract:

* ``save_game(state, path="save.json")`` – serialises a state dict to a file.
* ``load_game(path="save.json")`` – deserialises the file back into a dict.

Both functions work with plain Python data structures (no custom objects)
because the game engine is expected to convert entity instances to/from
their ``to_dict``/``from_dict`` forms before calling these helpers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def save_game(state: Dict[str, Any], path: str = "save.json") -> None:
    """Write ``state`` to ``path`` as pretty‑printed JSON.

    The ``state`` argument must conform to the ``save_file_json`` contract:

    ```json
    {
        "player": { ... player_state ... },
        "world":  { ... world_state ... }
    }
    ```

    Parameters
    ----------
    state : dict
        The complete game state to persist.
    path : str, optional
        Destination file path (relative to the working directory). Defaults
        to ``"save.json"``.
    """
    file_path = Path(path)

    # Ensure the parent directory exists (e.g. if a custom sub‑folder is used)
    if file_path.parent and not file_path.parent.exists():
        file_path.parent.mkdir(parents=True, exist_ok=True)

    with file_path.open("w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2)


def load_game(path: str = "save.json") -> Dict[str, Any]:
    """Read a saved game from ``path`` and return the deserialised dict.

    Returns a dictionary that matches the ``save_file_json`` contract.
    Raises ``FileNotFoundError`` if the file does not exist and
    ``ValueError`` if the JSON structure is malformed.

    Parameters
    ----------
    path : str, optional
        Path to the saved JSON file. Defaults to ``"save.json"``.

    Returns
    -------
    dict
        The loaded game state.
    """
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"Save file not found: {path}")

    with file_path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)

    # Minimal validation to catch obvious corruption
    if not isinstance(data, dict):
        raise ValueError("Saved game JSON must be an object at the top level")
    if "player" not in data or "world" not in data:
        raise ValueError(
            "Saved game JSON missing required keys: 'player' and/or 'world'"
        )

    return data


__all__ = ["save_game", "load_game"]
