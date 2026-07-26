import yaml
from typing import Dict, Any, Optional
from models import Room, Item, Weapon, Armor, HealingItem, NPC, Monster, Boss


def _create_item(item_data: Dict[str, Any]) -> Item:
    item_type = item_data.get("type", "").lower()
    if item_type == "weapon":
        return Weapon(
            id=item_data["id"],
            name=item_data["name"],
            description=item_data["description"],
            type=item_type,
            damage=item_data.get("damage", 0),
        )
    elif item_type == "armor":
        return Armor(
            id=item_data["id"],
            name=item_data["name"],
            description=item_data["description"],
            type=item_type,
            defense=item_data.get("defense", 0),
        )
    elif item_type == "healing":
        return HealingItem(
            id=item_data["id"],
            name=item_data["name"],
            description=item_data["description"],
            type=item_type,
            heal_amount=item_data.get("heal_amount", 0),
        )
    else:
        return Item(
            id=item_data["id"],
            name=item_data["name"],
            description=item_data["description"],
            type=item_type,
        )


def load_world(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r") as f:
        world_data = yaml.safe_load(f)

    # Load items
    items: Dict[str, Item] = {}
    for item_id, item_data in world_data.get("items", {}).items():
        items[item_id] = _create_item(item_data)

    # Load NPCs
    npcs: Dict[str, NPC] = {}
    for npc_id, npc_data in world_data.get("npcs", {}).items():
        npcs[npc_id] = NPC(
            id=npc_id,
            name=npc_data["name"],
            description=npc_data["description"],
            dialogue=npc_data.get("dialogue", []),
        )

    # Load monsters
    monsters: Dict[str, Monster] = {}
    for monster_id, monster_data in world_data.get("monsters", {}).items():
        monsters[monster_id] = Monster(
            id=monster_id,
            name=monster_data["name"],
            description=monster_data["description"],
            health=monster_data.get("health", 0),
            attack=monster_data.get("attack", 0),
            defense=monster_data.get("defense", 0),
        )

    # Load boss
    boss_data = world_data.get("boss", {})
    boss: Optional[Boss] = None
    if boss_data:
        boss = Boss(
            id=boss_data["id"],
            name=boss_data["name"],
            description=boss_data["description"],
            health=boss_data.get("health", 0),
            attack=boss_data.get("attack", 0),
            defense=boss_data.get("defense", 0),
            phase2_health_threshold=boss_data.get("phase2_health_threshold", 0),
            phase2_attack=boss_data.get("phase2_attack", 0),
            weakness_item=boss_data.get("weakness_item"),
        )

    # Load rooms
    rooms: Dict[str, Room] = {}
    for room_id, room_data in world_data.get("rooms", {}).items():
        rooms[room_id] = Room(
            id=room_id,
            title=room_data["title"],
            description=room_data["description"],
            exits=room_data.get("exits", {}),
            items=room_data.get("items", []),
            npcs=room_data.get("npcs", []),
            monster=room_data.get("monster"),
            boss=room_data.get("boss"),
        )

    return {
        "rooms": rooms,
        "items": items,
        "npcs": npcs,
        "monsters": monsters,
        "boss": boss,
        "start_room_id": world_data["start_room_id"],
    }
