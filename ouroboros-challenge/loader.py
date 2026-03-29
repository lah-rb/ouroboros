"""YAML loader module for text adventure game engine."""
import yaml
from typing import List, Dict, Any

from models import GameState, Room, Item, NPC


def load_world_data(filepath: str) -> GameState:
    """Load world data from a YAML file and return a GameState.
    
    Args:
        filepath: Path to the YAML file containing world data.
        
    Returns:
        GameState populated with rooms, items, NPCs, and initial player state.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    # Parse rooms
    rooms = {}
    for room_data in data.get('rooms', []):
        room_id = room_data['id']
        connections = {}
        for direction, target_room_id in room_data.get('connections', {}).items():
            connections[direction] = target_room_id
        
        room = Room(
            id=room_id,
            name=room_data['name'],
            description=room_data['description'],
            connections=connections,
            items=room_data.get('items', [])
        )
        rooms[room_id] = room
    
    # Parse items
    items = {}
    for item_data in data.get('items', []):
        item = Item(
            id=item_data['id'],
            name=item_data['name'],
            description=item_data['description'],
            takeable=item_data.get('takeable', True)
        )
        items[item.id] = item
    
    # Parse NPCs
    npcs = {}
    for npc_data in data.get('npcs', []):
        dialogue_nodes = []
        for node in npc_data.get('dialogue', []):
            dialogue_node = {
                'trigger': node['trigger'],
                'response': node['response'],
                'next_node': node.get('next_node', None)
            }
            dialogue_nodes.append(dialogue_node)
        
        npc = NPC(
            id=npc_data['id'],
            name=npc_data['name'],
            description=npc_data['description'],
            dialogue=dialogue_nodes
        )
        npcs[npc.id] = npc
    
    # Create initial game state
    initial_room_id = data.get('initial_room', None)
    if initial_room_id and initial_room_id in rooms:
        player_location = initial_room_id
    else:
        # Fallback to first room if no initial room specified
        player_location = next(iter(rooms.keys())) if rooms else ""
    
    return GameState(
        player_location=player_location,
        inventory=[],
        rooms=rooms,
        items=items,
        npcs=npcs,
        current_dialogue_node=None
    )
