import yaml
from typing import Dict
from models import Player, Monster, Item, Room, NPC


def load_world() -> Dict:
    with open("world.yaml", "r") as f:
        data = yaml.safe_load(f)

    rooms = {
        room["id"]: Room(
            id=room["id"],
            title=room["title"],
            description=room["description"],
            exits=room["exits"],
            items=room["items"],
            monsters=room["monsters"],
            npcs=room["npcs"],
        )
        for room in data["rooms"]
    }

    items = {
        item_id: Item(
            name=item["name"],
            item_type=item["type"],
            description=item["description"],
            stats=item.get("stats", {}),
        )
        for item_id, item in data["items"].items()
    }

    monsters = {
        monster_id: Monster(
            name=monster["name"],
            hp=monster["hp"],
            attack=monster["attack"],
            defense=monster["defense"],
            behaviour=monster["behaviour"],
        )
        for monster_id, monster in data["monsters"].items()
    }

    npcs = {
        npc_id: NPC(
            name=npc["name"],
            dialogue=npc["dialogue"],
            talked_once=npc.get("talked_once", False),
        )
        for npc_id, npc in data["npcs"].items()
    }

    return {"rooms": rooms, "items": items, "monsters": monsters, "npcs": npcs}


def load_player() -> Player:
    return Player()
