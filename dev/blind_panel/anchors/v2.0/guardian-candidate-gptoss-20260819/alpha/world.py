"""World loading utilities.

Provides:
- ``load_world``: reads ``data/world.yaml`` and constructs entity objects.
- ``init_world_state``: builds the mutable ``world_state`` dict that tracks
  dynamic aspects (items in rooms, monster defeat flags, NPC dialogue progress).

Both functions are pure (no side‑effects beyond reading the YAML file) and
conform to the contracts defined in the project blueprint.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict

import yaml

from entities import Item, Monster, NPC, Room

# Path to the static world definition (relative to the working directory)
_WORLD_YAML_PATH = pathlib.Path("data/world.yaml")


def _load_yaml_file(path: pathlib.Path) -> Dict[str, Any]:
    """Read a YAML file and return its contents as a Python dict.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        yaml.YAMLError: If the file cannot be parsed.
    """
    if not path.is_file():
        raise FileNotFoundError(f"World definition file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Expected top‑level mapping in {path}, got {type(data)}")
    return data


def load_world() -> Dict[str, Dict[str, Any]]:
    """Load the static world definition and instantiate entity objects.

    Returns a dictionary with the exact keys required by ``GameEngine``:

    ```python
    {
        "rooms":   {room_id: Room, ...},
        "items":   {item_id: Item, ...},
        "npcs":    {npc_id: NPC, ...},
        "monsters":{monster_id: Monster, ...}
    }
    ```

    The function validates that all required top‑level sections exist and
    that each identifier is unique within its section.
    """
    raw = _load_yaml_file(_WORLD_YAML_PATH)

    # Validate presence of required sections
    for key in ("rooms", "items", "npcs", "monsters"):
        if key not in raw:
            raise KeyError(f"Missing required top‑level key '{key}' in world.yaml")

    # Helper to ensure unique IDs and build mapping
    def _build_mapping(
        entries: list[dict[str, Any]], ctor, entity_name: str
    ) -> Dict[str, Any]:
        mapping: Dict[str, Any] = {}
        for entry in entries:
            if "id" not in entry:
                raise KeyError(f"{entity_name} entry missing 'id': {entry}")
            obj_id = entry["id"]
            if obj_id in mapping:
                raise ValueError(f"Duplicate {entity_name} id '{obj_id}'")
            mapping[obj_id] = ctor.from_dict(entry)
        return mapping

    items = _build_mapping(raw["items"], Item, "Item")
    npcs = _build_mapping(raw["npcs"], NPC, "NPC")
    monsters = _build_mapping(raw["monsters"], Monster, "Monster")
    rooms = _build_mapping(raw["rooms"], Room, "Room")

    return {
        "rooms": rooms,
        "items": items,
        "npcs": npcs,
        "monsters": monsters,
    }


def init_world_state(rooms: Dict[str, Room]) -> Dict[str, Any]:
    """Create the mutable ``world_state`` structure from a set of rooms.

    The returned dict follows the contract:

    ```python
    {
        "rooms": {
            room_id: {
                "items": [item_id, ...],
                "monster_defeated": bool,
                "npc_state": {
                    npc_id: {"dialogue_index": int},
                    ...
                },
            },
            ...
        },
        "defeated_monsters": []
    }
    ```

    Args:
        rooms: Mapping of room identifiers to :class:`entities.Room` objects.

    Returns:
        A fresh world state ready for use by the game engine.
    """
    state_rooms: Dict[str, Dict[str, Any]] = {}
    for room_id, room in rooms.items():
        # Items present at start are a shallow copy of the room's item list
        items_copy = list(room.items)

        # Monster defeat flag: False if a monster is present, True otherwise
        monster_defeated = room.monster is None

        # NPC state initialisation – each NPC starts with dialogue_index 0
        npc_state = {
            npc_id: {"dialogue_index": 0} for npc_id in room.npcs
        }

        state_rooms[room_id] = {
            "items": items_copy,
            "monster_defeated": monster_defeated,
            "npc_state": npc_state,
        }

    return {
        "rooms": state_rooms,
        "defeated_monsters": [],
    }


__all__ = ["load_world", "init_world_state"]
