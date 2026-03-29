"""Loader for game world data from YAML files."""

import yaml
from typing import Dict, List
from models import Room, Item, NPC


class WorldLoader:
    """Loads and parses world data from YAML files."""
    
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self.items: Dict[str, Item] = {}
        self.npcs: Dict[str, NPC] = {}
    
    def load_from_yaml(self, filepath: str) -> None:
        """Load world data from a YAML file."""
        with open(filepath, 'r') as f:
            data = yaml.safe_load(f)
        
        self._load_rooms(data.get('rooms', []))
        self._load_items(data.get('items', []))
        self._load_npcs(data.get('npcs', []))
    
    def _load_rooms(self, rooms_data: List[dict]) -> None:
        """Load room data from parsed YAML."""
        for room_data in rooms_data:
            room = Room(
                id=room_data['id'],
                name=room_data['name'],
                description=room_data['description'],
                connections=room_data.get('connections', {}),
                items=room_data.get('items', []),
                npcs=room_data.get('npcs', [])
            )
            self.rooms[room.id] = room
    
    def _load_items(self, items_data: List[dict]) -> None:
        """Load item data from parsed YAML."""
        for item_data in items_data:
            item = Item(
                id=item_data['id'],
                name=item_data['name'],
                description=item_data['description'],
                can_take=item_data.get('can_take', True),
                location=item_data.get('location')
            )
            self.items[item.id] = item
    
    def _load_npcs(self, npcs_data: List[dict]) -> None:
        """Load NPC data from parsed YAML."""
        for npc_data in npcs_data:
            npc = NPC(
                id=npc_data['id'],
                name=npc_data['name'],
                description=npc_data['description'],
                room_id=npc_data['room_id'],
                dialogue_tree=npc_data.get('dialogue_tree', {}),
                current_state=npc_data.get('current_state', 'intro')
            )
            self.npcs[npc.id] = npc
    
    def get_room(self, room_id: str) -> Room:
        """Get a room by ID."""
        return self.rooms[room_id]
    
    def get_item(self, item_id: str) -> Item:
        """Get an item by ID."""
        return self.items[item_id]
    
    def get_npc(self, npc_id: str) -> NPC:
        """Get an NPC by ID."""
        return self.npcs[npc_id]
