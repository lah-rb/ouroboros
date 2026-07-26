import json
from typing import Optional
from models import Player, GameState, EquipmentSlot


class GameStateManager:
    def __init__(self, initial_state: GameState):
        self.state = initial_state

    def get_state(self) -> GameState:
        return self.state

    def update_player_health(self, new_health: int):
        self.state.player.health = max(0, min(self.state.player.max_health, new_health))

    def add_to_inventory(self, item_id: str):
        if item_id not in self.state.inventory:
            self.state.inventory.append(item_id)

    def remove_from_inventory(self, item_id: str):
        if item_id in self.state.inventory:
            self.state.inventory.remove(item_id)

    def equip_item(self, slot: EquipmentSlot, item_id: str):
        self.state.equipped[str(slot)] = item_id

    def unequip_item(self, slot: EquipmentSlot):
        if str(slot) in self.state.equipped:
            del self.state.equipped[str(slot)]

    def move_player(self, new_room_id: str):
        self.state.current_room_id = new_room_id

    def remove_item_from_room(self, room_id: str, item_id: str):
        if room_id in self.state.room_states:
            if item_id in self.state.room_states[room_id]["items"]:
                self.state.room_states[room_id]["items"].remove(item_id)

    def add_item_to_room(self, room_id: str, item_id: str):
        if room_id not in self.state.room_states:
            self.state.room_states[room_id] = {"items": [], "npcs": [], "monster": None}
        if item_id not in self.state.room_states[room_id]["items"]:
            self.state.room_states[room_id]["items"].append(item_id)

    def remove_monster_from_room(self, room_id: str):
        if room_id in self.state.room_states:
            self.state.room_states[room_id]["monster"] = None

    def add_defeated_monster(self, monster_id: str):
        self.state.defeated_monsters.add(monster_id)

    def advance_npc_dialogue(self, npc_id: str):
        if npc_id in self.state.npc_dialogue_progress:
            self.state.npc_dialogue_progress[npc_id] += 1
        else:
            self.state.npc_dialogue_progress[npc_id] = 0

    def set_boss_defeated(self):
        self.state.boss_defeated = True


def save_game(state: GameState, file_path: str = "savegame.json") -> None:
    save_data = {
        "player": {
            "health": state.player.health,
            "max_health": state.player.max_health,
            "base_attack": state.player.base_attack,
            "base_defense": state.player.base_defense,
            "inventory": state.inventory,
            "equipped": {k: v for k, v in state.equipped.items()},
        },
        "current_room_id": state.current_room_id,
        "room_states": {
            rid: {
                "items": rstate["items"],
                "npcs": rstate["npcs"],
                "monster": rstate["monster"],
            }
            for rid, rstate in state.room_states.items()
        },
        "npc_dialogue_progress": state.npc_dialogue_progress,
        "defeated_monsters": list(state.defeated_monsters),
        "boss_defeated": state.boss_defeated,
    }
    with open(file_path, "w") as f:
        json.dump(save_data, f, indent=2)


def load_game(file_path: str = "savegame.json") -> Optional[GameState]:
    try:
        with open(file_path, "r") as f:
            save_data = json.load(f)

        player_data = save_data["player"]
        player = Player(
            health=player_data["health"],
            max_health=player_data["max_health"],
            base_attack=player_data.get("base_attack", 5),
            base_defense=player_data.get("base_defense", 2),
            inventory=player_data.get("inventory", []),
            equipped={
                EquipmentSlot[k]: v for k, v in player_data.get("equipped", {}).items()
            },
        )

        state = GameState(
            player=player,
            current_room_id=save_data["current_room_id"],
            inventory=player_data["inventory"],
            equipped=player_data.get("equipped", {}),
            room_states=save_data["room_states"],
            npc_dialogue_progress=save_data.get("npc_dialogue_progress", {}),
            defeated_monsters=set(save_data.get("defeated_monsters", [])),
            boss_defeated=save_data.get("boss_defeated", False),
        )
        return state
    except (FileNotFoundError, json.JSONDecodeError):
        return None
