import yaml
from typing import Dict, Any

from models import Room, Item, NPC


def load_world_data(filepath: str) -> Dict[str, Any]:
    """
    Load world data from a YAML file and return structured data matching
    the data contract.

    Args:
        filepath: Path to the YAML file containing world data.

    Returns:
        Dictionary with keys 'rooms', 'items', 'npcs' containing
        corresponding model instances.

    Raises:
        FileNotFoundError: If the file does not exist.
        yaml.YAMLError: If the YAML is malformed.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Validate required top-level keys
    required_keys = {"rooms", "items", "npcs"}
    if not isinstance(data, dict):
        raise ValueError("YAML file must contain a top-level dictionary")

    missing_keys = required_keys - set(data.keys())
    if missing_keys:
        raise ValueError(f"Missing required top-level keys: {missing_keys}")

    # Process rooms
    rooms = []
    for room_data in data["rooms"]:
        # Validate required room fields
        required_room_fields = {"id", "name", "description", "exits", "items", "npcs"}
        missing_room_fields = required_room_fields - set(room_data.keys())
        if missing_room_fields:
            raise ValueError(f"Room missing required fields: {missing_room_fields}")

        # Ensure exits is a dict (direction -> room_id)
        exits = room_data["exits"]
        if not isinstance(exits, dict):
            raise ValueError(f"Room '{room_data['id']}' exits must be a dictionary")

        # Process items and npcs lists
        room_items = room_data["items"]
        room_npcs = room_data["npcs"]

        if not isinstance(room_items, list):
            raise ValueError(f"Room '{room_data['id']}' items must be a list")
        if not isinstance(room_npcs, list):
            raise ValueError(f"Room '{room_data['id']}' npcs must be a list")

        room = Room(
            id=room_data["id"],
            name=room_data["name"],
            description=room_data["description"],
            exits=exits,
            items=room_items,
            npcs=room_npcs,
        )
        rooms.append(room)

    # Process items
    items = []
    for item_data in data["items"]:
        # Validate required item fields
        required_item_fields = {"id", "name", "description", "can_take", "can_use"}
        missing_item_fields = required_item_fields - set(item_data.keys())
        if missing_item_fields:
            raise ValueError(f"Item missing required fields: {missing_item_fields}")

        item = Item(
            id=item_data["id"],
            name=item_data["name"],
            description=item_data["description"],
            can_take=bool(item_data["can_take"]),
            can_use=bool(item_data["can_use"]),
        )
        items.append(item)

    # Process NPCs
    npcs = []
    for npc_data in data["npcs"]:
        # Validate required NPC fields
        required_npc_fields = {"id", "name", "description", "dialogue"}
        missing_npc_fields = required_npc_fields - set(npc_data.keys())
        if missing_npc_fields:
            raise ValueError(f"NPC missing required fields: {missing_npc_fields}")

        # Validate dialogue structure
        dialogue = npc_data["dialogue"]
        if not isinstance(dialogue, dict):
            raise ValueError(f"NPC '{npc_data['id']}' dialogue must be a dictionary")

        # Ensure each dialogue topic has a list of responses
        for topic, responses in dialogue.items():
            if not isinstance(responses, list):
                raise ValueError(
                    f"NPC '{npc_data['id']}' dialogue topic '{topic}' must have a list of responses"
                )

        npc = NPC(
            id=npc_data["id"],
            name=npc_data["name"],
            description=npc_data["description"],
            dialogue=dialogue,
        )
        npcs.append(npc)

    return {"rooms": rooms, "items": items, "npcs": npcs}
