import yaml
from models import Room, Item, NPC, Monster, Direction


def load_world(file_path: str) -> dict:
    with open(file_path, "r") as f:
        data = yaml.safe_load(f)
    validate_world_data(data)
    rooms = {}
    for rid, rdata in data["rooms"].items():
        connections = {}
        for dname, target in rdata.get("connections", {}).items():
            connections[dname] = target
        rooms[rid] = Room(
            id=rid,
            name=rdata["name"],
            description=rdata["description"],
            connections=connections,
            items=rdata.get("items", []),
            npcs=rdata.get("npcs", []),
            monsters=rdata.get("monsters", []),
        )
    items = {}
    for iid, idata in data["items"].items():
        items[iid] = Item(
            id=iid,
            name=idata["name"],
            type=idata["type"],
            stats=idata.get("stats", {}),
            description=idata.get("description", ""),
        )
    npcs = {}
    for nid, ndata in data["npcs"].items():
        npcs[nid] = NPC(
            id=nid,
            name=ndata["name"],
            dialogue=ndata.get("dialogue", []),
            quest_flags=ndata.get("quest_flags", []),
        )
    monsters = {}
    for mid, mdata in data["monsters"].items():
        monsters[mid] = Monster(
            id=mid,
            name=mdata["name"],
            health=mdata["health"],
            attack=mdata["attack"],
            behavior=mdata["behavior"],
            loot=mdata.get("loot", []),
            phases=mdata.get("phases", []),
        )
    return {
        "rooms": rooms,
        "items": items,
        "npcs": npcs,
        "monsters": monsters,
        "start_room": data["start_room"],
    }


def validate_world_data(data: dict) -> None:
    required_top = ["rooms", "items", "npcs", "monsters", "start_room"]
    for key in required_top:
        assert key in data, f"Missing top-level key: {key}"
    assert isinstance(data["rooms"], dict)
    assert isinstance(data["items"], dict)
    assert isinstance(data["npcs"], dict)
    assert isinstance(data["monsters"], dict)
    assert isinstance(data["start_room"], str)
    # Validate rooms
    for rid, room in data["rooms"].items():
        assert "name" in room and "description" in room
        assert "connections" in room and isinstance(room["connections"], dict)
        assert "items" in room and isinstance(room["items"], list)
        assert "npcs" in room and isinstance(room["npcs"], list)
        assert "monsters" in room and isinstance(room["monsters"], list)
    # Validate items
    for iid, item in data["items"].items():
        assert "name" in item and "type" in item
        assert item["type"] in ("weapon", "armor", "healing", "key")
        assert "stats" in item and isinstance(item["stats"], dict)
    # Validate npcs
    for nid, npc in data["npcs"].items():
        assert "name" in npc
        assert "dialogue" in npc and isinstance(npc["dialogue"], list)
        assert "quest_flags" in npc and isinstance(npc["quest_flags"], list)
    # Validate monsters
    for mid, monster in data["monsters"].items():
        assert (
            "name" in monster
            and "health" in monster
            and "attack" in monster
            and "behavior" in monster
        )
        assert monster["behavior"] in ("aggressive", "passive", "guard")
        assert "loot" in monster and isinstance(monster["loot"], list)
        if "phases" in monster:
            assert isinstance(monster["phases"], list)
            for phase in monster["phases"]:
                assert "health_threshold" in phase
                assert isinstance(phase["health_threshold"], int)
