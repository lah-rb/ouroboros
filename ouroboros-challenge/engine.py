import sys
from typing import Dict, Optional

from models import GameState, Command, Room, Item, NPC
from parser import parse_command


class GameEngine:
    def __init__(self, world_data: dict, initial_state: GameState) -> None:
        self.rooms: Dict[str, Room] = {}
        self.items: Dict[str, Item] = {}
        self.npcs: Dict[str, NPC] = {}
        self.state: GameState = initial_state
        self._load_world(world_data)

    def _load_world(self, world_data: dict) -> None:
        for room_id, room_data in world_data.get("rooms", {}).items():
            room = Room(
                id=room_id,
                name=room_data.get("name", ""),
                description=room_data.get("description", ""),
                exits=room_data.get("exits", {}),
                items=room_data.get("items", []),
                npcs=room_data.get("npcs", []),
            )
            self.rooms[room_id] = room

        for item_id, item_data in world_data.get("items", {}).items():
            item = Item(
                id=item_id,
                name=item_data.get("name", ""),
                description=item_data.get("description", ""),
                can_take=item_data.get("can_take", False),
                can_use=item_data.get("can_use", False),
            )
            self.items[item_id] = item

        for npc_id, npc_data in world_data.get("npcs", {}).items():
            npc = NPC(
                id=npc_id,
                name=npc_data.get("name", ""),
                description=npc_data.get("description", ""),
                dialogue=npc_data.get("dialogue", {}),
            )
            self.npcs[npc_id] = npc

    def _print_room_description(self) -> None:
        current_room = self.rooms.get(self.state.current_room)
        if not current_room:
            print(f"Error: Current room '{self.state.current_room}' not found.")
            return

        print(f"\n{current_room.name}")
        print(current_room.description)

        if current_room.items:
            item_names = []
            for item_id in current_room.items:
                item = self.items.get(item_id)
                if item:
                    item_names.append(item.name)
            print(f"You see: {', '.join(item_names)}")

        if current_room.npcs:
            npc_names = []
            for npc_id in current_room.npcs:
                npc = self.npcs.get(npc_id)
                if npc:
                    npc_names.append(npc.name)
            print(f"NPCs here: {', '.join(npc_names)}")

        exits = []
        for direction in current_room.exits:
            if direction in ("n", "north"):
                exits.append("north")
            elif direction in ("s", "south"):
                exits.append("south")
            elif direction in ("e", "east"):
                exits.append("east")
            elif direction in ("w", "west"):
                exits.append("west")
        if exits:
            print(f"Exits: {', '.join(exits)}")

    def run(self) -> None:
        self._print_room_description()

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.rstrip("\n")

                if line.strip().lower() in ("quit", "exit"):
                    print("Goodbye!")
                    break

                self._process_command(line)

            except KeyboardInterrupt:
                print("\nGoodbye!")
                break

    def _process_command(self, line: str) -> None:
        words = line.lower().split()
        if not words:
            return

        command = words[0]
        noun = " ".join(words[1:]) if len(words) > 1 else None

        if command in ("n", "north"):
            self._move("north")
        elif command in ("s", "south"):
            self._move("south")
        elif command in ("e", "east"):
            self._move("east")
        elif command in ("w", "west"):
            self._move("west")
        elif command == "look":
            self._look(noun)
        elif command in ("i", "inv", "inventory"):
            self._inventory()
        elif command == "take":
            self._take(noun)
        elif command == "drop":
            self._drop(noun)
        elif command == "use":
            self._use(noun)
        elif command == "talk":
            self._talk(noun)
        elif command == "help":
            self._show_help()
        else:
            print(f"I don't understand '{command}'.")

    def _move(self, direction: str) -> None:
        current_room = self.rooms.get(self.state.current_room)
        if not current_room:
            print(f"Error: Current room '{self.state.current_room}' not found.")
            return

        if direction not in current_room.exits:
            print(f"You can't go {direction} from here.")
            return

        next_room_id = current_room.exits[direction]
        next_room = self.rooms.get(next_room_id)
        if not next_room:
            print(f"Error: Room '{next_room_id}' not found.")
            return

        self.state.current_room = next_room_id
        self._print_room_description()

    def _look(self, noun: Optional[str]) -> None:
        if not noun:
            self._print_room_description()
            return

        current_room = self.rooms.get(self.state.current_room)
        if not current_room:
            print(f"Error: Current room '{self.state.current_room}' not found.")
            return

        # Check items in current room
        for item_id in current_room.items:
            item = self.items.get(item_id)
            if item and (
                item.name.lower() == noun.lower() or item.id.lower() == noun.lower()
            ):
                print(f"{item.name}: {item.description}")
                return

        # Check items in inventory
        for item_id in self.state.inventory:
            item = self.items.get(item_id)
            if item and (
                item.name.lower() == noun.lower() or item.id.lower() == noun.lower()
            ):
                print(f"{item.name}: {item.description}")
                return

        print(f"You don't see '{noun}' here.")

    def _inventory(self) -> None:
        if self.state.inventory:
            item_names = []
            for item_id in self.state.inventory:
                item = self.items.get(item_id)
                if item:
                    item_names.append(item.name)
            print(f"You are carrying: {', '.join(item_names)}")
        else:
            print("You are not carrying anything.")

    def _take(self, noun: Optional[str]) -> None:
        if not noun:
            print("Take what?")
            return

        current_room = self.rooms.get(self.state.current_room)
        if not current_room:
            print(f"Error: Current room '{self.state.current_room}' not found.")
            return

        item_to_take = None
        item_id_to_take = None

        for item_id in current_room.items:
            item = self.items.get(item_id)
            if item and (
                item.name.lower() == noun.lower() or item.id.lower() == noun.lower()
            ):
                item_to_take = item
                item_id_to_take = item_id
                break

        if item_to_take is None:
            for item_id in self.state.inventory:
                item = self.items.get(item_id)
                if item and (
                    item.name.lower() == noun.lower() or item.id.lower() == noun.lower()
                ):
                    print(f"You already have the {item.name}.")
                    return

        if item_to_take is None:
            print(f"You don't see '{noun}' here.")
            return

        if not item_to_take.can_take:
            print(f"You can't take the {item_to_take.name}.")
            return

        self.state.inventory.append(item_id_to_take)
        current_room.items.remove(item_id_to_take)
        print(f"You take the {item_to_take.name}.")

    def _drop(self, noun: Optional[str]) -> None:
        if not noun:
            print("Drop what?")
            return

        item_to_drop = None
        item_id_to_drop = None

        for item_id in self.state.inventory:
            item = self.items.get(item_id)
            if item and (
                item.name.lower() == noun.lower() or item.id.lower() == noun.lower()
            ):
                item_to_drop = item
                item_id_to_drop = item_id
                break

        if item_to_drop is None:
            print(f"You don't have the {noun}.")
            return

        self.state.inventory.remove(item_id_to_drop)

        current_room = self.rooms.get(self.state.current_room)
        if not current_room:
            print(f"Error: Current room '{self.state.current_room}' not found.")
            return

        current_room.items.append(item_id_to_drop)
        print(f"You drop the {item_to_drop.name}.")

    def _use(self, noun: Optional[str]) -> None:
        if not noun:
            print("Use what?")
            return

        item_to_use = None
        item_id_to_use = None

        for item_id in self.state.inventory:
            item = self.items.get(item_id)
            if item and (
                item.name.lower() == noun.lower() or item.id.lower() == noun.lower()
            ):
                item_to_use = item
                item_id_to_use = item_id
                break

        if item_to_use is None:
            print(f"You don't have the {noun}.")
            return

        if not item_to_use.can_use:
            print(f"You can't use the {item_to_use.name}.")
            return

        print(f"You use the {item_to_use.name}.")
        self.state.completed_actions.append(f"use_{item_id_to_use}")

    def _talk(self, noun: Optional[str]) -> None:
        if not noun:
            print("Talk to whom?")
            return

        current_room = self.rooms.get(self.state.current_room)
        if not current_room:
            print(f"Error: Current room '{self.state.current_room}' not found.")
            return

        npc_to_talk = None
        npc_id_to_talk = None

        for npc_id in current_room.npcs:
            npc = self.npcs.get(npc_id)
            if npc and (
                npc.name.lower() == noun.lower() or npc.id.lower() == noun.lower()
            ):
                npc_to_talk = npc
                npc_id_to_talk = npc_id
                break

        if npc_to_talk is None:
            print(f"You don't see '{noun}' here.")
            return

        print(f"{npc_to_talk.name} says: Hello! How can I help you?")
        print("Available topics: hello, help, quest")

        while True:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.rstrip("\n")

                words = line.lower().split()
                topic = words[0] if words else ""

                if topic in ("exit", "bye"):
                    print(f"{npc_to_talk.name} says: Goodbye!")
                    break

                if topic in npc_to_talk.dialogue:
                    responses = npc_to_talk.dialogue[topic]
                    if responses:
                        print(f"{npc_to_talk.name} says: {responses[0]}")
                    else:
                        print(f"{npc_to_talk.name} says: ...")
                else:
                    print(f"{npc_to_talk.name} says: I don't know about that.")

            except KeyboardInterrupt:
                print("\nConversation ended.")
                break

    def _show_help(self) -> None:
        """Display help information."""
        print("Available commands:")
        print("  n, north     - Move north")
        print("  s, south     - Move south")
        print("  e, east      - Move east")
        print("  w, west      - Move west")
        print("  look         - Look around the current room")
        print("  look <item>  - Look at a specific item")
        print("  i, inv, inventory - Show your inventory")
        print("  take <item>  - Pick up an item")
        print("  drop <item>  - Drop an item")
        print("  use <item>   - Use an item")
        print("  talk <npc>   - Talk to an NPC")
        print("  help         - Show this help message")
        print("  quit         - Quit the game")
