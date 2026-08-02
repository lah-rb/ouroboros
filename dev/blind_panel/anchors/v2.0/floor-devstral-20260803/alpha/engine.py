import json
from typing import Dict
from models import Player, Monster
from parser import parse_command, Command


class GameEngine:
    def __init__(self, world_data: Dict, player_data: Player):
        self.world = world_data
        self.player = player_data
        self.current_room = self.world["rooms"][player_data.location]
        self.game_over = False
        self.victory = False
        self.completed_monsters = set()
        self.state = {
            "player": player_data,
            "world": world_data,
            "current_room": self.current_room,
            "game_over": False,
            "victory": False,
        }

    def run(self):
        print("Welcome to the Text Adventure Game!")
        print("Type 'help' for a list of commands.")
        while not self.game_over:
            self.show_room()
            command = input("> ").strip().lower()
            if not command:
                continue
            cmd = parse_command(command)
            self.handle_command(cmd)

    def show_room(self):
        print(f"\n{self.current_room.title}")
        print(self.current_room.description)
        print("\nExits:", ", ".join(self.current_room.exits.keys()))
        if self.current_room.items:
            print(
                "Items:",
                ", ".join(
                    [
                        self.world["items"][item_id].name
                        for item_id in self.current_room.items
                    ]
                ),
            )
        if self.current_room.monsters and not any(
            m in self.completed_monsters for m in self.current_room.monsters
        ):
            print(
                "Monsters:",
                ", ".join(
                    [self.world["monsters"][m].name for m in self.current_room.monsters]
                ),
            )

    def handle_command(self, cmd: Command):
        if cmd.action == "quit":
            self.game_over = True
            return
        elif cmd.action == "help":
            self.show_help()
        elif cmd.action == "look":
            self.show_room()
        elif cmd.action == "status":
            self.show_status()
        elif cmd.action in ["north", "south", "east", "west"]:
            self.move(cmd.action)
        elif cmd.action == "take" and cmd.target:
            self.take_item(cmd.target)
        elif cmd.action == "drop" and cmd.target:
            self.drop_item(cmd.target)
        elif cmd.action == "use" and cmd.target:
            self.use_item(cmd.target)
        elif cmd.action == "examine" and cmd.target:
            self.examine_item(cmd.target)
        elif cmd.action == "equip" and cmd.target:
            self.equip_item(cmd.target)
        elif cmd.action == "talk to" and cmd.target:
            self.talk_to_npc(cmd.target)
        elif cmd.action == "attack" and cmd.target:
            self.attack_monster(cmd.target)
        elif cmd.action == "flee":
            self.flee()
        else:
            print(
                "I don't understand that command. Type 'help' for a list of commands."
            )

    def show_help(self):
        print("\nAvailable commands:")
        print("  Movement: north, south, east, west")
        print(
            "  Inventory: take <item>, drop <item>, use <item>, examine <item>, equip <item>"
        )
        print("  Interaction: talk to <npc>, attack <monster>, flee")
        print("  Utilities: look, status, help, quit")

    def show_status(self):
        print("\nStatus:")
        print(f"Health: {self.player.health}")
        print(f"Attack: {self.player.attack}")
        print(f"Defense: {self.player.defense}")
        print(f"Location: {self.current_room.title}")
        if self.player.inventory:
            print(
                "Inventory:",
                ", ".join(
                    [
                        self.world["items"][item_id].name
                        for item_id in self.player.inventory
                    ]
                ),
            )
        if self.player.equipment["weapon"]:
            print(
                f"Weapon: {self.world['items'][self.player.equipment['weapon']].name}"
            )
        if self.player.equipment["armor"]:
            print(f"Armor: {self.world['items'][self.player.equipment['armor']].name}")

    def move(self, direction: str):
        if direction in self.current_room.exits:
            next_room_id = self.current_room.exits[direction]
            self.current_room = self.world["rooms"][next_room_id]
            self.player.location = next_room_id
            print(f"You move {direction}.")
            self.check_monsters()
        else:
            print("You can't go that way.")

    def take_item(self, item_name: str):
        for item_id in self.current_room.items:
            item = self.world["items"][item_id]
            if item.name.lower() == item_name.lower():
                self.player.inventory.append(item_id)
                self.current_room.items.remove(item_id)
                print(f"You took the {item.name}.")
                return
        print(f"There is no {item_name} here.")

    def drop_item(self, item_name: str):
        for item_id in self.player.inventory:
            item = self.world["items"][item_id]
            if item.name.lower() == item_name.lower():
                self.player.inventory.remove(item_id)
                self.current_room.items.append(item_id)
                print(f"You dropped the {item.name}.")
                return
        print(f"You don't have a {item_name}.")

    def use_item(self, item_name: str):
        for item_id in self.player.inventory:
            item = self.world["items"][item_id]
            if item.name.lower() == item_name.lower():
                if item.type == "healing":
                    self.player.health += item.stats.get("hp", 0)
                    print(
                        f"You used the {item.name} and restored {item.stats.get('hp', 0)} health."
                    )
                    self.player.inventory.remove(item_id)
                else:
                    print(f"You can't use the {item.name}.")
                return
        print(f"You don't have a {item_name}.")

    def examine_item(self, item_name: str):
        for item_id in self.player.inventory:
            item = self.world["items"][item_id]
            if item.name.lower() == item_name.lower():
                print(f"{item.name}: {item.description}")
                return
        print(f"You don't have a {item_name}.")

    def equip_item(self, item_name: str):
        for item_id in self.player.inventory:
            item = self.world["items"][item_id]
            if item.name.lower() == item_name.lower():
                if item.type == "weapon":
                    self.player.equipment["weapon"] = item_id
                    self.player.attack += item.stats.get("attack", 0)
                    print(f"You equipped the {item.name}.")
                elif item.type == "armor":
                    self.player.equipment["armor"] = item_id
                    self.player.defense += item.stats.get("defense", 0)
                    print(f"You equipped the {item.name}.")
                else:
                    print(f"You can't equip the {item.name}.")
                return
        print(f"You don't have a {item_name}.")

    def talk_to_npc(self, npc_name: str):
        # Remove leading 'to' if present in the NPC name
        cleaned_npc_name = npc_name.lower()
        if cleaned_npc_name.startswith("to "):
            cleaned_npc_name = cleaned_npc_name[3:]

        # Initialize dialogue state if not present
        if "dialogue_state" not in self.state:
            self.state["dialogue_state"] = {}

        # Check NPCs first
        for npc_id in self.current_room.npcs:
            npc = self.world["npcs"][npc_id]
            if npc.name.lower() == cleaned_npc_name:
                # Get current dialogue index for this NPC, default to 0
                current_index = self.state["dialogue_state"].get(npc_id, 0)

                # Check if there's more dialogue available and conditions are met
                if current_index < len(npc.dialogue):
                    dialogue_entry = npc.dialogue[current_index]

                    # Check if there are conditions for this dialogue line
                    if "condition" in dialogue_entry:
                        condition = dialogue_entry["condition"]
                        # Handle the 'talked_once' condition specifically
                        if condition == "talked_once":
                            # Check if player has talked to NPC before (current_index > 0)
                            if current_index <= 0:
                                # Skip to next dialogue line that doesn't have conditions or meets them
                                current_index += 1
                                while current_index < len(npc.dialogue):
                                    next_dialogue = npc.dialogue[current_index]
                                    if (
                                        "condition" not in next_dialogue
                                        or self._check_condition(
                                            next_dialogue["condition"],
                                            next_dialogue.get("item_name"),
                                        )
                                    ):
                                        break
                                    current_index += 1
                            # For other conditions, handle as before
                        elif condition == "has_item":
                            item_name = dialogue_entry.get("item_name", "")
                            has_item = any(
                                item.lower() == item_name.lower()
                                for item in self.state["player"].inventory
                            )
                            if not has_item:
                                # Skip to next dialogue line that doesn't have conditions or meets them
                                current_index += 1
                                while current_index < len(npc.dialogue):
                                    next_dialogue = npc.dialogue[current_index]
                                    if (
                                        "condition" not in next_dialogue
                                        or self._check_condition(
                                            next_dialogue["condition"],
                                            next_dialogue.get("item_name"),
                                        )
                                    ):
                                        break
                                    current_index += 1
                        elif condition == "killed_monster":
                            monster_name = dialogue_entry.get("monster_name", "")
                            killed_monsters = self.state.get("completed", {}).get(
                                "monsters", []
                            )
                            if monster_name.lower() not in [
                                m.lower() for m in killed_monsters
                            ]:
                                current_index += 1
                                while current_index < len(npc.dialogue):
                                    next_dialogue = npc.dialogue[current_index]
                                    if (
                                        "condition" not in next_dialogue
                                        or self._check_condition(
                                            next_dialogue["condition"],
                                            next_dialogue.get("monster_name"),
                                        )
                                    ):
                                        break
                                    current_index += 1
                        elif condition == "visited_room":
                            room_id = dialogue_entry.get("room_id", "")
                            visited_rooms = self.state.get("visited_rooms", [])
                            if room_id not in visited_rooms:
                                current_index += 1
                                while current_index < len(npc.dialogue):
                                    next_dialogue = npc.dialogue[current_index]
                                    if (
                                        "condition" not in next_dialogue
                                        or self._check_condition(
                                            next_dialogue["condition"],
                                            next_dialogue.get("room_id"),
                                        )
                                    ):
                                        break
                                    current_index += 1
                        else:
                            # Unknown condition, skip to next line
                            current_index += 1
                            continue

                    # Display the current dialogue line
                    print(f"{npc.name}: {dialogue_entry['text']}")

                    # Increment the dialogue index for next time
                    self.state["dialogue_state"][npc_id] = current_index + 1
                else:
                    # All dialogue lines have been shown
                    print(f"{npc.name}: I have nothing more to say right now.")

                return

        # Check monsters next
        for monster_id in self.current_room.monsters:
            monster = self.world["monsters"][monster_id]
            if monster.name.lower() == cleaned_npc_name:
                # Simple dialogue response for monsters
                print(f"{monster.name}: {monster.name} growls at you.")
                return

        print(f"There is no {npc_name} here.")

    def _check_condition(self, condition: str, target: str) -> bool:
        """Helper method to check conditions for dialogue branching."""
        if condition == "has_item":
            return any(
                item.lower() == target.lower()
                for item in self.state["player"].inventory
            )
        elif condition == "killed_monster":
            killed_monsters = self.state.get("completed", {}).get("monsters", [])
            return target.lower() in [m.lower() for m in killed_monsters]
        elif condition == "visited_room":
            visited_rooms = self.state.get("visited_rooms", [])
            return target in visited_rooms
        return False

    def attack_monster(self, monster_name: str):
        for monster_id in self.current_room.monsters:
            monster = self.world["monsters"][monster_id]
            if monster.name.lower() == monster_name.lower():
                print(f"You attack the {monster.name}!")
                damage = max(1, self.player.attack - monster.defense)
                monster.hp -= damage
                if monster.hp <= 0:
                    print(f"You defeated the {monster.name}!")
                    self.completed_monsters.add(monster_id)
                    self.current_room.monsters.remove(monster_id)
                    if monster_id == "boss":
                        self.victory = True
                        self.game_over = True
                else:
                    print(f"The {monster.name} has {monster.hp} health remaining.")
                    self.monster_attack(monster)
                return
        print(f"There is no {monster_name} here.")

    def monster_attack(self, monster: Monster):
        damage = max(1, monster.attack - self.player.defense)
        self.player.health -= damage
        print(f"The {monster.name} attacks you for {damage} damage!")
        if self.player.health <= 0:
            print("You have been defeated!")
            self.game_over = True

    def flee(self):
        if len(self.current_room.exits) > 0:
            direction = list(self.current_room.exits.keys())[0]
            next_room_id = self.current_room.exits[direction]
            self.current_room = self.world["rooms"][next_room_id]
            self.player.location = next_room_id
            print(f"You fled to the {self.current_room.title}.")
        else:
            print("You can't flee from here.")

    def check_monsters(self):
        if self.current_room.monsters and not any(
            m in self.completed_monsters for m in self.current_room.monsters
        ):
            print("You encounter some monsters!")
            for monster_id in self.current_room.monsters:
                if monster_id not in self.completed_monsters:
                    monster = self.world["monsters"][monster_id]
                    print(f"A {monster.name} appears!")

    def save_game(self, filename: str = "save.json"):
        save_data = {
            "player": {
                "health": self.player.health,
                "attack": self.player.attack,
                "defense": self.player.defense,
                "inventory": self.player.inventory,
                "equipment": self.player.equipment,
                "location": self.player.location,
            },
            "completed": {"monsters": list(self.completed_monsters)},
        }
        with open(filename, "w") as f:
            json.dump(save_data, f)

    def load_game(self, filename: str = "save.json"):
        with open(filename, "r") as f:
            save_data = json.load(f)
        self.player.health = save_data["player"]["health"]
        self.player.attack = save_data["player"]["attack"]
        self.player.defense = save_data["player"]["defense"]
        self.player.inventory = save_data["player"]["inventory"]
        self.player.equipment = save_data["player"]["equipment"]
        self.player.location = save_data["player"]["location"]
        self.completed_monsters = set(save_data["completed"]["monsters"])
        self.current_room = self.world["rooms"][self.player.location]


def equip_item(self, item_name: str) -> None:
    """Equip an item from the player's inventory."""
    for item in self.player.inventory:
        if item.name == item_name:
            if hasattr(item, "type") and item.type == "equippable":
                # Check if equipment slots are available
                if (
                    not hasattr(self.player, "equipment")
                    or self.player.equipment is None
                ):
                    self.player.equipment = {}

                # Equip weapon or armor based on item type
                if hasattr(item, "type") and item.type == "weapon":
                    self.player.equipment["weapon"] = item.name
                    print(f"You equipped the {item.name} as your weapon.")
                elif hasattr(item, "type") and item.type == "armor":
                    self.player.equipment["armor"] = item.name
                    print(f"You equipped the {item.name} as your armor.")
                else:
                    print(f"The {item.name} cannot be equipped as a weapon or armor.")
                return
            else:
                print(f"The {item.name} cannot be equipped.")
                return
    print(f"You don't have a {item_name} in your inventory.")
