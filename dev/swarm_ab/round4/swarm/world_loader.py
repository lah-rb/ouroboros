"""Loads YAML world definitions and builds model objects."""

from __future__ import annotations
import yaml  # type: ignore
from typing import Dict, Any

from models import Room, Item, NPC, Monster, Boss


# ⟦OUROBOROS-SYMBOL load_world⟧ def load_world() -> Dict[str, Dict[str, Any]]:  (body preserved — keep this line)
def load_world() -> Dict[str, Dict[str, Any]]:
    """Parse the world YAML files and return mappings of identifiers to model instances.

    Returns:
        A dictionary with keys ``rooms``, ``items``, ``npcs``,
        ``monsters`` and optionally ``bosses``. Each value is a mapping
        from the object's ``id`` to an instantiated model object.

    Raises:
        FileNotFoundError: If any required YAML file is missing.
        yaml.YAMLError: If a YAML file cannot be parsed.
    """
    # Resolve data files relative to this module's location.
    from pathlib import Path

    base_path = Path(__file__).resolve().parent
    rooms_path = base_path / "rooms.yaml"
    items_path = base_path / "items.yaml"
    npcs_path = base_path / "npcs.yaml"
    monsters_path = base_path / "monsters.yaml"

    # Load YAML files; let FileNotFoundError and yaml.YAMLError propagate.
    with rooms_path.open("r", encoding="utf-8") as f:
        rooms_data = yaml.safe_load(f) or {}
    with items_path.open("r", encoding="utf-8") as f:
        items_data = yaml.safe_load(f) or {}
    with npcs_path.open("r", encoding="utf-8") as f:
        npcs_data = yaml.safe_load(f) or {}
    with monsters_path.open("r", encoding="utf-8") as f:
        monsters_data = yaml.safe_load(f) or {}

    # Build model instance mappings.
    rooms: Dict[str, Room] = {}
    for entry in rooms_data.get("rooms", []):
        room = Room(
            id=entry["id"],
            name=entry["name"],
            description=entry["description"],
            connections=entry["connections"],
            items=entry["items"],
            npcs=entry["npcs"],
            monsters=entry["monsters"],
        )
        rooms[room.id] = room

    items: Dict[str, Item] = {}
    for entry in items_data.get("items", []):
        item = Item(
            id=entry["id"],
            name=entry["name"],
            description=entry["description"],
            type=entry["type"],
            attack_bonus=entry["attack_bonus"],
            defense_bonus=entry["defense_bonus"],
            heal_amount=entry["heal_amount"],
        )
        items[item.id] = item

    npcs: Dict[str, NPC] = {}
    for entry in npcs_data.get("npcs", []):
        npc = NPC(
            id=entry["id"],
            name=entry["name"],
            description=entry["description"],
            dialogue=entry["dialogue"],
        )
        npcs[npc.id] = npc

    monsters: Dict[str, Monster] = {}
    bosses: Dict[str, Boss] = {}
    for entry in monsters_data.get("monsters", []):
        if "health_phase1" in entry:
            boss = Boss(
                id=entry["id"],
                name=entry["name"],
                description=entry["description"],
                health_phase1=entry["health_phase1"],
                health_phase2=entry["health_phase2"],
                attack_phase1=entry["attack_phase1"],
                attack_phase2=entry["attack_phase2"],
                weakness_item_id=entry["weakness_item_id"],
                location=entry["location"],
            )
            bosses[boss.id] = boss
        else:
            monster = Monster(
                id=entry["id"],
                name=entry["name"],
                description=entry["description"],
                health=entry["health"],
                attack=entry["attack"],
                behavior=entry["behavior"],
                location=entry["location"],
            )
            monsters[monster.id] = monster

    world: Dict[str, Dict[str, Any]] = {
        "rooms": rooms,
        "items": items,
        "npcs": npcs,
        "monsters": monsters,
    }
    if bosses:
        world["bosses"] = bosses
    return world
