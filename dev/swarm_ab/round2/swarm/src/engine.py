"""
Main game engine for the text adventure.

Responsibilities
-----------------
* Load the initial world state.
* Run the main input‑output loop.
* Parse player commands and dispatch them to concrete handlers.
* Update the shared ``GameState`` dictionary according to the data contracts.
* Integrate combat, saving and loading.

The implementation is deliberately self‑contained: all command handling lives
inside this module, but the heavy‑lifting (parsing, combat resolution,
world loading, persistence) is delegated to the other packages as required
by the architecture specification.
"""

from __future__ import annotations

from typing import Callable, Dict, List

# Architecture‑specified imports -------------------------------------------------
# The symbols live in their respective submodules; we import them directly.
from src.parser import parse_command  # Callable[[str], Dict[str, Any]]
from src.combat import resolve_combat_turn  # Callable[[Dict, str], None]
from src.world_loader import load_world  # Callable[[], Dict]
from src.save_load import save_game, load_game  # (state: Dict) -> None / () -> Dict
from src.models import GameState  # Alias for the canonical state dict type

# --------------------------------------------------------------------------- #
# Helper types
# --------------------------------------------------------------------------- #
Command = Dict[str, str]  # Expected keys: "verb" and optionally "noun"


class GameEngine:
    """
    Core engine that maintains the mutable ``GameState`` and processes player
    input until the game ends.
    """

    def __init__(self) -> None:
        # Load a fresh world; ``load_world`` must return a dict that already
        # conforms to the ``GameState`` contract.
        self.state: GameState = load_world()
        self.running: bool = True

        # Mapping from command verb → handler method
        self._dispatch_table: Dict[str, Callable[[Command], None]] = {
            "go": self._handle_go,
            "move": self._handle_go,
            "north": self._handle_go,
            "south": self._handle_go,
            "east": self._handle_go,
            "west": self._handle_go,
            "take": self._handle_take,
            "get": self._handle_take,
            "pickup": self._handle_take,
            "attack": self._handle_attack,
            "hit": self._handle_attack,
            "look": self._handle_look,
            "inspect": self._handle_look,
            "inventory": self._handle_inventory,
            "inv": self._handle_inventory,
            "equip": self._handle_equip,
            "unequip": self._handle_unequip,
            "save": self._handle_save,
            "load": self._handle_load,
            "quit": self._handle_quit,
            "exit": self._handle_quit,
        }

    # --------------------------------------------------------------------- #
    # Public API
    # --------------------------------------------------------------------- #
    def run(self) -> None:
        """Main REPL loop."""
        self._print_welcome()
        while self.running and not self.state.get("game_over", False):
            try:
                line = input("> ").strip()
            except EOFError:
                # Treat end‑of‑file as a quit request.
                line = "quit"

            if not line:
                continue

            command = parse_command(
                line
            )  # Expected to return {"verb": ..., "noun": ...}
            self._process_command(command)

        self._print_farewell()

    # --------------------------------------------------------------------- #
    # Core processing
    # --------------------------------------------------------------------- #
    def _process_command(self, command: Command) -> None:
        """Dispatch a parsed command to the appropriate handler."""
        verb = command.get("verb", "").lower()
        handler = self._dispatch_table.get(verb)

        if handler is None:
            print(f"I don't understand '{verb}'.")
            return

        try:
            handler(command)
        except Exception as exc:  # pragma: no cover – defensive programming
            print(f"An error occurred while processing the command: {exc}")

        # After each player action, resolve any pending combat.
        self._resolve_combat_if_needed()

        # Check win/lose conditions.
        self._check_game_end_conditions()

    # --------------------------------------------------------------------- #
    # Command handlers
    # --------------------------------------------------------------------- #
    def _handle_go(self, command: Command) -> None:
        """Move the player to an adjacent room."""
        direction = command.get("noun", "").lower()
        current_room_id = self.state["player_location"]
        rooms = self.state["rooms"]
        current_room = rooms.get(current_room_id, {})

        connections: Dict[str, str] = current_room.get("connections", {})
        target_room_id = connections.get(direction)

        if not target_room_id:
            print(f"You can't go '{direction}' from here.")
            return

        self.state["player_location"] = target_room_id
        # Mark the room as visited.
        rooms[target_room_id]["visited"] = True
        print(f"You move {direction} to {target_room_id}.")

    def _handle_take(self, command: Command) -> None:
        """Pick up an item from the current room."""
        item_id = command.get("noun")
        if not item_id:
            print("Take what?")
            return

        room = self.state["rooms"][self.state["player_location"]]
        room_items: List[str] = room.get("items", [])
        if item_id not in room_items:
            print(f"There is no '{item_id}' here.")
            return

        # Transfer the item to the player's inventory.
        room_items.remove(item_id)
        self.state["player"]["inventory"].append(item_id)
        print(f"You take the {item_id}.")

    def _handle_attack(self, command: Command) -> None:
        """Initiate combat with a monster in the current room."""
        monster_id = command.get("noun")
        if not monster_id:
            print("Attack what?")
            return

        room = self.state["rooms"][self.state["player_location"]]
        monsters_in_room: List[str] = room.get("monsters", [])
        if monster_id not in monsters_in_room:
            print(f"There is no '{monster_id}' here.")
            return

        # Resolve a single combat turn where the player attacks the chosen monster.
        resolve_combat_turn(self.state, monster_id)

    def _handle_look(self, command: Command) -> None:
        """Describe the current location."""
        room_id = self.state["player_location"]
        room = self.state["rooms"][room_id]
        description = room.get("description", "You see nothing special.")
        print(description)

        items = room.get("items", [])
        monsters = room.get("monsters", [])
        npcs = room.get("npcs", [])

        if items:
            print("Items here:", ", ".join(items))
        if monsters:
            print("Monsters here:", ", ".join(monsters))
        if npcs:
            print("People here:", ", ".join(npcs))

    def _handle_inventory(self, command: Command) -> None:
        """Show player inventory."""
        inv = self.state["player"]["inventory"]
        if not inv:
            print("Your inventory is empty.")
        else:
            print("You are carrying:", ", ".join(inv))

    def _handle_equip(self, command: Command) -> None:
        """Equip a weapon or armor from inventory."""
        item_id = command.get("noun")
        if not item_id:
            print("Equip what?")
            return

        if item_id not in self.state["player"]["inventory"]:
            print(f"You don't have '{item_id}'.")
            return

        # Simplistic heuristic: items ending with "_weapon" are weapons,
        # items ending with "_armor" are armor.
        if item_id.endswith("_weapon"):
            self.state["player"]["equipped_weapon"] = item_id
            print(f"You equip the weapon '{item_id}'.")
        elif item_id.endswith("_armor"):
            self.state["player"]["equipped_armor"] = item_id
            print(f"You equip the armor '{item_id}'.")
        else:
            print(f"The item '{item_id}' cannot be equipped.")

    def _handle_unequip(self, command: Command) -> None:
        """Unequip weapon or armor."""
        slot = command.get("noun", "").lower()
        if slot in ("weapon", "w"):
            self.state["player"]["equipped_weapon"] = None
            print("You unequip your weapon.")
        elif slot in ("armor", "a"):
            self.state["player"]["equipped_armor"] = None
            print("You unequip your armor.")
        else:
            print("Specify 'weapon' or 'armor' to unequip.")

    def _handle_save(self, command: Command) -> None:
        """Persist the current game state."""
        save_game(self.state)
        print("Game saved.")

    def _handle_load(self, command: Command) -> None:
        """Load a previously saved game state."""
        loaded = load_game()
        if not isinstance(loaded, dict):
            print("Failed to load a valid save file.")
            return
        self.state = loaded  # type: ignore[assignment]
        print("Game loaded.")

    def _handle_quit(self, command: Command) -> None:
        """Terminate the game loop."""
        self.running = False
        print("Goodbye!")

    # --------------------------------------------------------------------- #
    # Post‑action helpers
    # --------------------------------------------------------------------- #
    def _resolve_combat_if_needed(self) -> None:
        """
        If there are any alive monsters in the current room after the player's
        turn, let each of them act. ``resolve_combat_turn`` is expected to handle
        a single monster's action; we call it for every alive monster.
        """
        room = self.state["rooms"][self.state["player_location"]]
        monster_ids: List[str] = room.get("monsters", [])
        for m_id in monster_ids:
            monster = self.state["monsters"].get(m_id, {})
            if monster.get("alive", False):
                resolve_combat_turn(self.state, m_id)

    def _check_game_end_conditions(self) -> None:
        """Set ``game_over``/``victory`` flags based on player health and objectives."""
        player = self.state["player"]
        if player["health"] <= 0:
            self.state["game_over"] = True
            self.state["victory"] = False
            print("You have perished. Game over.")
            return

        # Example victory condition: all monsters defeated.
        any_alive = any(m.get("alive", False) for m in self.state["monsters"].values())
        if not any_alive:
            self.state["game_over"] = True
            self.state["victory"] = True
            print("All foes have fallen. You are victorious!")

    # --------------------------------------------------------------------- #
    # UI helpers
    # --------------------------------------------------------------------- #
    def _print_welcome(self) -> None:
        print("Welcome to the adventure! Type 'help' for commands, or 'quit' to exit.")
        self._handle_look({})  # Show initial room description.

    def _print_farewell(self) -> None:
        if self.state.get("victory"):
            print("Congratulations on your triumph!")
        elif self.state.get("game_over"):
            print("Better luck next time.")
        else:
            print("Thanks for playing.")


def run_engine() -> None:
    """
    Entry point used by ``main.py``. Creates a ``GameEngine`` instance and
    starts the main loop.
    """
    engine = GameEngine()
    engine.run()
