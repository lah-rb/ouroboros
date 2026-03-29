"""Main game engine for the text adventure."""

import json
from typing import Optional
from models import GameState
from loader import WorldLoader


class GameEngine:
    """Core game logic and state management."""
    
    def __init__(self, world_loader: WorldLoader):
        self.world = world_loader
        self.state = GameState(player_room="start")
        self.parser = None  # Will be set by main
    
    def move(self, direction: str) -> str:
        """Attempt to move player in the given direction."""
        current_room = self.world.get_room(self.state.player_room)
        
        if direction not in current_room.connections:
            return f"You can't go {direction} from here."
        
        new_room_id = current_room.connections[direction]
        self.state.player_room = new_room_id
        
        new_room = self.world.get_room(new_room_id)
        return self._describe_room(new_room)
    
    def take_item(self, item_name: str) -> str:
        """Attempt to pick up an item."""
        # Find item by name in current room
        current_room = self.world.get_room(self.state.player_room)
        
        for item_id in current_room.items:
            item = self.world.get_item(item_id)
            if item.name.lower() == item_name.lower():
                if not item.can_take:
                    return f"You can't take the {item.name}."
                
                if item_id in self.state.inventory:
                    return f"You already have the {item.name}."
                
                item.in_inventory = True
                self.state.inventory.append(item_id)
                current_room.items.remove(item_id)
                
                return f"You picked up the {item.name}."
        
        return f"I don't see a {item_name} here."
    
    def drop_item(self, item_name: str) -> str:
        """Attempt to drop an item."""
        for item_id in self.state.inventory:
            item = self.world.get_item(item_id)
            if item.name.lower() == item_name.lower():
                item.in_inventory = False
                self.state.inventory.remove(item_id)
                self.world.get_room(self.state.player_room).items.append(item_id)
                
                return f"You dropped the {item.name}."
        
        return f"You don't have a {item_name}."
    
    def use_item(self, item_name: str) -> str:
        """Attempt to use an item."""
        for item_id in self.state.inventory:
            item = self.world.get_item(item_id)
            if item.name.lower() == item_name.lower():
                return f"You try to use the {item.name}, but nothing happens."
        
        return f"You don't have a {item_name} to use."
    
    def examine(self, target: str) -> str:
        """Examine an item or the current room."""
        if not target:
            current_room = self.world.get_room(self.state.player_room)
            return self._describe_room(current_room)
        
        # Check inventory
        for item_id in self.state.inventory:
            item = self.world.get_item(item_id)
            if item.name.lower() == target.lower():
                return f"{item.name}: {item.description}"
        
        # Check room items
        current_room = self.world.get_room(self.state.player_room)
        for item_id in current_room.items:
            item = self.world.get_item(item_id)
            if item.name.lower() == target.lower():
                return f"{item.name}: {item.description}"
        
        return f"I don't see a {target} here."
    
    def talk_to_npc(self, npc_name: str) -> str:
        """Initiate conversation with an NPC."""
        current_room = self.world.get_room(self.state.player_room)
        
        for npc_id in current_room.npcs:
            npc = self.world.get_npc(npc_id)
            if npc.name.lower() == npc_name.lower():
                return self._get_npc_response(npc)
        
        return f"I don't see {npc_name} here."
    
    def _get_npc_response(self, npc) -> str:
        """Get NPC response based on current state."""
        # Simple dialogue system - in a real implementation, this would be more sophisticated
        if npc.current_state == "intro":
            response = f"{npc.name} says: 'Hello there! I'm {npc.name}.'"
            npc.current_state = "friendly"
        elif npc.current_state == "friendly":
            response = f"{npc.name} says: 'I've been waiting for someone to talk to!'"
        else:
            response = f"{npc.name} looks at you silently."
        
        return response
    
    def show_inventory(self) -> str:
        """Show player's inventory."""
        if not self.state.inventory:
            return "You are carrying nothing."
        
        items = []
        for item_id in self.state.inventory:
            item = self.world.get_item(item_id)
            items.append(f"- {item.name}")
        
        return "You are carrying:\n" + "\n".join(items)
    
    def show_help(self) -> str:
        """Show help information."""
        return """
Available commands:
  go [direction] - Move in a direction (north, south, east, west)
  take [item]    - Pick up an item
  drop [item]    - Drop an item from inventory
  use [item]     - Use an item
  examine [thing] - Look at something closely
  talk [npc]     - Talk to a character
  inventory      - Show your inventory
  look           - Look around the current room
  help           - Show this help message
  quit           - Exit the game
"""
    
    def describe_current_room(self) -> str:
        """Get description of current room."""
        return self._describe_room(self.world.get_room(self.state.player_room))
    
    def _describe_room(self, room) -> str:
        """Generate a descriptive room string."""
        output = [f"\n{room.name}", "=" * len(room.name), room.description]
        
        # List items
        if room.items:
            item_names = [self.world.get_item(item_id).name for item_id in room.items]
            output.append(f"\nYou see: {', '.join(item_names)}")
        
        # List NPCs
        if room.npcs:
            npc_names = [self.world.get_npc(npc_id).name for npc_id in room.npcs]
            output.append(f"\nCharacters here: {', '.join(npc_names)}")
        
        # List exits
        exits = list(room.connections.keys())
        if exits:
            output.append(f"\nExits: {', '.join(exits)}")
        
        return "\n".join(output)
    
    def save_game(self, filepath: str) -> None:
        """Save game state to JSON file."""
        save_data = {
            "player_room": self.state.player_room,
            "inventory": self.state.inventory,
            "room_states": self.state.room_states,
            "npc_states": {npc_id: npc.current_state for npc_id, npc in self.world.npcs.items()},
            "completed_actions": self.state.completed_actions
        }
        
        with open(filepath, 'w') as f:
            json.dump(save_data, f, indent=2)
    
    def load_game(self, filepath: str) -> bool:
        """Load game state from JSON file."""
        try:
            with open(filepath, 'r') as f:
                save_data = json.load(f)
            
            self.state.player_room = save_data["player_room"]
            self.state.inventory = save_data["inventory"]
            self.state.room_states = save_data.get("room_states", {})
            self.state.completed_actions = save_data.get("completed_actions", [])
            
            # Restore NPC states
            npc_states = save_data.get("npc_states", {})
            for npc_id, state in npc_states.items():
                if npc_id in self.world.npcs:
                    self.world.npcs[npc_id].current_state = state
            
            return True
        except (FileNotFoundError, KeyError):
            return False
