"""
Static world definition loader.
Loads `world.yaml` and builds dictionaries of objects used by the game engine.
"""

import yaml
from pathlib import Path
from typing import Dict

from entities import (
    Item,
    Weapon,
    Armor,
    HealingItem,
    Monster,
    NPC,
    Room,
)


def _load_yaml() -> dict:
    """Read world.yaml from the same directory as this file."""
    yaml_path = Path(__file__).with_name("world.yaml")
    with yaml_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_world() -> dict:
    """
    Load world data from ``world.yaml`` and construct the in‑memory objects.

    Returns a dictionary with keys:
        rooms:   Dict[room_id, Room]
        items:   Dict[item_id, Item]
        monsters:Dict[monster_id, Monster]
        npcs:    Dict[npc_id, NPC]
    """
    raw = _load_yaml()

    # ---- Items ----
    items: Dict[str, Item] = {}
    for itm in raw.get("items", []):
        itm_type = itm["type"]
        stats = itm.get("stats", {})

        if itm_type == "weapon":
            # Weapon attack bonus may be stored under different keys.
            attack_bonus = 0
            for key in ("attack", "damage", "attack_bonus"):
                if key in stats:
                    attack_bonus = int(stats[key])
                    break
            item_obj = Weapon(
                id=itm["id"],
                name=itm["name"],
                description=itm["description"],
                type=itm_type,
                attack_bonus=attack_bonus,
            )
        elif itm_type == "armor":
            # Armor may also have an attack bonus (e.g., magical weapons) – keep existing logic.
            attack_bonus = stats.get("attack", 0)
            item_obj = Armor(
                id=itm["id"],
                name=itm["name"],
                description=itm["description"],
                type=itm_type,
                attack_bonus=attack_bonus,
            )
        elif itm_type == "healing":
            # Retrieve heal amount using fallback keys.
            heal_amount = 0
            for key in ("heal", "heal_amount", "healing"):
                if key in stats:
                    heal_amount = int(stats[key])
                    break
            item_obj = HealingItem(
                id=itm["id"],
                name=itm["name"],
                description=itm["description"],
                type=itm_type,
                heal_amount=heal_amount,
            )
        else:  # generic item (e.g., key)
            item_obj = Item(
                id=itm["id"],
                name=itm["name"],
                description=itm["description"],
                type=itm_type,
            )
        items[item_obj.id] = item_obj

    # ---- NPCs ----
    npcs: Dict[str, NPC] = {}
    for n in raw.get("npcs", []):
        npc_obj = NPC(
            id=n["id"],
            name=n["name"],
            dialogue=n.get("dialogue", {}),
            triggers=n.get("triggers", {}),
        )
        npcs[npc_obj.id] = npc_obj

    # ---- Monsters ----
    monsters: Dict[str, Monster] = {}
    for m in raw.get("monsters", []):
        monster_obj = Monster(
            id=m["id"],
            name=m["name"],
            max_health=m["health"],
            health=m["health"],
            attack=m["attack"],
            behavior=m["behavior"],
            location=m["location"],
        )
        monsters[monster_obj.id] = monster_obj

    # ---- Rooms ----
    rooms: Dict[str, Room] = {}
    for r in raw.get("rooms", []):
        room_obj = Room(
            id=r["id"],
            name=r["name"],
            description=r["description"],
            exits=r.get("exits", {}),
            items=r.get("items", []),
            monsters=r.get("monsters", []),
            npcs=r.get("npcs", []),
        )
        rooms[room_obj.id] = room_obj

    # Populate room monster lists with actual ids from definitions
    for monster in monsters.values():
        if monster.location in rooms:
            rooms[monster.location].monsters.append(monster.id)

    # NPC locations are already reflected via room definitions; no extra linking needed.
    return {
        "rooms": rooms,
        "items": items,
        "monsters": monsters,
        "npcs": npcs,
    }
