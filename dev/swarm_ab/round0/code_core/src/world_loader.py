"""
Loads the world definition from a YAML file and converts it into model objects.
"""

import yaml
from typing import Dict, Any

from .models import Room, Item, NPC, Monster


def load_world(yaml_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Reads the YAML file at ``yaml_path`` and returns a dictionary with four keys:
    'rooms', 'items', 'npcs', 'monsters'. Each maps an ID string to the corresponding
    model instance.
    """
    with open(yaml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    rooms = {
        room_data["id"]: Room.from_dict(room_data) for room_data in raw.get("rooms", [])
    }
    items = {
        item_data["id"]: Item.from_dict(item_data) for item_data in raw.get("items", [])
    }
    npcs = {npc_data["id"]: NPC.from_dict(npc_data) for npc_data in raw.get("npcs", [])}
    monsters = {
        mon_data["id"]: Monster.from_dict(mon_data)
        for mon_data in raw.get("monsters", [])
    }

    return {
        "rooms": rooms,
        "items": items,
        "npcs": npcs,
        "monsters": monsters,
    }
