from typing import Dict, List, Optional
from parser import parse_command, Command
from models import Direction, Item, NPC, Player
from game_state import GameState
from world import create_world
from combat import CombatEngine

class GameEngine:
    def __init__(self):
        self.world_data = create_world()
        self.game_state = None
        self.running = False

    def initialize_game(self) -> None:
        # Create a new player
        player = Player(health=30, attack=5, defense=2)

        # Start in the entrance_hall room as per world.yaml
        start_room = self.world_data["rooms"].get("entrance_hall")
        if not start_room:
            raise ValueError("Start room 'entrance_hall' not found in world data")

        # Initialize game state
        self.game_state = GameState(
            player=player,
            inventory=[],
            equipment={},
            location=start_room
        )
        self.running = True

    def handle_command(self, command: Command) -> str:
        if not self.game_state:
            return "Game not initialized. Type 'new' to start a new game."

        if command.action == "move":
            if command.direction:
                next_room_id = self.game_state.location.connections.get(command.direction)
                if next_room_id:
                    next_room = self.world_data["rooms"][next_room_id]
                    self.game_state.move_to_room(next_room)
                    return f"You move {command.direction}. {next_room.description}"
                else:
                    return "You can't go that way."
            return "Please specify a direction to move."

        elif command.action == "get":
            if command.target:
                # Find item in current room by item_id
                for item_id in self.game_state.location.items:
                    if item_id == command.target.lower():
                        item = self.world_data["items"][item_id]
                        self.game_state.add_to_inventory(item)
                        return f"You picked up {item.name}."
                return f"There is no {command.target} here."
            return "Get what?"

        elif command.action == "take":
            # Handle 'take' action (alias for 'get')
            if command.target:
                # Find item in current room by item_id
                for item_id in self.game_state.location.items:
                    if item_id == command.target.lower():
                        item = self.world_data["items"][item_id]
                        # Check if item is already in inventory to prevent duplicates
                        if any(i.id == item_id for i in self.game_state.inventory):
                            return f"You already have {item.name}."
                        self.game_state.add_to_inventory(item)
                        return f"You picked up {item.name}."
                return f"There is no {command.target} here."
            return "Take what?"

        elif command.action == "drop":
            if command.target:
                # Find item in inventory by item_id
                for item in self.game_state.inventory:
                    if item.id == command.target.lower():
                        # Remove item from inventory
                        self.game_state.inventory.remove(item)
                        # Add item to current room's items
                        self.game_state.location.items.append(item.id)
                        return f"You dropped {item.name}."
                return f"You don't have {command.target} in your inventory."
            return "Drop what?"

        elif command.action == "use":
            if command.target:
                # Check inventory for item by item_id
                for item in self.game_state.inventory:
                    if item.id == command.target.lower():
                        if item.type == "potion" and item.stats.get("health"):
                            self.game_state.player.health += item.stats["health"]
                            return f"You used {item.name} and restored {item.stats['health']} health."
                        return f"You use {item.name}, but nothing happens."
                return f"You don't have {command.target}."
            return "Use what?"

        elif command.action == "equip":
            if command.target:
                # Check inventory for item by item_id
                for item in self.game_state.inventory:
                    if item.id == command.target.lower():
                        if self.game_state.equip_item(item):
                            return f"You equipped {item.name}."
                        return f"You can't equip {item.name}."
                return f"You don't have {command.target}."
            return "Equip what?"

        elif command.action == "attack":
            # Find monster in current room
            for monster_id in self.game_state.location.monsters:
                monster = self.world_data["monsters"][monster_id]
                combat_engine = CombatEngine(
                    player=self.game_state.player,
                    monster=monster,
                    game_state=self.game_state
                )
                result = combat_engine.fight()
                if "defeated" in result:
                    self.game_state.location.monsters.remove(monster_id)
                    self.game_state.mark_monster_completed(monster_id)
                return result
            return "There's nothing to attack here."

        elif command.action == "talk":
            if command.target:
                # Find NPC in current room by npc_id
                for npc_id in self.game_state.location.npcs:
                    if npc_id == command.target.lower():
                        npc = self.world_data["npcs"][npc_id]
                        condition = self.game_state.get_dialogue_condition(npc_id) or "first_encounter"
                        for dialogue in npc.dialogue:
                            if dialogue["condition"] == condition:
                                return dialogue["text"]
                        return f"{npc.name} doesn't have anything to say right now."
                return f"There is no {command.target} here to talk to."
            return "Talk to whom?"

        elif command.action == "examine":
            if command.target:
                # Check items in current room by item_id
                for item_id in self.game_state.location.items:
                    if item_id == command.target.lower():
                        item = self.world_data["items"][item_id]
                        return f"{item.name}: {item.description}"

                # Check NPCs in current room by npc_id
                for npc_id in self.game_state.location.npcs:
                    if npc_id == command.target.lower():
                        npc = self.world_data["npcs"][npc_id]
                        if npc.dialogue:
                            return f"{npc.name}: {npc.dialogue[0]['text']}"
                        else:
                            return f"{npc.name} doesn't have anything to say."

                # If not found in items or NPCs, return not found message
                return f"You don't see any {command.target} here."
            return "Examine what?"

        elif command.action == "look":
            return self.game_state.location.description

        elif command.action == "inventory":
            if self.game_state.inventory:
                items_list = ", ".join(item.name for item in self.game_state.inventory)
                return f"You are carrying: {items_list}"
            return "You're not carrying anything."

        elif command.action == "items":
            # Handle 'items' action (alias for 'inventory')
            if self.game_state.inventory:
                items_list = ", ".join(item.name for item in self.game_state.inventory)
                return f"You are carrying: {items_list}"
            return "You're not carrying anything."

        elif command.action == "stats":
            return (f"Health: {self.game_state.player.health}, "
                    f"Attack: {self.game_state.player.attack}, "
                    f"Defense: {self.game_state.player.defense}")

        elif command.action == "help":
            return ("Available commands:\n"
                    "move [direction] - Move in a direction (north, south, east, west)\n"
                    "get/take [item] - Pick up an item\n"
                    "drop [item] - Drop an item from your inventory\n"
                    "use [item] - Use an item from your inventory\n"
                    "equip [item] - Equip an item from your inventory\n"
                    "attack - Attack a monster in the room\n"
                    "talk [npc] - Talk to an NPC\n"
                    "examine [item/npc] - Examine an item or NPC\n"
                    "look - Look around the current room\n"
                    "inventory/items - View your inventory\n"
                    "stats - View your character stats\n"
                    "save - Save the game\n"
                    "quit - Quit the game")

        elif command.action == "save":
            save_game(self.game_state)
            return "Game saved."

        elif command.action == "quit":
            self.running = False
            return "Goodbye!"

        else:
            return "I don't understand that command."

    def run(self) -> None:
        print("Welcome to the Gothic Castle Adventure!")
        print("Type 'new' to start a new game or 'load' to continue a saved game.")
        print("Type 'help' for a list of commands.")

        while True:
            command_input = input("> ").strip()
            if not command_input:
                continue

            if command_input.lower() == "quit":
                print("Goodbye!")
                self.running = False
                break

            if command_input.lower() in ["new", "start"]:
                self.initialize_game()
                print("Game started!")
                print(self.game_state.location.description)
                continue

            if command_input.lower() == "load":
                saved_game = load_game()
                if saved_game is not None:
                    self.game_state = saved_game
                    print("Game loaded!")
                    print(self.game_state.location.description)
                else:
                    print("No saved game found.")
                continue

            command = parse_command(command_input)
            response = self.handle_command(command)
            print(response)

            if not self.running:
                break

def initialize_game(self) -> None:
    """Initialize the game by creating a new Player instance."""
    self.player = Player(health=100, attack=10, defense=5)
