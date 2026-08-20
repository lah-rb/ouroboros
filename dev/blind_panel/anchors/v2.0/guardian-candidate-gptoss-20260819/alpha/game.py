"""Core game engine.

Coordinates the static world (rooms, items, NPCs, monsters), mutable world
state, player actions, combat, UI rendering and persistence.
"""

from __future__ import annotations

from typing import Any, Dict, List

from parser import Command, parse_command
from world import load_world, init_world_state
from combat import CombatEngine
from save_load import save_game, load_game
from ui import (
    display_help,
    display_status,
    display_room,
    display_combat_turn,
    display_defeat,
    display_victory,
)

from entities import Player, Room, Monster, Item, NPC


class GameEngine:
    """Main game loop and command dispatcher."""

    def __init__(self) -> None:
        # Load static world data
        world_data = load_world()
        self.rooms: Dict[str, Room] = world_data["rooms"]
        self.items: Dict[str, Item] = world_data["items"]
        self.npcs: Dict[str, NPC] = world_data["npcs"]
        self.monsters: Dict[str, Monster] = world_data["monsters"]

        # Initialise mutable world state (items in rooms, monster flags, NPC dialogue)
        self.world_state: Dict[str, Any] = init_world_state(self.rooms)

        # Create the player
        name = input("Enter your character's name: ").strip() or "Adventurer"
        start_room_id = self._choose_start_room()
        self.player: Player = Player.create(name=name, location=start_room_id)

        # Show initial room description
        self._display_current_room()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run(self) -> None:
        """Run the interactive command loop."""
        while True:
            try:
                raw = input("\n> ")
            except EOFError:
                # End of input stream; exit the game loop gracefully.
                break

            command = parse_command(raw)
            continue_game = self._process_command(command)
            if not continue_game:
                break

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _choose_start_room(self) -> str:
        """Pick a sensible starting room id.

        Preference is given to a room with id ``entrance``; otherwise the first
        key in the rooms dict is used.
        """
        if "entrance" in self.rooms:
            return "entrance"
        # deterministic fallback: sorted order of keys
        return sorted(self.rooms.keys())[0]

    def _current_room(self) -> Room:
        """Return the static :class:`Room` object where the player currently is."""
        return self.rooms[self.player.location]

    def _room_state(self, room_id: str) -> Dict[str, Any]:
        """Convenient accessor for mutable state of a given room."""
        return self.world_state["rooms"][room_id]

    def _display_current_room(self) -> None:
        """Render the current room using UI helpers."""
        room = self._current_room()
        display_room(
            room,
            world_state=self.world_state,
            items_lookup=self.items,
            npcs_lookup=self.npcs,
        )

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------
    def _process_command(self, command: Command) -> bool:
        """Execute a parsed command.

        Returns ``True`` to continue the game loop, ``False`` to exit.
        """
        verb = command.verb
        args = command.args

        if verb == "noop":
            return True

        if verb == "unknown":
            print("I don't understand that command.")
            return True

        if verb == "help":
            display_help()
            return True

        if verb == "status":
            display_status(self.player)
            return True

        if verb == "look":
            self._display_current_room()
            return True

        if verb == "move":
            if not args:
                print("Move where? Specify a direction.")
                return True
            self._move_player(args[0])
            return True

        if verb == "inventory":
            self._show_inventory()
            return True

        if verb == "take":
            if not args:
                print("Take what? Specify an item id.")
                return True
            self._take_item(args[0])
            return True

        if verb == "drop":
            if not args:
                print("Drop what? Specify an item id.")
                return True
            self._drop_item(args[0])
            return True

        if verb == "equip":
            if not args:
                print("Equip what? Specify an item id.")
                return True
            self._equip_item(args[0])
            return True

        if verb == "unequip":
            if not args:
                print("Unequip which slot? (weapon/armor)")
                return True
            self._unequip_slot(args[0])
            return True

        if verb == "use":
            if not args:
                print("Use what? Specify an item id.")
                return True
            self._use_item(args[0])
            return True

        if verb == "talk":
            if not args:
                print("Talk to whom? Specify an NPC id.")
                return True
            self._talk_to_npc(args[0])
            return True

        if verb == "attack":
            return self._handle_combat()

        if verb == "flee":
            # If a monster is present, start combat handling which supports fleeing
            current_room = self._current_room()
            if getattr(current_room, "monster", None):
                return self._handle_combat()
            print("There's nothing to flee from.")
            return True

        if verb == "examine":
            if not args:
                print("Examine what? Specify an item id or 'room'.")
                return True
            return self._handle_examine(args)

        if verb == "save":
            self._save_game()
            return True

        if verb == "load":
            self._load_game()
            # After loading, show the current room and status
            self._display_current_room()
            display_status(self.player)
            return True

        if verb == "restart":
            print("Restarting game...")
            new_engine = GameEngine()
            new_engine.run()
            return False  # stop the current loop; new engine takes over

        if verb == "quit":
            print("Goodbye!")
            return False

        # Fallback for any other verb (should not happen)
        print(f"The command '{verb}' is not implemented yet.")
        return True

    # ------------------------------------------------------------------
    # Movement
    # ------------------------------------------------------------------
    def _move_player(self, direction: str) -> None:
        """Attempt to move the player in ``direction``."""
        room = self._current_room()
        exits = room.exits
        if direction not in exits:
            print(f"You can't go '{direction}' from here.")
            return
        new_room_id = exits[direction]
        self.player.location = new_room_id
        self._display_current_room()

    # ------------------------------------------------------------------
    # Inventory handling
    # ------------------------------------------------------------------
    def _show_inventory(self) -> None:
        """Print a list of items currently carried."""
        if not self.player.inventory:
            print("Your inventory is empty.")
            return

        print("You are carrying:")
        for item_id in self.player.inventory:
            item = self.items.get(item_id)
            name = item.name if item else item_id
            print(f" - {name} ({item_id})")

    def _take_item(self, item_id: str) -> None:
        """Pick up an item from the current room."""
        room_state = self._room_state(self.player.location)
        if item_id not in room_state["items"]:
            print(f"There is no '{item_id}' here.")
            return
        # Remove from room and add to inventory
        room_state["items"].remove(item_id)
        self.player.add_item(item_id)
        item = self.items.get(item_id)
        name = item.name if item else item_id
        print(f"You take the {name}.")

    def _drop_item(self, item_id: str) -> None:
        """Leave an item in the current room."""
        # Attempt to remove the item from the player's inventory.
        if not self.player.remove_item(item_id):
            print(f"You don't have '{item_id}' in your inventory.")
            return

        # Add the item to the current room's mutable state.
        room_state = self._room_state(self.player.location)
        room_state.setdefault("items", []).append(item_id)

        # Resolve a friendly name for feedback.
        item = self.items.get(item_id)
        name = item.name if item else item_id
        print(f"You drop the {name}.")

    def _equip_item(self, item_id: str) -> None:
        """Equip a weapon or armor from inventory and update player stats."""
        # Ensure the player actually possesses the item.
        if item_id not in self.player.inventory:
            print(f"You don't have '{item_id}' to equip.")
            return

        # Retrieve the Item object from the master item registry.
        item = self.items.get(item_id)
        if not item:
            print(f"Item '{item_id}' does not exist in the world data.")
            return

        # Import the enum locally to avoid changing module‑level imports.
        from entities import ItemType

        # Determine which equipment slot the item belongs to via enum comparison.
        if item.type == ItemType.WEAPON:
            slot = "weapon"
        elif item.type == ItemType.ARMOR:
            slot = "armor"
        else:
            print(f"The {item.name} cannot be equipped.")
            return

        # If the item is already equipped in that slot, inform the player.
        current_equipped = self.player.equipment.get(slot)
        if current_equipped == item_id:
            print(f"The {item.name} is already equipped as your {slot}.")
            return

        # Remove stat bonuses from any previously equipped item in this slot.
        if current_equipped:
            old_item = self.items.get(current_equipped)
            if old_item:
                self.player.attack -= old_item.stats.get("attack", 0)
                self.player.defense -= old_item.stats.get("defense", 0)

        # Equip the new item using the player's equip helper.
        self.player.equip(item_id, slot)

        # Apply the new item's stat bonuses.
        self.player.attack += item.stats.get("attack", 0)
        self.player.defense += item.stats.get("defense", 0)

        print(f"You equip the {item.name} as your {slot}.")

    def _unequip_slot(self, slot: str) -> None:
        """Remove equipment from a slot and update player stats."""
        # Validate slot name.
        if slot not in ("weapon", "armor"):
            print("Invalid slot. Choose 'weapon' or 'armor'.")
            return

        equipped_id = self.player.equipment.get(slot)
        if not equipped_id:
            print(f"You have nothing equipped in the {slot} slot.")
            return

        # Retrieve the Item object for stat adjustment.
        item = self.items.get(equipped_id)

        # Subtract the item's stat bonuses from the player.
        if item:
            self.player.attack -= item.stats.get("attack", 0)
            self.player.defense -= item.stats.get("defense", 0)

        # Perform the actual unequip operation.
        self.player.unequip(slot)

        # Friendly name for output.
        name = item.name if item else equipped_id
        print(f"You unequip the {name} from your {slot} slot.")

    def _use_item(self, item_id: str) -> None:
        """Consume a consumable item (e.g., healing potion).

        The item is removed from the player's inventory only if it actually
        restores a positive amount of health.
        """
        # Verify the player actually has the item.
        if item_id not in self.player.inventory:
            print(f"You don't have '{item_id}' to use.")
            return

        # Retrieve the Item object from the master item list.
        item = self.items.get(item_id)
        if not item:
            print(f"Item '{item_id}' does not exist.")
            return

        # Import the enum locally to avoid altering module‑level imports.
        from entities import ItemType

        # Ensure the item is a consumable; otherwise it cannot be used now.
        if item.type != ItemType.CONSUMABLE:
            print(f"The {item.name} cannot be used right now.")
            return

        # Determine how much health the consumable restores.
        heal_amount = int(item.stats.get("heal", 0))
        if heal_amount <= 0:
            print(f"The {item.name} has no effect.")
            # No health restored → do not remove the item.
            return

        new_health = min(self.player.max_health, self.player.health + heal_amount)
        healed = new_health - self.player.health
        if healed <= 0:
            # Player already at max health; nothing is restored.
            print(f"You are already at full health; the {item.name} has no effect.")
            return

        # Apply healing.
        self.player.health = new_health
        print(f"You use the {item.name} and recover {healed} health.")

        # Remove the consumable from inventory after it has been used.
        if hasattr(self.player, "remove_item"):
            self.player.remove_item(item_id)
        else:
            # Fallback: directly modify the inventory list.
            try:
                self.player.inventory.remove(item_id)
            except ValueError:
                pass

    def _handle_examine(self, args: List[str]) -> bool:
        """Examine an item in the inventory, the current room, or the room itself.

        The command may be given as one or more words (e.g. ``examine steel sword``)
        or quoted strings (e.g. ``examine "Sharp Steel Sword"``).  All arguments are
        joined, surrounding quotes are stripped, and the resulting target is matched
        against both item IDs **and** item display names (case‑insensitive).

        Returns ``True`` to keep the main game loop running.
        """
        # Combine arguments into a single target string and normalize it.
        raw_target = " ".join(args).strip()
        # Strip surrounding quotes if present.
        if (raw_target.startswith('"') and raw_target.endswith('"')) or (
            raw_target.startswith("'") and raw_target.endswith("'")
        ):
            raw_target = raw_target[1:-1].strip()
        target = raw_target.lower()

        # Examine the room itself.
        if target == "room":
            self._display_current_room()
            return True

        # Helper to safely retrieve an Item object by its ID.
        def get_item(item_id: str) -> Item | None:
            itm = self.items.get(item_id)
            if not itm:
                print(f"Item data for '{item_id}' is missing.")
            return itm

        # ------------------------------------------------------------------
        # 1. Direct lookup by ID in the player's inventory.
        # ------------------------------------------------------------------
        if target in self.player.inventory:
            item = get_item(target)
            if item:
                self._print_item_details(item)
            return True

        # ------------------------------------------------------------------
        # 2. Direct lookup by ID among items present in the current room.
        # ------------------------------------------------------------------
        room_state = self._room_state(self.player.location)
        if target in room_state.get("items", []):
            item = get_item(target)
            if item:
                self._print_item_details(item)
            return True

        # ------------------------------------------------------------------
        # 3. Match by display name (case‑insensitive) among visible items.
        # ------------------------------------------------------------------
        visible_item_ids = set(self.player.inventory)
        visible_item_ids.update(room_state.get("items", []))

        for item_id in visible_item_ids:
            item = self.items.get(item_id)
            if not item:
                continue
            if item.name.lower() == target:
                self._print_item_details(item)
                return True

        # ------------------------------------------------------------------
        # No matching item found.
        # ------------------------------------------------------------------
        print(f"There is no '{raw_target}' here to examine.")
        return True

    def _print_item_details(self, item: Item) -> None:
        """Display detailed information about an ``Item``."""
        # Header with the item's name.
        print(f"{item.name}:")

        # Show description if available; otherwise show the enum name of the type.
        description = getattr(item, "description", None)
        if description:
            print(description)
        else:
            # Import the enum locally to avoid altering module‑level imports.
            from entities import ItemType

            # If the type is an ItemType enum, display its name; otherwise fall back to raw value.
            item_type_display = (
                item.type.name
                if isinstance(item.type, ItemType)
                else str(item.type)
            )
            print(f"Type: {item_type_display}")

        # If the item defines stats, format them as a comma‑separated list.
        if item.stats:
            stats_str = ", ".join(f"{key}={value}" for key, value in item.stats.items())
            print(f"Stats: {stats_str}")

    # ------------------------------------------------------------------
    # NPC interaction
    # ------------------------------------------------------------------
    def _talk_to_npc(self, npc_id: str) -> None:
        """Engage in dialogue with an NPC present in the room."""
        room = self._current_room()
        if npc_id not in room.npcs:
            print(f"There is no NPC '{npc_id}' here.")
            return
        npc = self.npcs.get(npc_id)
        if not npc:
            print(f"NPC data for '{npc_id}' is missing.")
            return

        # Retrieve or initialise dialogue state for this NPC
        room_state = self._room_state(room.id)
        npc_state = room_state.setdefault("npc_state", {})
        state = npc_state.setdefault(npc_id, {"dialogue_index": 0})
        idx = state["dialogue_index"]

        if not npc.dialogue:
            print(f"{npc.name} has nothing to say.")
            return

        # Cycle through dialogue entries; for simplicity we ignore triggers
        entry = npc.dialogue[idx % len(npc.dialogue)]
        for line in entry.lines:
            print(f"{npc.name}: {line}")

        # Advance dialogue index for next interaction
        state["dialogue_index"] = (idx + 1) % len(npc.dialogue)

    # ------------------------------------------------------------------
    # Combat handling
    # ------------------------------------------------------------------
    def _handle_combat(self) -> bool:
        """Enter combat with the monster in the current room, if any.

        Returns ``True`` to continue the game loop, ``False`` if the player
        is defeated (game over) or the final boss is slain (victory).
        """
        room = self._current_room()
        monster_id = room.monster
        if not monster_id:
            print("There is no monster here to attack.")
            return True

        room_state = self._room_state(room.id)
        if room_state.get("monster_defeated", False):
            print("The corpse of the monster lies still.")
            return True

        monster = self.monsters.get(monster_id)
        if not monster:
            print(f"Monster data for '{monster_id}' is missing.")
            return True

        combat_engine = CombatEngine(self.player, monster)

        while True:
            raw = input("\nCombat> ")
            cmd = parse_command(raw)
            player_action = cmd.verb

            # Allow the player to flee out of combat
            if player_action == "flee":
                print("You retreat from the fight.")
                break

            # Handle consumable item usage during combat
            if player_action == "use":
                if not cmd.args:
                    print("Use what? Specify an item ID.")
                    continue
                item_id = cmd.args[0]
                self._use_item(item_id)
                # After using an item, skip the monster's turn for this round
                continue

            turn_result = combat_engine.take_turn(player_action)

            display_combat_turn(turn_result, self.player, monster)

            if turn_result.get("player_defeated"):
                display_defeat()
                return False  # Game over

            if turn_result.get("monster_defeated"):
                # Update world state to reflect the slain monster
                room_state["monster_defeated"] = True
                self.world_state.setdefault("defeated_monsters", []).append(monster_id)

                # Drop loot into the room
                for drop_id in monster.drops:
                    room_state["items"].append(drop_id)
                print(f"The {monster.name} collapses, dropping its loot.")

                # Victory condition: if this monster is a boss (behavior == "boss")
                if monster.behavior.lower() == "boss":
                    display_victory()
                    return False  # End the game after victory
                break  # Exit combat loop after monster defeat

            # If combat continues, loop for next player action
        return True

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _save_game(self) -> None:
        """Serialise current game state to JSON."""
        state = {
            "player": self.player.to_dict(),
            "world": self.world_state,
        }
        save_game(state)
        print("Game saved successfully.")

    def _load_game(self) -> None:
        """Load a previously saved game state."""
        loaded = load_game()
        # Reconstruct player object
        self.player = Player.from_dict(loaded["player"])
        # Restore mutable world state
        self.world_state = loaded["world"]
        print("Game loaded successfully.")


__all__ = ["GameEngine"]
