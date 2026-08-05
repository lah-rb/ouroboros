from typing import Dict, Optional, Union, Any
from models import (
    Player,
    Room,
    Weapon,
    Armor,
    HealingItem,
    Monster,
    Boss,
    GameState,
    Direction,
    EquipmentSlot,
)
from parser import Command, CommandType, parse_input
from combat import CombatEngine
from game_state import GameStateManager, save_game
import sys


class GameEngine:
    def __init__(self, world_data: Dict[str, Any]):
        self.rooms = world_data["rooms"]
        self.items = world_data["items"]
        self.npcs = world_data["npcs"]
        self.monsters = world_data["monsters"]
        self.boss = world_data["boss"]
        self.start_room_id = world_data["start_room_id"]

        # Initialize game state
        player = Player()
        room_states = {}
        for room_id, room in self.rooms.items():
            room_states[room_id] = {
                "items": room.items.copy(),
                "npcs": room.npcs.copy(),
                "monster": room.monster,
                "boss": room.boss,
            }

        self.state_manager = GameStateManager(
            GameState(
                player=player,
                current_room_id=self.start_room_id,
                inventory=[],
                equipped={},
                room_states=room_states,
                npc_dialogue_progress={},
                defeated_monsters=set(),
                boss_defeated=False,
            )
        )

    def get_current_room(self) -> Room:
        return self.rooms[self.state_manager.state.current_room_id]

    def get_current_room_state(self) -> Dict[str, Any]:
        room_id = self.state_manager.state.current_room_id
        return self.state_manager.state.room_states.get(
            room_id, {"items": [], "npcs": [], "monster": None, "boss": None}
        )

    def render_room(self):
        room = self.get_current_room()
        room_state = self.get_current_room_state()

        print(f"\n{room.title}")
        print("-" * len(room.title))
        print(room.description)

        # List items
        if room_state["items"]:
            print("\nYou see:")
            for item_id in room_state["items"]:
                if item_id in self.items:
                    print(f"  - {self.items[item_id].name}")

        # List NPCs
        if room_state["npcs"]:
            print("\nPresent:")
            for npc_id in room_state["npcs"]:
                if npc_id in self.npcs:
                    print(f"  - {self.npcs[npc_id].name}")

        # List monsters/boss
        if (
            room_state["monster"]
            and room_state["monster"] not in self.state_manager.state.defeated_monsters
        ):
            monster = self.monsters[room_state["monster"]]
            print(f"\nA {monster.name} blocks your path!")

        if room_state["boss"] and not self.state_manager.state.boss_defeated:
            print(f"\n{self.boss.name} stands before you, ready for battle!")

        # List exits
        print("\nExits:")
        for direction, target in room.exits.items():
            print(f"  - {direction.capitalize()} to {self.rooms[target].title}")

    def render_inventory(self):
        inventory = self.state_manager.state.inventory
        if not inventory:
            print("\nYour inventory is empty.")
            return

        print("\nInventory:")
        for item_id in inventory:
            if item_id in self.items:
                item = self.items[item_id]
                equipped = ""
                if (
                    EquipmentSlot.WEAPON in self.state_manager.state.equipped
                    and self.state_manager.state.equipped[EquipmentSlot.WEAPON]
                    == item_id
                ):
                    equipped = " (equipped as weapon)"
                elif (
                    EquipmentSlot.ARMOR in self.state_manager.state.equipped
                    and self.state_manager.state.equipped[EquipmentSlot.ARMOR]
                    == item_id
                ):
                    equipped = " (equipped as armor)"
                print(f"  - {item.name}{equipped}")

    def render_status(self):
        player = self.state_manager.state.player
        equipped_weapon = None
        equipped_armor = None

        if "weapon" in self.state_manager.state.equipped:
            weapon_id = self.state_manager.state.equipped["weapon"]
            if weapon_id in self.items and isinstance(self.items[weapon_id], Weapon):
                equipped_weapon = self.items[weapon_id]

        if "armor" in self.state_manager.state.equipped:
            armor_id = self.state_manager.state.equipped["armor"]
            if armor_id in self.items and isinstance(self.items[armor_id], Armor):
                equipped_armor = self.items[armor_id]

        weapon_damage = equipped_weapon.damage if equipped_weapon else 0
        armor_defense = equipped_armor.defense if equipped_armor else 0

        print(f"\nHealth: {player.health}/{player.max_health}")
        print(f"Attack: {player.base_attack} + {weapon_damage} (from weapon)")
        print(f"Defense: {player.base_defense} + {armor_defense} (from armor)")

    def render_help(self):
        print("\nAvailable commands:")
        print("  Movement: go [direction], north/south/east/west/n/s/e/w")
        print("  Items: take [item], drop [item], use [item], examine [item]")
        print("  Interaction: talk to [npc], attack [monster]")
        print("  Game: look, inventory, status, help, quit")
        print("  Save/Load: save, load")

    def handle_movement(self, direction: Direction):
        room = self.get_current_room()
        direction_map = {
            Direction.NORTH: "north",
            Direction.SOUTH: "south",
            Direction.EAST: "east",
            Direction.WEST: "west",
            Direction.UP: "up",
            Direction.DOWN: "down",
        }
        dir_str = direction_map[direction]

        if dir_str in room.exits:
            target_room_id = room.exits[dir_str]
            self.state_manager.move_player(target_room_id)
            print(f"You move {dir_str} to the {self.rooms[target_room_id].title}.")
            self.render_room()
        else:
            print("You can't go that way.")

    def handle_take(self, item_name: str):
        room_state = self.get_current_room_state()
        # Try to match item by name or ID
        for item_id in room_state["items"]:
            if (
                item_id == item_name.lower()
                or self.items[item_id].name.lower() == item_name.lower()
            ):
                self.state_manager.add_to_inventory(item_id)
                self.state_manager.remove_item_from_room(
                    self.state_manager.state.current_room_id, item_id
                )
                print(f"You take the {self.items[item_id].name}.")
                return

        print(f"There is no '{item_name}' here to take.")

    def handle_drop(self, item_name: str):
        inventory = self.state_manager.state.inventory
        # Try to match item by name or ID
        for item_id in inventory[:]:  # Copy to avoid modification during iteration
            if (
                item_id == item_name.lower()
                or self.items[item_id].name.lower() == item_name.lower()
            ):
                # Unequip if equipped
                if (
                    EquipmentSlot.WEAPON in self.state_manager.state.equipped
                    and self.state_manager.state.equipped[EquipmentSlot.WEAPON]
                    == item_id
                ):
                    self.state_manager.unequip_item(EquipmentSlot.WEAPON)
                if (
                    EquipmentSlot.ARMOR in self.state_manager.state.equipped
                    and self.state_manager.state.equipped[EquipmentSlot.ARMOR]
                    == item_id
                ):
                    self.state_manager.unequip_item(EquipmentSlot.ARMOR)

                self.state_manager.remove_from_inventory(item_id)
                self.state_manager.add_item_to_room(
                    self.state_manager.state.current_room_id, item_id
                )
                print(f"You drop the {self.items[item_id].name}.")
                return

        print(f"You don't have '{item_name}' in your inventory.")

    def handle_use(self, item_name: str):
        inventory = self.state_manager.state.inventory
        # Try to match item by name or ID
        for item_id in inventory:
            if (
                item_id == item_name.lower()
                or self.items[item_id].name.lower() == item_name.lower()
            ):
                item = self.items[item_id]
                if isinstance(item, HealingItem):
                    new_health = min(
                        self.state_manager.state.player.max_health,
                        self.state_manager.state.player.health + item.heal_amount,
                    )
                    old_health = self.state_manager.state.player.health
                    self.state_manager.update_player_health(new_health)
                    print(
                        f"You use {item.name} and heal for {item.heal_amount} health."
                    )
                    self.state_manager.remove_from_inventory(item_id)
                elif isinstance(item, Weapon):
                    # Equip the weapon in the weapon slot
                    self.state_manager.equip_item("weapon", item_id)
                    print(f"You equip {item.name} as your weapon.")
                else:
                    print(f"You can't use {item.name} that way.")
                return

        print(f"You don't have '{item_name}' in your inventory.")

    def handle_examine(self, target: str):
        # Check items in room
        room_state = self.get_current_room_state()
        for item_id in room_state["items"]:
            if (
                item_id == target.lower()
                or self.items[item_id].name.lower() == target.lower()
            ):
                print(
                    f"\n{self.items[item_id].name}: {self.items[item_id].description}"
                )
                return

        # Check items in inventory
        for item_id in self.state_manager.state.inventory:
            if (
                item_id == target.lower()
                or self.items[item_id].name.lower() == target.lower()
            ):
                print(
                    f"\n{self.items[item_id].name}: {self.items[item_id].description}"
                )
                return

        # Check NPCs
        for npc_id in room_state["npcs"]:
            if (
                npc_id == target.lower()
                or self.npcs[npc_id].name.lower() == target.lower()
            ):
                print(f"\n{self.npcs[npc_id].name}: {self.npcs[npc_id].description}")
                return

        # Check monster
        if (
            room_state["monster"]
            and room_state["monster"] not in self.state_manager.state.defeated_monsters
        ):
            monster = self.monsters[room_state["monster"]]
            if (
                room_state["monster"] == target.lower()
                or monster.name.lower() == target.lower()
            ):
                print(f"\n{monster.name}: {monster.description}")
                return

        # Check boss
        if room_state["boss"] and not self.state_manager.state.boss_defeated:
            if (
                self.boss.id == target.lower()
                or self.boss.name.lower() == target.lower()
            ):
                print(f"\n{self.boss.name}: {self.boss.description}")
                return

        print(f"You don't see '{target}' here.")

    def handle_talk(self, npc_name: str):
        room_state = self.get_current_room_state()
        for npc_id in room_state["npcs"]:
            if (
                npc_id == npc_name.lower()
                or self.npcs[npc_id].name.lower() == npc_name.lower()
            ):
                npc = self.npcs[npc_id]
                progress = self.state_manager.state.npc_dialogue_progress.get(npc_id, 0)

                if progress < len(npc.dialogue):
                    dialogue = npc.dialogue[progress]
                    print(f"\n{npc.name} says: {dialogue['text']}")
                    self.state_manager.advance_npc_dialogue(npc_id)
                else:
                    print(f"\n{npc.name} has nothing more to say.")
                return

        print(f"There is no one named '{npc_name}' here to talk to.")

    def handle_attack(self, monster_name: Optional[str] = None):
        room_state = self.get_current_room_state()
        enemy = None
        enemy_id = None

        # Check for boss first
        if room_state["boss"] and not self.state_manager.state.boss_defeated:
            enemy = self.boss
            enemy_id = self.boss.id
        # Check for monster
        elif (
            room_state["monster"]
            and room_state["monster"] not in self.state_manager.state.defeated_monsters
        ):
            enemy = self.monsters[room_state["monster"]]
            enemy_id = room_state["monster"]
        else:
            print("There's nothing here to attack.")
            return

        # If specific monster was mentioned, verify it matches
        if monster_name:
            if (
                enemy_id != monster_name.lower()
                and enemy.name.lower() != monster_name.lower()
            ):
                print(f"There is no '{monster_name}' here to attack.")
                return

        print(f"\nYou engage the {enemy.name} in combat!")
        self._start_combat(enemy, enemy_id, is_boss=(room_state["boss"] is not None))

    def _start_combat(self, enemy: Union[Monster, Boss], enemy_id: str, is_boss: bool):
        player = self.state_manager.state.player
        combat_engine = CombatEngine(player, enemy)

        while True:
            # Player's turn
            print("\nWhat will you do? (attack, use [item], flee)")
            command_input = input("> ").strip().lower()
            command = parse_input(command_input)

            if command.type == CommandType.ATTACK:
                result = combat_engine.resolve_attack(
                    self.items, self.boss.weakness_item if is_boss else None
                )

                print(result.message)
                if result.phase_change:
                    print(f"The {enemy.name} enters phase 2!")

                # Update health
                self.state_manager.update_player_health(result.player_health)
                enemy.health = result.enemy_health

                if result.is_enemy_defeated:
                    if is_boss:
                        self.state_manager.set_boss_defeated()
                        print(
                            f"\nYou have defeated the {enemy.name}! The dungeon is free!"
                        )
                        print("\nCONGRATULATIONS! You have won the game!")
                        sys.exit(0)
                    else:
                        self.state_manager.add_defeated_monster(enemy_id)
                        self.state_manager.remove_monster_from_room(
                            self.state_manager.state.current_room_id
                        )
                        print(f"\nYou have defeated the {enemy.name}!")
                        self.render_room()
                        return

                if result.is_player_defeated:
                    print("\nYou have been defeated...")
                    print("\nGAME OVER")
                    print("Type 'quit' to exit or 'load' to try again.")
                    return

                # Enemy's turn (if still alive)
                if not result.is_enemy_defeated:
                    # Simple monster AI - just attack
                    enemy_attack = enemy.attack
                    if is_boss and combat_engine.phase2:
                        enemy_attack = self.boss.phase2_attack

                    player_defense = player.base_defense
                    if EquipmentSlot.ARMOR in self.state_manager.state.equipped:
                        armor_id = self.state_manager.state.equipped[
                            EquipmentSlot.ARMOR
                        ]
                        if armor_id in self.items and isinstance(
                            self.items[armor_id], Armor
                        ):
                            player_defense += self.items[armor_id].defense

                    damage = max(1, enemy_attack - player_defense // 2)
                    new_health = player.health - damage
                    self.state_manager.update_player_health(new_health)
                    print(f"The {enemy.name} hits you for {damage} damage.")

                    if new_health <= 0:
                        print("\nYou have been defeated...")
                        print("\nGAME OVER")
                        print("Type 'quit' to exit or 'load' to try again.")
                        return

            elif command.type == CommandType.USE:
                if not command.item_id:
                    print("Use what?")
                    continue

                # Find the item in inventory
                used_item = None
                for item_id in self.state_manager.state.inventory:
                    if item_id == command.item_id.lower() or (
                        item_id in self.items
                        and self.items[item_id].name.lower() == command.item_id.lower()
                    ):
                        if isinstance(self.items[item_id], HealingItem):
                            used_item = self.items[item_id]
                            result = combat_engine.use_healing_item(used_item)
                            print(result.message)
                            self.state_manager.update_player_health(
                                result.player_health
                            )
                            self.state_manager.remove_from_inventory(item_id)
                            break

                if not used_item:
                    print(f"You don't have '{command.item_id}' in your inventory.")
                    continue

            elif command.type == CommandType.FLEE:
                # 50% chance to flee successfully
                import random

                if random.random() < 0.5:
                    result = combat_engine.flee()
                    print(result.message)
                    print("You escaped successfully!")
                    self.render_room()
                    return
                else:
                    print("You failed to flee!")
                    # Enemy gets a free attack
                    enemy_attack = enemy.attack
                    if is_boss and combat_engine.phase2:
                        enemy_attack = self.boss.phase2_attack

                    player_defense = player.base_defense
                    if EquipmentSlot.ARMOR in self.state_manager.state.equipped:
                        armor_id = self.state_manager.state.equipped[
                            EquipmentSlot.ARMOR
                        ]
                        if armor_id in self.items and isinstance(
                            self.items[armor_id], Armor
                        ):
                            player_defense += self.items[armor_id].defense

                    damage = max(1, enemy_attack - player_defense // 2)
                    new_health = player.health - damage
                    self.state_manager.update_player_health(new_health)
                    print(
                        f"The {enemy.name} hits you for {damage} damage as you try to run."
                    )

                    if new_health <= 0:
                        print("\nYou have been defeated...")
                        print("\nGAME OVER")
                        print("Type 'quit' to exit or 'load' to try again.")
                        return

            elif command.type == CommandType.QUIT:
                print("You can't quit during combat! Fight or flee!")
                continue

            else:
                print("Invalid combat command. Try attack, use [item], or flee.")

    def handle_command(self, command: Command):
        if command.type == CommandType.MOVE:
            self.handle_movement(command.direction)
        elif command.type == CommandType.TAKE:
            self.handle_take(command.item_id or "")
        elif command.type == CommandType.DROP:
            self.handle_drop(command.item_id or "")
        elif command.type == CommandType.USE:
            self.handle_use(command.item_id or "")
        elif command.type == CommandType.EXAMINE:
            self.handle_examine(command.item_id or "")
        elif command.type == CommandType.TALK:
            self.handle_talk(command.npc_id or "")
        elif command.type == CommandType.ATTACK:
            self.handle_attack(command.monster_id)
        elif command.type == CommandType.FLEE:
            print("You can only flee during combat.")
        elif command.type == CommandType.LOOK:
            self.render_room()
        elif command.type == CommandType.INVENTORY:
            self.render_inventory()
        elif command.type == CommandType.STATUS:
            self.render_status()
        elif command.type == CommandType.HELP:
            self.render_help()
        elif command.type == CommandType.SAVE:
            save_game(self.state_manager.state)
            print("\nGame saved successfully.")
        elif command.type == CommandType.LOAD:
            from game_state import load_game

            loaded_state = load_game()
            if loaded_state:
                self.state_manager.state = loaded_state
                print("\nGame loaded successfully.")
                self.render_room()
            else:
                print("\nNo saved game found.")
        elif command.type == CommandType.QUIT:
            return False
        else:
            print(
                "I don't understand that command. Type 'help' for a list of commands."
            )

        return True

    def run(self):
        print("Welcome to the Dungeon Adventure!")
        print("Type 'help' for a list of commands.")
        self.render_room()

        while True:
            try:
                command_input = input("\n> ").strip()
                if not command_input:
                    continue

                command = parse_input(command_input)
                if command.type == CommandType.QUIT:
                    print("\nThanks for playing!")
                    break

                if not self.handle_command(command):
                    break

                # Check if player is dead
                if self.state_manager.state.player.health <= 0:
                    print("\nYou have died. Game over!")
                    print("Type 'load' to try again or 'quit' to exit.")
                    while True:
                        command_input = input("> ").strip().lower()
                        if command_input in ("quit", "exit", "q"):
                            print("\nThanks for playing!")
                            return
                        elif command_input in ("load", "l"):
                            from game_state import load_game

                            loaded_state = load_game()
                            if loaded_state:
                                self.state_manager.state = loaded_state
                                print("\nGame loaded successfully.")
                                self.render_room()
                                break
                            else:
                                print("\nNo saved game found.")
                        else:
                            print("Type 'load' to try again or 'quit' to exit.")

            except KeyboardInterrupt:
                print("\nThanks for playing!")
                break
            except EOFError:
                print("\nThanks for playing!")
                break
