from typing import Dict, List, Optional
from models import Player, Item, Monster, NPC, Room

class GameState:
    def __init__(self, player: Player, inventory: List[Item], equipment: Dict[str, Item], location: Room):
        self.player = player
        self.inventory = inventory
        self.equipment = equipment
        self.location = location
        self.completed_monsters: List[str] = []  # monster_ids
        self.dialogue_progress: Dict[str, str] = {}  # {npc_id: condition}

    def add_to_inventory(self, item: Item) -> None:
        # Check if item is already in inventory to prevent duplicates
        if not any(i.id == item.id for i in self.inventory):
            self.inventory.append(item)

    def remove_from_inventory(self, item_id: str) -> Optional[Item]:
        for i, item in enumerate(self.inventory):
            if item.id == item_id:
                return self.inventory.pop(i)
        return None

    def equip_item(self, item: Item) -> bool:
        if item.type == "weapon":
            self.equipment["weapon"] = item
            return True
        elif item.type == "armor":
            self.equipment["armor"] = item
            return True
        return False

    def unequip_item(self, item_type: str) -> Optional[Item]:
        if item_type in self.equipment:
            return self.equipment.pop(item_type)
        return None

    def move_to_room(self, room: Room) -> None:
        self.location = room

    def mark_monster_completed(self, monster_id: str) -> None:
        if monster_id not in self.completed_monsters:
            self.completed_monsters.append(monster_id)

    def set_dialogue_progress(self, npc_id: str, condition: str) -> None:
        self.dialogue_progress[npc_id] = condition

    def get_dialogue_condition(self, npc_id: str) -> Optional[str]:
        return self.dialogue_progress.get(npc_id)
