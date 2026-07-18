"""Load world definition from YAML files and instantiate model objects."""

from __future__ import annotations
import yaml
from typing import Dict, Any
from src.models import Room, Item, NPC, Monster


def load_world(yaml_path: str) -> Dict[str, Any]:
    """Parse a world description file and create model instances.

    The returned dictionary contains the keys ``rooms``, ``items``,
    ``npcs``, ``monsters`` and ``player_start`` exactly as described in
    the data contract. Each collection holds fully‑instantiated objects
    of the corresponding model class.

    Args:
        yaml_path: Filesystem path to a YAML file following the world
            schema, or a raw YAML string (used by doctests).

    Returns:
        A mapping with the loaded world data.

    Raises:
        yaml.YAMLError: If the supplied YAML cannot be parsed.
    """
    # Load raw data from YAML. If the path does not exist, treat the
    # argument itself as a YAML document string.
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raw = yaml.safe_load(yaml_path)

    # ------------------------------------------------------------------
    # Helper: resolve a list of ids to actual objects using a lookup dict.
    # ------------------------------------------------------------------
    def resolve_ids(id_list, lookup):
        if not id_list:
            return []
        return [lookup[i] for i in id_list if i in lookup]

    # ------------------------------------------------------------------
    # Build items
    # ------------------------------------------------------------------
    raw_items = raw.get("items", [])
    items_by_id: Dict[str, Item] = {}
    for itm in raw_items:
        item_obj = Item(
            id=itm["id"],
            name=itm["name"],
            description=itm.get("description", ""),
            type=itm.get("type", ""),
        )
        items_by_id[item_obj.id] = item_obj

    # ------------------------------------------------------------------
    # Build monsters
    # ------------------------------------------------------------------
    raw_monsters = raw.get("monsters", [])
    monsters_by_id: Dict[str, Monster] = {}
    for mon in raw_monsters:
        monster_obj = Monster(
            id=mon["id"],
            name=mon["name"],
            health=mon["health"],
            max_health=mon["max_health"],
            attack=mon["attack"],
            description=mon.get("description", ""),
        )
        monsters_by_id[monster_obj.id] = monster_obj

    # ------------------------------------------------------------------
    # Build NPCs
    # ------------------------------------------------------------------
    raw_npcs = raw.get("npcs", [])
    npcs_by_id: Dict[str, NPC] = {}
    for npc in raw_npcs:
        npc_obj = NPC(
            id=npc["id"],
            name=npc["name"],
            location=npc["location"],
            dialogue=npc.get("dialogue", []),
            dialogue_index=npc.get("dialogue_index", 0),
        )
        npcs_by_id[npc_obj.id] = npc_obj

    # ------------------------------------------------------------------
    # Build rooms, wiring items, npcs and monster references
    # ------------------------------------------------------------------
    raw_rooms = raw.get("rooms", [])
    rooms_by_id: Dict[str, Room] = {}
    for rm in raw_rooms:
        room_items = resolve_ids(rm.get("items", []), items_by_id)
        room_npcs = resolve_ids(rm.get("npcs", []), npcs_by_id)

        monster_ref = None
        monster_id = rm.get("monster")
        if monster_id:
            monster_ref = monsters_by_id.get(monster_id)

        room_obj = Room(
            id=rm["id"],
            name=rm["name"],
            description=rm.get("description", ""),
            connections=rm.get("connections", {}),
            items=room_items,
            npcs=room_npcs,
            monster=monster_ref,
        )
        rooms_by_id[room_obj.id] = room_obj

    # ------------------------------------------------------------------
    # Player start information – returned unchanged (could be dict or
    # primitive values as defined by the world schema)
    # ------------------------------------------------------------------
    player_start = raw.get("player_start", {})

    return {
        "rooms": rooms_by_id,
        "items": items_by_id,
        "npcs": npcs_by_id,
        "monsters": monsters_by_id,
        "player_start": player_start,
    }
