import json
from typing import Dict, List, Optional
from game_state import GameState

def save_game(game_state: GameState) -> None:
    save_data = {
        "player": {
            "health": game_state.player.health,
            "attack": game_state.player.attack,
            "defense": game_state.player.defense,
            "equipped": {
                "weapon": game_state.equipment.get("weapon", {}).id if "weapon" in game_state.equipment else None,
                "armor": game_state.equipment.get("armor", {}).id if "armor" in game_state.equipment else None
            }
        },
        "inventory": [item.id for item in game_state.inventory],
        "location": game_state.location.id,
        "completed_monsters": game_state.completed_monsters,
        "dialogue_progress": game_state.dialogue_progress
    }

    with open("savegame.json", "w") as f:
        json.dump(save_data, f)

def load_game() -> Optional[GameState]:
    try:
        with open("savegame.json", "r") as f:
            save_data = json.load(f)
    except FileNotFoundError:
        return None

    # Reconstruct GameState from saved data
    player_data = save_data.get("player", {})
    inventory_data = save_data.get("inventory", [])
    equipment_data = save_data.get("equipment", {})
    location_id = save_data.get("location")

    # In a real implementation, you would use world_data to get references to rooms, items, etc.
    # For now, we'll return None to indicate the function is not yet fully implemented.
    return None
