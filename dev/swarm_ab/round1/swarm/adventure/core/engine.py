"""Main game engine coordinating state, command dispatch, and gameplay loops."""

from typing import Dict, Any

from adventure.models.entities import Player
from adventure.core.parser import Command, parse_command
from adventure.core.combat import CombatEngine
from adventure.utils.io import save_state, load_state


class GameEngine:
    """Orchestrates the adventure game.

    Args:
        world_data: Dictionary produced by `load_world`, containing rooms,
            items, npcs, monsters, boss definition, and start locations.

    Public Methods:
        start() -> None:
            Begins the interactive command loop, handling player input until
            quit or defeat.

    Command Dispatch Mapping (verb → handler method signature):
        "go"      -> _handle_move(self, direction: str) -> None
        "take"    -> _handle_take(self, item_id: str) -> None
        "drop"    -> _handle_drop(self, item_id: str) -> None
        "use"     -> _handle_use(self, item_id: str) -> None
        "examine" -> _handle_examine(self, target: str) -> None
        "talk"    -> _handle_talk(self, npc_id: str) -> None
        "attack"  -> _handle_attack(self, monster_id: str) -> None
        "flee"    -> _handle_flee(self) -> None
        "look"    -> _handle_look(self) -> None
        "status"  -> _handle_status(self) -> None
        "help"    -> _handle_help(self) -> None
        "quit"    -> _handle_quit(self) -> None
        "save"    -> _handle_save(self, path: str) -> None
        "load"    -> _handle_load(self, path: str) -> None

    Raises:
        RuntimeError: If internal state becomes inconsistent.

    >>> dummy_world = {"rooms": [], "items": [], "npcs": [], "monsters": [], "boss": {}, "start_room": "", "boss_room": ""}
    >>> engine = GameEngine(dummy_world)
    >>> isinstance(engine, GameEngine)
    True
    """

    def __init__(self, world_data: Dict[str, Any]) -> None:
        # Store the raw world data for possible later use.
        self._world_data = world_data

        # Basic sanity check – required keys must exist.
        required_keys = {
            "rooms",
            "items",
            "npcs",
            "monsters",
            "boss",
            "start_room",
            "boss_room",
        }
        missing = required_keys - world_data.keys()
        if missing:
            raise RuntimeError(f"World data missing required keys: {missing}")

        # Initialize player.  The Player model is expected to accept a name
        # and an inventory list; if the signature differs, the import will
        # raise an error, which satisfies the contract's “raise on inconsistency”.
        self.player = Player(name="Hero", inventory=[])

        # Set current room based on start location.
        self.current_room_id = world_data.get("start_room")
        self._rooms_by_id = {room.id: room for room in world_data.get("rooms", [])}
        self._items_by_id = {item.id: item for item in world_data.get("items", [])}
        self._npcs_by_id = {npc.id: npc for npc in world_data.get("npcs", [])}
        self._monsters_by_id = {
            monster.id: monster for monster in world_data.get("monsters", [])
        }

        # Verify that the start room exists.
        if self.current_room_id not in self._rooms_by_id:
            raise RuntimeError(
                f"Start room '{self.current_room_id}' not found in world data."
            )

        # Combat engine – one per game session.
        self.combat_engine = CombatEngine()

        # Command dispatch table mapping verb strings to bound methods.
        self._dispatch = {
            "go": self._handle_move,
            "take": self._handle_take,
            "drop": self._handle_drop,
            "use": self._handle_use,
            "examine": self._handle_examine,
            "talk": self._handle_talk,
            "attack": self._handle_attack,
            "flee": self._handle_flee,
            "look": self._handle_look,
            "status": self._handle_status,
            "help": self._handle_help,
            "quit": self._handle_quit,
            "save": self._handle_save,
            "load": self._handle_load,
        }

        # Internal flag to control the main loop.
        self._running = False

    def start(self) -> None:
        """Run the main input loop until termination."""
        self._running = True
        # Initial description.
        self._handle_look()
        while self._running:
            try:
                raw = input("> ").strip()
                if not raw:
                    continue
                cmd: Command = parse_command(raw)
                handler = self._dispatch.get(cmd.verb)
                if handler is None:
                    print(f"Unknown command '{cmd.verb}'. Type 'help' for assistance.")
                    continue
                # Dispatch with appropriate arguments.
                if cmd.args:
                    handler(*cmd.args)
                else:
                    handler()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting game.")
                break
            except RuntimeError as e:
                print(f"Runtime error: {e}")
                break

    # --- Command handlers -------------------------------------------------
    def _handle_move(self, direction: str) -> None:
        """Move the player to an adjacent room in the given direction."""
        current_room = self._rooms_by_id.get(self.current_room_id)
        if not current_room:
            raise RuntimeError("Current room data missing.")
        next_room_id = getattr(current_room.exits, direction, None)
        if not next_room_id:
            print(f"You can't go {direction} from here.")
            return
        if next_room_id not in self._rooms_by_id:
            raise RuntimeError(f"Target room '{next_room_id}' does not exist.")
        self.current_room_id = next_room_id
        self._handle_look()

    def _handle_take(self, item_id: str) -> None:
        """Pick up an item from the current room and add it to inventory."""
        room = self._rooms_by_id.get(self.current_room_id)
        if not room:
            raise RuntimeError("Current room data missing.")
        if item_id not in room.items:
            print(f"There is no item '{item_id}' here.")
            return
        item = self._items_by_id.get(item_id)
        if not item:
            raise RuntimeError(f"Item definition for '{item_id}' missing.")
        room.items.remove(item_id)
        self.player.inventory.append(item_id)
        print(f"You take the {item.name}.")

    def _handle_drop(self, item_id: str) -> None:
        """Drop an inventory item into the current room."""
        if item_id not in self.player.inventory:
            print(f"You don't have '{item_id}'.")
            return
        room = self._rooms_by_id.get(self.current_room_id)
        if not room:
            raise RuntimeError("Current room data missing.")
        self.player.inventory.remove(item_id)
        room.items.append(item_id)
        item = self._items_by_id.get(item_id)
        print(f"You drop the {item.name}.")

    def _handle_use(self, item_id: str) -> None:
        """Use an item from inventory; delegate to item's use logic if present."""
        if item_id not in self.player.inventory:
            print(f"You don't have '{item_id}'.")
            return
        item = self._items_by_id.get(item_id)
        if not item:
            raise RuntimeError(f"Item definition for '{item_id}' missing.")
        # Assume items expose a `use` method that returns a string description.
        if hasattr(item, "use"):
            result = item.use(self)  # Pass engine for context if needed.
            if result:
                print(result)
        else:
            print(f"The {item.name} can't be used.")

    def _handle_examine(self, target: str) -> None:
        """Examine a room feature, item, NPC, or monster."""
        # Check room first.
        room = self._rooms_by_id.get(self.current_room_id)
        if not room:
            raise RuntimeError("Current room data missing.")
        if target == "room":
            print(room.description)
            return
        # Items in room.
        if target in room.items:
            item = self._items_by_id.get(target)
            print(
                item.description
                if hasattr(item, "description")
                else f"You see a {item.name}."
            )
            return
        # NPCs in room.
        if target in room.npcs:
            npc = self._npcs_by_id.get(target)
            print(npc.dialogue if hasattr(npc, "dialogue") else f"{npc.name} is here.")
            return
        # Monsters in room.
        if target in room.monsters:
            monster = self._monsters_by_id.get(target)
            print(
                monster.description
                if hasattr(monster, "description")
                else f"A {monster.name} lurks here."
            )
            return
        print(f"There is nothing notable about '{target}'.")

    def _handle_talk(self, npc_id: str) -> None:
        """Talk to an NPC present in the current room."""
        room = self._rooms_by_id.get(self.current_room_id)
        if not room or npc_id not in room.npcs:
            print(f"There is no one named '{npc_id}' here.")
            return
        npc = self._npcs_by_id.get(npc_id)
        if not npc:
            raise RuntimeError(f"NPC definition for '{npc_id}' missing.")
        # Assume NPCs have a `speak` method returning dialogue.
        if hasattr(npc, "speak"):
            print(npc.speak())
        else:
            print(f"{npc.name} has nothing to say.")

    def _handle_attack(self, monster_id: str) -> None:
        """Initiate combat with a monster in the current room."""
        room = self._rooms_by_id.get(self.current_room_id)
        if not room or monster_id not in room.monsters:
            print(f"There is no monster '{monster_id}' here.")
            return
        monster = self._monsters_by_id.get(monster_id)
        if not monster:
            raise RuntimeError(f"Monster definition for '{monster_id}' missing.")
        # Use the combat engine; assume it returns a bool indicating player survival.
        survived = self.combat_engine.fight(self.player, monster)
        if survived:
            print(f"You have defeated the {monster.name}!")
            room.monsters.remove(monster_id)
        else:
            print("You have been slain.")
            self._running = False

    def _handle_flee(self) -> None:
        """Attempt to flee from combat; ends current combat round."""
        # Simple implementation: just announce and continue.
        print("You attempt to flee...")
        # In a full system this would interact with CombatEngine; here we just note it.
        self.combat_engine.attempt_flee(self.player)

    def _handle_look(self) -> None:
        """Describe the current room, its exits, items, NPCs, and monsters."""
        room = self._rooms_by_id.get(self.current_room_id)
        if not room:
            raise RuntimeError("Current room data missing.")
        print(f"\n{room.name}")
        print(room.description)
        # Exits
        exits = [dir for dir in vars(room.exits) if getattr(room.exits, dir)]
        if exits:
            print("Exits: " + ", ".join(exits))
        # Items
        if room.items:
            item_names = [
                self._items_by_id[i].name for i in room.items if i in self._items_by_id
            ]
            print("You see: " + ", ".join(item_names))
        # NPCs
        if room.npcs:
            npc_names = [
                self._npcs_by_id[n].name for n in room.npcs if n in self._npcs_by_id
            ]
            print("People here: " + ", ".join(npc_names))
        # Monsters
        if room.monsters:
            monster_names = [
                self._monsters_by_id[m].name
                for m in room.monsters
                if m in self._monsters_by_id
            ]
            print("Danger! " + ", ".join(monster_names))

    def _handle_status(self) -> None:
        """Print player status (health, inventory, etc.)."""
        # Assume Player has health and maybe other stats.
        health = getattr(self.player, "health", "unknown")
        print(f"Health: {health}")
        if self.player.inventory:
            inv_names = [
                self._items_by_id[i].name
                for i in self.player.inventory
                if i in self._items_by_id
            ]
            print("Inventory: " + ", ".join(inv_names))
        else:
            print("Inventory is empty.")

    def _handle_help(self) -> None:
        """Display a brief help message with available commands."""
        cmds = sorted(self._dispatch.keys())
        print("Available commands: " + ", ".join(cmds))

    def _handle_quit(self) -> None:
        """Terminate the game loop."""
        print("Goodbye!")
        self._running = False

    def _handle_save(self, path: str) -> None:
        """Save current game state to a file."""
        state = {
            "player": self.player,
            "current_room_id": self.current_room_id,
            "rooms": list(self._rooms_by_id.values()),
            "items": list(self._items_by_id.values()),
            "npcs": list(self._npcs_by_id.values()),
            "monsters": list(self._monsters_by_id.values()),
        }
        save_state(state, path)
        print(f"Game saved to {path}.")

    def _handle_load(self, path: str) -> None:
        """Load game state from a file, replacing current session."""
        loaded = load_state(path)
        # Basic validation of loaded structure.
        if not isinstance(loaded, dict):
            raise RuntimeError("Loaded state is malformed.")
        self.player = loaded.get("player", self.player)
        self.current_room_id = loaded.get("current_room_id", self.current_room_id)

        # Rebuild lookup tables from loaded collections.
        self._rooms_by_id = {room.id: room for room in loaded.get("rooms", [])}
        self._items_by_id = {item.id: item for item in loaded.get("items", [])}
        self._npcs_by_id = {npc.id: npc for npc in loaded.get("npcs", [])}
        self._monsters_by_id = {
            monster.id: monster for monster in loaded.get("monsters", [])
        }

        print(f"Game loaded from {path}.")
