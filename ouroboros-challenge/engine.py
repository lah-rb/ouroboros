"""Game engine module for text adventure game.

This module provides the GameEngine class that manages game state,
processes player commands, and coordinates interactions between
game components.
"""

from typing import Dict, List, Optional, Tuple

from models import GameState, Room, Item, NPC
from parser import parse_command, Command, CommandType


class GameEngine:
    """Main game engine that processes commands and manages game state."""

    def __init__(self, world_data: GameState):
        """Initialize the game engine with world data.

        Args:
            world_data: The loaded game world state containing rooms,
                items, NPCs, and initial player state.
        """
        self.world = world_data
        self.player_location = world_data.player_location
        self.inventory: List[str] = list(world_data.inventory)
        self.room_states: Dict[str, dict] = dict(world_data.room_states)
        self.npc_states: Dict[str, str] = dict(world_data.npc_states)
        self.game_over = False

    def process_command(self, command_str: str) -> str:
        """Process a player command and return response text.

        Args:
            command_str: Raw command string from the player.

        Returns:
            Response text describing the result of the command.
        """
        if self.game_over:
            return "The game has ended. Use 'quit' to exit."

        command = parse_command(command_str)

        if command.type == CommandType.MOVE:
            return self._handle_move(command)
        elif command.type == CommandType.TAKE:
            return self._handle_take(command)
        elif command.type == CommandType.DROP:
            return self._handle_drop(command)
        elif command.type == CommandType.USE:
            return self._handle_use(command)
        elif command.type == CommandType.EXAMINE:
            return self._handle_examine(command)
        elif command.type == CommandType.TALK:
            return self._handle_talk(command)
        elif command.type == CommandType.LOOK:
            return self._handle_look()
        elif command.type == CommandType.HELP:
            return self._handle_help()
        elif command.type == CommandType.QUIT:
            return self._handle_quit()
        else:
            return "I don't understand that command. Type 'help' for available commands."

    def _handle_move(self, command: Command) -> str:
        """Handle movement command."""
        direction = command.args[0] if command.args else None

        if not direction:
            return "Move in which direction? (north, south, east, west)"

        current_room = self._get_room(self.player_location)
        if not current_room:
            return f"Error: Current room '{self.player_location}' not found."

        # Check for exit in the specified direction
        if direction in current_room.exits:
            new_room_id = current_room.exits[direction]
            self.player_location = new_room_id
            return self._describe_room(new_room_id)
        else:
            return f"You can't go {direction} from here."

    def _handle_take(self, command: Command) -> str:
        """Handle take item command."""
        item_id = command.args[0] if command.args else None

        if not item_id:
            return "Take which item?"

        current_room = self._get_room(self.player_location)
        if not current_room:
            return f"Error: Current room '{self.player_location}' not found."

        # Check if item exists in room
        if item_id in current_room.items:
            # Remove item from room
            current_room.items.remove(item_id)
            # Add to inventory
            self.inventory.append(item_id)
            item = self._get_item(item_id)
            return f"You picked up the {item.name}."
        else:
            return "You don't see that here."

    def _handle_drop(self, command: Command) -> str:
        """Handle drop item command."""
        item_id = command.args[0] if command.args else None

        if not item_id:
            return "Drop which item?"

        # Check if player has the item
        if item_id in self.inventory:
            # Remove from inventory
            self.inventory.remove(item_id)
            # Add to current room
            current_room = self._get_room(self.player_location)
            if current_room:
                current_room.items.append(item_id)
                item = self._get_item(item_id)
                return f"You dropped the {item.name}."
            else:
                return "Error: Current room not found."
        else:
            return "You don't have that item."

    def _handle_use(self, command: Command) -> str:
        """Handle use item command."""
        if len(command.args) < 2:
            return "Use which item on what? (e.g., 'use key door')"

        item_id = command.args[0]
        target = command.args[1]

        # Check if player has the item
        if item_id not in self.inventory:
            return f"You don't have the {item_id}."

        # Handle special use cases based on current room and items
        current_room = self._get_room(self.player_location)
        if not current_room:
            return f"Error: Current room '{self.player_location}' not found."

        # Example: Use key on door
        if item_id == "key" and target in ["door", "exit"]:
            if "locked_door" in current_room.properties:
                current_room.properties.remove("locked_door")
                self.room_states[current_room.id]["door_unlocked"] = True
                return "You use the key to unlock the door. The path forward is now clear!"
            else:
                return "There's nothing here that needs a key."

        # Default response for other items
        item = self._get_item(item_id)
        return f"You try to use the {item.name} on the {target}, but nothing happens."

    def _handle_examine(self, command: Command) -> str:
        """Handle examine command."""
        target = command.args[0] if command.args else None

        if not target:
            return "Examine what?"

        # Check inventory first
        for item_id in self.inventory:
            if item_id == target or target in self._get_item(item_id).name.lower():
                item = self._get_item(item_id)
                return f"{item.name}: {item.description}"

        # Check current room items
        current_room = self._get_room(self.player_location)
        if current_room:
            for item_id in current_room.items:
                if item_id == target or target in self._get_item(item_id).name.lower():
                    item = self._get_item(item_id)
                    return f"{item.name}: {item.description}"

        # Check NPCs
        if current_room:
            for npc_id in current_room.npcs:
                if npc_id == target or target in self._get_npc(npc_id).name.lower():
                    npc = self._get_npc(npc_id)
                    return f"{npc.name}: {npc.description}"

        # Check room itself
        if target in ["room", "area", "surroundings"]:
            return current_room.description

        return "You don't see that here."

    def _handle_talk(self, command: Command) -> str:
        """Handle talk to NPC command."""
        npc_id = command.args[0] if command.args else None

        if not npc_id:
            return "Talk to whom?"

        current_room = self._get_room(self.player_location)
        if not current_room:
            return f"Error: Current room '{self.player_location}' not found."

        # Check if NPC is in the current room
        if npc_id in current_room.npcs:
            npc = self._get_npc(npc_id)
            # Get current dialogue node for this NPC
            dialogue_node = self.npc_states.get(npc_id, "start")

            # Get response based on dialogue state
            response = self._get_dialogue_response(npc, dialogue_node)

            return f"{npc.name} says: \"{response}\""
        else:
            return "You don't see that person here."

    def _handle_look(self) -> str:
        """Handle look command to describe current room."""
        return self._describe_room(self.player_location)

    def _handle_help(self) -> str:
        """Handle help command to show available commands."""
        return (
            "Available commands:\n"
            "- go [direction]: Move north, south, east, or west\n"
            "- take [item]: Pick up an item\n"
            "- drop [item]: Drop an item from inventory\n"
            "- use [item] [target]: Use an item on something\n"
            "- examine [object]: Look closely at something\n"
            "- talk [npc]: Talk to a character\n"
            "- look: Examine your current surroundings\n"
            "- help: Show this help message\n"
            "- quit: Exit the game"
        )

    def _handle_quit(self) -> str:
        """Handle quit command."""
        self.game_over = True
        return "Thanks for playing! You can save your progress before exiting."

    def _describe_room(self, room_id: str) -> str:
        """Generate a description of the specified room."""
        room = self._get_room(room_id)
        if not room:
            return f"Error: Room '{room_id}' not found."

        description = [f"{room.name}\n{room.description}"]

        # List items in room
        if room.items:
            item_names = [self._get_item(item_id).name for item_id in room.items]
            description.append(f"\nYou see: {', '.join(item_names)}")

        # List NPCs in room
        if room.npcs:
            npc_names = [self._get_npc(npc_id).name for npc_id in room.npcs]
            description.append(f"\nCharacters here: {', '.join(npc_names)}")

        # List exits
        exits = list(room.exits.keys())
        if exits:
            description.append(f"\nExits: {', '.join(exits)}")

        return "\n".join(description)

    def _get_room(self, room_id: str) -> Optional[Room]:
        """Get room by ID."""
        for room in self.world.rooms:
            if room.id == room_id:
                return room
        return None

    def _get_item(self, item_id: str) -> Optional[Item]:
        """Get item by ID."""
        for item in self.world.items:
            if item.id == item_id:
                return item
        return None

    def _get_npc(self, npc_id: str) -> Optional[NPC]:
        """Get NPC by ID."""
        for npc in self.world.npcs:
            if npc.id == npc_id:
                return npc
        return None

    def _get_dialogue_response(self, npc: NPC, dialogue_node: str) -> str:
        """Get response for an NPC at a given dialogue node."""
        # Find the dialogue node in the NPC's dialogue tree
        if dialogue_node in npc.dialogue:
            return npc.dialogue[dialogue_node]
        else:
            # Default fallback
            return "I have nothing more to say."

    def get_state(self) -> GameState:
        """Get current game state for saving."""
        return GameState(
            player_location=self.player_location,
            inventory=list(self.inventory),
            room_states=dict(self.room_states),
            npc_states=dict(self.npc_states)
        )
