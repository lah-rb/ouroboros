#!/usr/bin/env python3
"""
Entry point and CLI interface for the text adventure game.
"""

import sys
from typing import Dict, Any

from engine import GameEngine
from models import GameState, Room, Item, NPC


def load_world_data() -> Dict[str, Any]:
    """
    Load world data from YAML file.

    Returns:
        Dictionary containing rooms, items, and NPCs
    """
    import yaml

    try:
        with open("world_data.yaml", "r") as f:
            data = yaml.safe_load(f)

        # Convert to appropriate structures
        rooms = []
        for room_data in data.get("rooms", []):
            room = Room(
                id=room_data["id"],
                name=room_data["name"],
                description=room_data["description"],
                exits=room_data.get("exits", {}),
                items=room_data.get("items", []),
                npcs=room_data.get("npcs", []),
            )
            rooms.append(room)

        items = []
        for item_data in data.get("items", []):
            item = Item(
                id=item_data["id"],
                name=item_data["name"],
                description=item_data["description"],
                can_take=item_data.get("can_take", True),
                can_use=item_data.get("can_use", False),
            )
            items.append(item)

        npcs = []
        for npc_data in data.get("npcs", []):
            npc = NPC(
                id=npc_data["id"],
                name=npc_data["name"],
                description=npc_data["description"],
                dialogue=npc_data.get("dialogue", {}),
            )
            npcs.append(npc)

        return {"rooms": rooms, "items": items, "npcs": npcs}
    except FileNotFoundError:
        print("Error: world_data.yaml not found")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading world data: {e}")
        sys.exit(1)


def create_initial_state() -> GameState:
    """
    Create the initial game state.

    Returns:
        Initial GameState object
    """
    return GameState(
        current_room="start",
        inventory=[],
        room_descriptions_seen={"start": True},
        completed_actions=[],
    )


def main() -> None:
    """
    Main entry point for the game.
    """
    # Load world data
    world_data = load_world_data()

    # Create initial state
    initial_state = create_initial_state()

    # Create game engine
    engine = GameEngine(world_data, initial_state)

    # Run the game
    engine.run()


if __name__ == "__main__":
    main()
