import yaml
from typing import Dict, List
from models import Room, Monster, NPC, Item

def create_world() -> Dict[str, Dict]:
    with open("world.yaml", "r") as f:
        data = yaml.safe_load(f)

    rooms_data = data["rooms"]
    items_data = data["items"]
    monsters_data = data["monsters"]
    npcs_data = data["npcs"]

    # Create items
    items = {}
    for item_id, item_info in items_data.items():
        items[item_id] = Item(
            item_id=item_info["id"],
            name=item_info["name"],
            item_type=item_info["type"],
            description=item_info["description"],
            stats=item_info.get("stats", {})
        )

    # Create monsters
    monsters = {}
    for monster_id, monster_info in monsters_data.items():
        monsters[monster_id] = Monster(
            name=monster_info["name"],
            health=monster_info["health"],
            attack=monster_info["attack"],
            behavior=monster_info["behavior"]
        )

    # Create NPCs
    npcs = {}
    for npc_id, npc_info in npcs_data.items():
        npcs[npc_id] = NPC(
            npc_id=npc_info["id"],
            name=npc_info["name"],
            dialogue=npc_info["dialogue"]
        )

    # Create rooms
    rooms = {}
    for room_id, room_info in rooms_data.items():
        rooms[room_id] = Room(
            room_id=room_info["id"],
            title=room_info["title"],
            description=room_info["description"],
            connections=room_info["connections"],
            items=room_info["items"],
            monsters=room_info["monsters"],
            npcs=room_info["npcs"]
        )

    return {
        "rooms": rooms,
        "items": items,
        "monsters": monsters,
        "npcs": npcs
    }
