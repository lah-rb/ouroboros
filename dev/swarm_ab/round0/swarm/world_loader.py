"""Load YAML world definition and instantiate entity objects."""

from __future__ import annotations

import pathlib
import yaml
from typing import Dict, Tuple

from entities import Room, Item, NPC, Monster


def load_world(
    data_path: str,
) -> Tuple[Dict[str, Room], Dict[str, Item], Dict[str, NPC], Dict[str, Monster]]:
    """Parse ``world.yaml`` and return concrete entity instances.

    Args:
        data_path: Filesystem path to the YAML file describing the world.
            If a relative path is provided it will be interpreted as relative
            to the directory containing this module.

    Returns:
        A 4‑tuple of dictionaries mapping each entity id to a fully‑initialized
        instance of the corresponding class (Room, Item, NPC, Monster).

    Raises:
        FileNotFoundError: If ``data_path`` does not exist.
        yaml.YAMLError: If the file cannot be parsed as valid YAML.
        ValueError: If required keys are missing from the definition.
    """
    # Resolve the path: treat relative paths as relative to this file's directory.
    path = pathlib.Path(data_path)
    if not path.is_absolute():
        path = pathlib.Path(__file__).parent / data_path

    # Verify the file exists; let FileNotFoundError propagate if not.
    if not path.exists():
        raise FileNotFoundError(f"World definition file not found: {path}")

    # Load YAML content
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Ensure top‑level structure is a dict
    if not isinstance(data, dict):
        raise ValueError("World definition must be a mapping.")

    required_keys = ("rooms", "items", "npcs", "monsters")
    for key in required_keys:
        if key not in data:
            raise ValueError(f"Missing required top‑level key: {key}")

    # Helper to instantiate entities
    def _instantiate(mapping: dict, cls):
        result: Dict[str, object] = {}
        for entity_id, attrs in mapping.items():
            if not isinstance(attrs, dict):
                raise ValueError(f"Attributes for {entity_id} must be a mapping.")
            result[entity_id] = cls(**attrs)
        return result

    rooms = _instantiate(data.get("rooms", {}), Room)
    items = _instantiate(data.get("items", {}), Item)
    npcs = _instantiate(data.get("npcs", {}), NPC)
    monsters = _instantiate(data.get("monsters", {}), Monster)

    return rooms, items, npcs, monsters
