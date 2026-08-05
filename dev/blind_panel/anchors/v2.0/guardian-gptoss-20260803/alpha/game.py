"""
Main game engine handling state, command dispatch, combat, and persistence.
"""

import random
import json
import os
from typing import Dict, List, Set

from entities import (
    Player,
    Item,
    Weapon,
    Armor,
    HealingItem,
    Monster,
    NPC,
    Room,
)
from world import load_world
from parser import parse_command

SAVE_PATH = "savegame.json"


class Game:
    """
    Central class that holds mutable game state and provides methods for
    command handling, combat, saving/loading, etc.
    """

    def __init__(self) -> None:
        # Load static world data
        world_data = load_world()
        self.rooms: Dict[str, Room] = world_data["rooms"]
        self.items: Dict[str, Item] = world_data["items"]
        self.monsters: Dict[str, Monster] = world_data["monsters"]
        self.npcs: Dict[str, NPC] = world_data["npcs"]

        # Determine the initial player location.
        # Use a room explicitly keyed as "start" if it exists; otherwise,
        # fall back to the first room defined in the world data.
        if "start" in self.rooms:
            self.location = "start"
        else:
            try:
                self.location = next(iter(self.rooms))
            except StopIteration:
                raise RuntimeError(
                    "World data contains no rooms to start in."
                ) from None

        # Mutable state
        self.player = Player()
        self.inventory: List[str] = []  # item ids
        self.equipment: Dict[str, str | None] = {
            "weapon": None,
            "armor": None,
        }
        self.defeated_monsters: Set[str] = set()
        self.npc_progress: Dict[str, str] = {}  # npc_id -> dialogue node

        # Load progress if a save file exists
        if os.path.exists(SAVE_PATH):
            try:
                with open(SAVE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                self.load_state(state)
                print("Loaded saved game.")
            except Exception:
                print("Failed to load save file; starting new game.")

    # ----------------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------------
    def save_state(self) -> dict:
        """Return a dict matching the updated savegame.json contract, including each room's current items."""
        # Capture the current inventory of each room
        rooms_state = {
            room_id: list(room.items) for room_id, room in self.rooms.items()
        }

        return {
            "player": {
                "health": self.player.health,
                "attack": self.player.attack,
            },
            "location": self.location,
            "inventory": list(self.inventory),
            "equipment": {
                "weapon": self.equipment["weapon"],
                "armor": self.equipment["armor"],
            },
            "defeated_monsters": list(self.defeated_monsters),
            "npc_progress": dict(self.npc_progress),
            "rooms": rooms_state,
        }

    def load_state(self, state: dict) -> None:
        """
        Populate mutable game state from a saved dictionary.

        Restores the player's health, current location, inventory,
        equipped items, defeated monsters and NPC progress. Also restores
        each room's item list from the optional ``rooms`` mapping in the
        save file. If the mapping is missing or malformed the original
        room definitions are left untouched.
        """
        # --- Player attributes -------------------------------------------------
        player_data = state.get("player", {})
        self.player.health = player_data.get("health", 20)

        # Preserve base_attack unless an explicit field exists in the save.
        if "base_attack" in player_data:
            self.player.base_attack = player_data["base_attack"]

        # --- Location -----------------------------------------------------------
        saved_location = state.get("location", "start")
        if saved_location in self.rooms:
            resolved_location = saved_location
        else:
            # Attempt to resolve by matching the human‑readable room name (case‑insensitive)
            lowered = saved_location.lower()
            resolved_location = None
            for room_id, room_obj in self.rooms.items():
                if getattr(room_obj, "name", "").lower() == lowered:
                    resolved_location = room_id
                    break
            # Fallback to the start room if no match is found
            if not resolved_location:
                resolved_location = "start"
        self.location = resolved_location

        # --- Inventory -----------------------------------------------------------
        self.inventory = list(state.get("inventory", []))

        # --- Equipment -----------------------------------------------------------
        equipment_data = state.get("equipment", {})
        self.equipment["weapon"] = equipment_data.get("weapon")
        self.equipment["armor"] = equipment_data.get("armor")

        # Re‑link equipped objects to the player instance.
        if self.equipment["weapon"]:
            weapon_obj = self.items.get(self.equipment["weapon"])
            self.player.weapon = weapon_obj if isinstance(weapon_obj, Weapon) else None
        else:
            self.player.weapon = None

        if self.equipment["armor"]:
            armor_obj = self.items.get(self.equipment["armor"])
            self.player.armor = armor_obj if isinstance(armor_obj, Armor) else None
        else:
            self.player.armor = None

        # --- Defeated monsters ---------------------------------------------------
        self.defeated_monsters = set(state.get("defeated_monsters", []))

        # --- NPC progress --------------------------------------------------------
        self.npc_progress = dict(state.get("npc_progress", {}))

        # --- Rooms items restoration ---------------------------------------------
        rooms_state = state.get("rooms")
        if isinstance(rooms_state, dict):
            for room_id, room_obj in self.rooms.items():
                saved_items = rooms_state.get(room_id)
                if isinstance(saved_items, list):
                    # Ensure we store a copy to avoid accidental aliasing.
                    room_obj.items = list(saved_items)
        # If ``rooms`` mapping is missing or malformed, keep original items unchanged.

    def write_save(self) -> None:
        """Write current state to SAVE_PATH."""
        with open(SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(self.save_state(), f, indent=2)

    # ----------------------------------------------------------------------
    # Core helpers
    # ----------------------------------------------------------------------
    def current_room(self) -> Room:
        return self.rooms[self.location]

    def describe_current_location(self) -> None:
        """
        Print details about the player's current location, handling missing entities gracefully.
        """
        room = self.current_room()

        # Room name and description
        print(room.name)
        print(room.description)

        # Items in the room
        items = getattr(room, "items", [])
        if items:
            item_names = [
                self.items[item_id].name for item_id in items if item_id in self.items
            ]
            if item_names:
                print("You see the following items:", ", ".join(item_names))

        # NPCs in the room
        npcs = getattr(room, "npcs", [])
        if npcs:
            npc_names = [
                self.npcs[npc_id].name for npc_id in npcs if npc_id in self.npcs
            ]
            if npc_names:
                print("People here:", ", ".join(npc_names))

        # Monsters in the room – safely ignore undefined monster IDs
        monsters = getattr(room, "monsters", [])
        if monsters:
            monster_names = [
                self.monsters[monster_id].name
                for monster_id in monsters
                if monster_id in self.monsters
            ]
            if monster_names:
                print("Danger! Monsters present:", ", ".join(monster_names))

        # Exits
        exits = getattr(room, "exits", {})
        exit_list = ", ".join(exits.keys()) if isinstance(exits, dict) else ""
        print("Exits:", exit_list)

    def move_player(self, direction: str) -> None:
        """
        Move the player to an adjacent room in the given direction.

        Parameters
        ----------
        direction: str
            The direction command entered by the player (e.g., "north").

        The method validates that the direction is available from the current
        room and that the target room exists in the world map before updating
        the player's location.  If validation fails, an informative message is
        printed and the location remains unchanged.
        """
        # Normalize input to match how exits are stored (typically lower‑case)
        direction = direction.lower()

        current = self.current_room()
        if direction not in current.exits:
            print("You can't go that way.")
            return

        new_room_id = current.exits[direction]

        # Guard against malformed world data where the exit points to a non‑existent room
        if new_room_id not in self.rooms:
            print(f"The path leads to an unknown location ('{new_room_id}').")
            return

        self.location = new_room_id
        self.describe_current_location()

    # ----------------------------------------------------------------------
    # Command handling
    # ----------------------------------------------------------------------
    def handle_command(self, raw: str) -> bool:
        """
        Process a raw command string.
        Returns True to continue the game loop, False to quit.
        """
        cmd = parse_command(raw)
        action = cmd.action
        args = cmd.args or []

        if action == "empty":
            return True

        if action == "help":
            self.print_help()
        elif action == "quit":
            print("Thanks for playing!")
            self.write_save()
            return False
        elif action == "look":
            self.describe_current_location()
        elif action == "status":
            self.print_status()
        elif action == "inventory":
            self.print_inventory()
        elif action == "go":
            if not args:
                print("Go where?")
            else:
                self.move_player(args[0])
        elif action in {"north", "south", "east", "west"}:
            self.move_player(action)
        elif action == "take":
            self.take_item(args)
        elif action == "drop":
            self.drop_item(args)
        elif action == "examine":
            self.examine_item(args)
        elif action == "use":
            self.use_item(args)
        elif action == "equip":
            self.equip_item(args)
        elif action == "talk":
            self.talk_to(args)
        elif action == "attack":
            self.initiate_combat(args)
        elif action == "flee":
            if getattr(self, "in_combat", False):
                # Signal the active combat loop to exit
                setattr(self, "combat_flee_requested", True)
                print("You flee from combat.")
            else:
                print("There's nothing to flee from right now.")
        else:
            print("I don't understand that command.")
        return True

    # ----------------------------------------------------------------------
    # Utility commands
    # ----------------------------------------------------------------------
    def print_help(self) -> None:
        help_text = """
Available commands:
  go <direction>          – move (north, south, east, west)
  look                    – examine the current room
  status                  – show health, attack, location
  inventory               – list carried items
  take <item>             – pick up an item
  drop <item>             – leave an item behind
  examine <item>          – read an item's description
  use <item>              – use a consumable (e.g., healing potion)
  equip <item>            – equip a weapon or armor
  talk to <npc>           – converse with a character
  attack <monster>        – engage in combat
  flee                    – attempt to run from combat
  help                    – show this help message
  quit                    – exit the game
"""
        print(help_text.strip())

    def print_status(self) -> None:
        """Display the player's current health, attack, location, and equipped items."""
        # Core stats
        print(f"Health: {self.player.health}")
        print(f"Attack: {self.player.attack}")

        # Current location name (fallback if room data is missing)
        room = self.current_room()
        location_name = getattr(room, "name", "Unknown")
        print(f"Location: {location_name}")

        # Equipped weapon
        weapon_id = self.equipment.get("weapon")
        if weapon_id:
            weapon_item = self.items.get(weapon_id)
            weapon_name = weapon_item.name if weapon_item else "Unknown"
        else:
            weapon_name = "None"

        # Equipped armor
        armor_id = self.equipment.get("armor")
        if armor_id:
            armor_item = self.items.get(armor_id)
            armor_name = armor_item.name if armor_item else "Unknown"
        else:
            armor_name = "None"

        print(f"Weapon: {weapon_name}")
        print(f"Armor: {armor_name}")

    def print_inventory(self) -> None:
        if not self.inventory:
            print("You are carrying nothing.")
            return
        names = [self.items[i].name for i in self.inventory]
        print("Inventory:", ", ".join(names))

    # ----------------------------------------------------------------------
    # Item interactions
    # ----------------------------------------------------------------------
    def take_item(self, args: List[str]) -> None:
        """
        Pick up an item from the current room using case‑insensitive name matching.

        The lookup first tries an exact name match; if none is found it falls back to a
        substring (partial) match. Inventory is checked after taking the item to avoid
        duplicates.
        """
        # No arguments supplied.
        if not args:
            print("Take what?")
            return

        query = " ".join(args).strip().lower()
        room = self.current_room()

        # Ensure we have a list of items to work with.
        room_items: List[str] = getattr(room, "items", [])

        target_id: str | None = None
        target_item = None

        # --- Exact name match -------------------------------------------------
        for item_id in room_items:
            itm = self.items.get(item_id)
            if not itm:
                continue
            if itm.name.lower() == query:
                target_id, target_item = item_id, itm
                break

        # --- Substring (partial) match ---------------------------------------
        if target_id is None:
            for item_id in room_items:
                itm = self.items.get(item_id)
                if not itm:
                    continue
                if query in itm.name.lower():
                    target_id, target_item = item_id, itm
                    break

        # No matching item found in the room.
        if target_id is None:
            print("No such item here.")
            return

        # Already in player's inventory?
        if target_id in self.inventory:
            print("You already have that item.")
            return

        # Add to inventory and remove from the room.
        self.inventory.append(target_id)
        try:
            room_items.remove(target_id)
        except ValueError:
            pass  # Item was not in the list; ignore.

        print(f"You pick up the {target_item.name}.")

    def drop_item(self, args: List[str]) -> None:
        """Drop an item from the inventory using case‑insensitive name matching.

        The lookup first attempts an exact name match; if none is found it falls back
        to a substring (partial) match. If the item is currently equipped it will be
        unequipped before being placed in the current room.
        """
        if not args:
            print("Drop what?")
            return

        query = " ".join(args).strip().lower()

        # Search for an exact match in the inventory first.
        target_id = None
        target_item = None
        for item_id in self.inventory:
            itm = self.items[item_id]
            if itm.name.lower() == query:
                target_id, target_item = item_id, itm
                break

        # If no exact match, look for a substring match.
        if target_id is None:
            for item_id in self.inventory:
                itm = self.items[item_id]
                if query in itm.name.lower():
                    target_id, target_item = item_id, itm
                    break

        if target_id is None:
            print("You don't have that item.")
            return

        # Remove from inventory and place in the current room.
        self.inventory.remove(target_id)
        self.current_room().items.append(target_id)

        # Unequip if currently equipped.
        if self.equipment.get("weapon") == target_id:
            self.player.weapon = None
            self.equipment["weapon"] = None
        if self.equipment.get("armor") == target_id:
            self.player.armor = None
            self.equipment["armor"] = None

        print(f"You drop the {target_item.name}.")

    def examine_item(self, args: List[str]) -> None:
        """Display the description of an item by name.

        The lookup matches items case‑insensitively. It first attempts an exact
        name match; if none is found it falls back to a substring (partial) match.
        Inventory items are checked before items in the current room.
        """
        if not args:
            print("Examine what?")
            return

        query = " ".join(args).strip().lower()

        # Helper to search a collection of item IDs for a matching Item.
        def find_match(collection):
            for item_id in collection:
                itm = self.items[item_id]
                name_lower = itm.name.lower()
                if name_lower == query:  # exact match
                    return itm, True
            for item_id in collection:
                itm = self.items[item_id]
                if query in itm.name.lower():  # substring match
                    return itm, False
            return None, False

        # Search inventory first, then the current room.
        for coll in (self.inventory, self.current_room().items):
            matched_item, is_exact = find_match(coll)
            if matched_item:
                print(f"{matched_item.name}: {matched_item.description}")
                return

        print("You see no such item.")

    def use_item(self, args: List[str]) -> None:
        """Use an item from the inventory using case‑insensitive name matching.

        The lookup first attempts an exact name match; if none is found it falls back
        to a substring (partial) match. Healing items restore health up to the
        maximum of 20 and are removed from the inventory after use. Other items
        cannot be used and produce an informative message.
        """
        if not args:
            print("Use what?")
            return

        query = " ".join(args).strip().lower()

        # Search for an exact match in the inventory first.
        target_id = None
        target_item = None
        for item_id in self.inventory:
            itm = self.items[item_id]
            if itm.name.lower() == query:
                target_id, target_item = item_id, itm
                break

        # If no exact match, look for a substring match.
        if target_id is None:
            for item_id in self.inventory:
                itm = self.items[item_id]
                if query in itm.name.lower():
                    target_id, target_item = item_id, itm
                    break

        if target_id is None:
            print("You don't have that item.")
            return

        if isinstance(target_item, HealingItem):
            previous_health = self.player.health
            self.player.health = min(self.player.health + target_item.heal_amount, 20)
            actual_healed = self.player.health - previous_health

            # Remove the used healing item from inventory.
            self.inventory.remove(target_id)

            print(f"You use the {target_item.name} and recover {actual_healed} health.")
            print(f"Current health: {self.player.health}")
        else:
            print("You can't use that right now.")

    def equip_item(self, args: List[str]) -> None:
        """Equip a weapon or armor from the inventory using case‑insensitive name matching.

        The lookup first attempts an exact name match; if none is found it falls back to a
        substring (partial) match. Weapons are equipped as the player's weapon and
        armors as the player's armor. Items that cannot be equipped produce an
        informative message.
        """
        if not args:
            print("Equip what?")
            return

        query = " ".join(args).strip().lower()

        # Search for an exact match in the inventory first.
        target_id = None
        target_item = None
        for item_id in self.inventory:
            itm = self.items[item_id]
            if itm.name.lower() == query:
                target_id, target_item = item_id, itm
                break

        # If no exact match, look for a substring match.
        if target_id is None:
            for item_id in self.inventory:
                itm = self.items[item_id]
                if query in itm.name.lower():
                    target_id, target_item = item_id, itm
                    break

        if target_id is None:
            print("You don't have that item.")
            return

        if isinstance(target_item, Weapon):
            self.player.weapon = target_item
            self.equipment["weapon"] = target_id
            print(f"You equip the {target_item.name} as your weapon.")
        elif isinstance(target_item, Armor):
            self.player.armor = target_item
            self.equipment["armor"] = target_id
            print(f"You equip the {target_item.name} as armor.")
        else:
            print("That item can't be equipped.")

    # ----------------------------------------------------------------------
    # NPC interaction
    # ----------------------------------------------------------------------
    def talk_to(self, args: List[str]) -> None:
        if not args:
            print("Talk to whom?")
            return
        # Accept "to <npc>" or just "<npc>"
        if args[0] == "to" and len(args) > 1:
            name = " ".join(args[1:])
        else:
            name = " ".join(args)
        room = self.current_room()
        for npc_id in room.npcs:
            npc = self.npcs[npc_id]
            if npc.name.lower() == name:
                node = self.npc_progress.get(npc_id, "start")
                text = npc.dialogue.get(node, "")
                print(f'{npc.name} says: "{text}"')
                # Simple progression: if there is a 'next' node, move to it
                next_node = f"{node}_next"
                if next_node in npc.dialogue:
                    self.npc_progress[npc_id] = next_node
                return
        print("No one here by that name.")

    # ----------------------------------------------------------------------
    # Combat
    # ----------------------------------------------------------------------
    def initiate_combat(self, args: List[str]) -> None:
        """Handle combat initiation and run an interactive combat loop.

        The player can issue commands each turn. Recognized command:
        - ``flee``: ends combat immediately, leaving the monster alive in the room.
        Any other command is treated as a basic attack.
        """
        if not args:
            print("Attack what?")
            return

        target_name = " ".join(args).lower()
        room = self.current_room()

        # Locate the monster matching the target name
        target_id = None
        for monster_id in list(room.monsters):
            monster_obj = self.monsters.get(monster_id)
            if not monster_obj:
                continue  # stale reference, ignore
            if monster_obj.name.lower() == target_name:
                target_id = monster_id
                break

        if not target_id:
            print("No such monster here.")
            return

        monster = self.monsters[target_id]
        print(f"You engage the {monster.name}!")

        # Interactive combat loop
        while monster.health > 0 and self.player.health > 0:
            # Prompt player for a combat command
            raw = input("> ").strip()
            if not raw:
                continue

            cmd = parse_command(raw)

            # Handle flee command
            if cmd.action == "flee":
                print("You flee from combat.")
                # Combat ends; monster stays in the room
                break

            # Default to a basic attack
            dmg = self.player.attack
            monster.health -= dmg
            print(
                f"You strike for {dmg} damage. {monster.name} health is now {max(monster.health, 0)}."
            )
            if monster.health <= 0:
                print(f"The {monster.name} collapses!")
                self.defeated_monsters.add(monster.id)
                if monster.id in room.monsters:
                    room.monsters.remove(monster.id)
                break

            # Monster's turn (only if still alive)
            behavior = monster.behavior
            if behavior == "aggressive":
                self.player.health -= monster.attack
                print(
                    f"The {monster.name} attacks you for {monster.attack} damage. Your health is now {self.player.health}."
                )
            elif behavior == "defensive":
                if random.random() < 0.5:
                    self.player.health -= monster.attack
                    print(
                        f"The {monster.name} manages to hit you for {monster.attack} damage. Your health is now {self.player.health}."
                    )
                else:
                    print(f"The {monster.name} holds back this turn.")
            elif behavior == "coward":
                if random.random() < 0.3:
                    print(f"The {monster.name} flees in terror!")
                    if monster.id in room.monsters:
                        room.monsters.remove(monster.id)
                    break
                else:
                    self.player.health -= monster.attack
                    print(
                        f"The {monster.name} attacks you for {monster.attack} damage. Your health is now {self.player.health}."
                    )
            elif behavior == "boss":
                # Two‑phase boss logic
                if monster.health <= monster.max_health // 2 and not getattr(
                    monster, "phase_two", False
                ):
                    monster.phase_two = True
                    monster.attack += 4
                    print(
                        f"The {monster.name} roars! It enters a furious second phase, increasing its attack!"
                    )
                # Check for weakness item in inventory
                if self.equipment["weapon"] == "boss_key":
                    extra = 15
                    monster.health -= extra
                    print(
                        f"The {monster.name} shudders as the key's power strikes! Extra {extra} damage."
                    )
                self.player.health -= monster.attack
                print(
                    f"The {monster.name} slashes you for {monster.attack} damage. Your health is now {self.player.health}."
                )
            else:
                # Default attack behavior
                self.player.health -= monster.attack
                print(
                    f"The {monster.name} attacks you for {monster.attack} damage. Your health is now {self.player.health}."
                )

        # Post‑combat outcome handling
        if self.player.health <= 0:
            print("\nYou have fallen in battle. Game over.")
            self.write_save()
            exit(0)

    # ----------------------------------------------------------------------
    # Main loop entry point
    # ----------------------------------------------------------------------
    def run(self) -> None:
        """Start the interactive game loop."""
        print("\n--- Adventure Begins ---")
        self.describe_current_location()
        while True:
            try:
                raw = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\nExiting game.")
                self.write_save()
                break
            continue_game = self.handle_command(raw)
            if not continue_game:
                break


def __init__(self) -> None:
    """
    Initialise a new Game instance.

    This constructor sets up the game's world, player state, and starts the
    player in the 'start' room.  It no longer attempts to automatically load a
    saved game; loading must be performed explicitly via the appropriate
    command (e.g., a "load" command that calls ``load_state``).
    """
    # Initialise core attributes
    self.rooms: dict[str, Room] = {}
    self.player: Player = Player()
    self._current_room_key: str = "start"

    # Build the game world – this mirrors the original initialisation logic
    # but deliberately omits any automatic loading from SAVE_PATH.
    self._create_world()

    # Ensure the player starts in the designated start room
    if self._current_room_key not in self.rooms:
        raise RuntimeError(
            f"Start room '{self._current_room_key}' was not created during world setup."
        )
    self.player.location = self.rooms[self._current_room_key]

    # No automatic load_state call – saved games must be loaded explicitly.
