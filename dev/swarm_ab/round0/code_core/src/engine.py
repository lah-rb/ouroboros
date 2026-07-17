"""
Main game engine: orchestrates world loading, command handling,
state updates, combat, and persistence.
"""

from typing import Dict

from .parser import parse_command, Command
from .world_loader import load_world
from .combat import CombatEngine, CombatResult
from .save_load import save_game, load_game
from .models import (
    GameState,
    Player,
    Room,
    Item,
    NPC,
    Monster,
)


class GameEngine:
    """
    Runs the interactive loop. All mutable state lives inside ``self.state``.
    """

    def __init__(self, yaml_path: str = "src/world.yaml"):
        # Load static world data
        world = load_world(yaml_path)
        self.rooms_def: Dict[str, Room] = world["rooms"]
        self.items_def: Dict[str, Item] = world["items"]
        self.npcs_def: Dict[str, NPC] = world["npcs"]
        self.monsters_def: Dict[str, Monster] = world["monsters"]

        # Initialise dynamic state
        start_room_id = next(iter(self.rooms_def))  # first room in dict
        player = Player(
            health=30,
            max_health=30,
            attack=2,
            defense=1,
            inventory=[],
            equipped_weapon=None,
            equipped_armor=None,
        )
        rooms_state = {}
        for rid, room in self.rooms_def.items():
            rooms_state[rid] = {
                "items": list(room.items),
                "monsters": list(room.monsters),
                "npcs": list(room.npcs),
            }

        self.state = GameState(
            current_room_id=start_room_id,
            player=player,
            rooms=rooms_state,
            npc_dialogue_progress={},
            defeated_monsters=[],
            turn_counter=0,
        )

        self.combat_engine = CombatEngine(self.items_def)

    # ------------------------------------------------------------------ #
    # Helper methods
    # ------------------------------------------------------------------ #

    def current_room(self) -> Room:
        return self.rooms_def[self.state.current_room_id]

    def describe_current_room(self) -> str:
        room = self.current_room()
        lines = [f"\n{room.name}", room.description]
        # Exits
        if room.connections:
            exits = ", ".join(room.connections.keys())
            lines.append(f"Exits: {exits}")
        # Items
        items_here = self.state.rooms[room.id]["items"]
        if items_here:
            item_names = [self.items_def[i].name for i in items_here]
            lines.append("You see: " + ", ".join(item_names))
        # NPCs
        npcs_here = self.state.rooms[room.id]["npcs"]
        if npcs_here:
            npc_names = [self.npcs_def[n].name for n in npcs_here]
            lines.append("People here: " + ", ".join(npc_names))
        # Monsters
        monsters_here = self.state.rooms[room.id]["monsters"]
        if monsters_here:
            monster_names = [self.monsters_def[m].name for m in monsters_here]
            lines.append("Danger! " + ", ".join(monster_names) + " lurk here.")
        return "\n".join(lines)

    def move_player(self, direction: str) -> str:
        room = self.current_room()
        if direction not in room.connections:
            return "You can't go that way."
        new_room_id = room.connections[direction]
        self.state.current_room_id = new_room_id
        self.state.turn_counter += 1
        return self.describe_current_room()

    def take_item(self, item_name: str) -> str:
        room_state = self.state.rooms[self.state.current_room_id]
        # Find matching item id by name (case‑insensitive)
        for iid in room_state["items"]:
            if self.items_def[iid].name.lower() == item_name.lower():
                self.state.player.inventory.append(iid)
                room_state["items"].remove(iid)
                self.state.turn_counter += 1
                return f"You pick up the {self.items_def[iid].name}."
        return "There is no such item here."

    def drop_item(self, item_name: str) -> str:
        inv = self.state.player.inventory
        for iid in inv:
            if self.items_def[iid].name.lower() == item_name.lower():
                inv.remove(iid)
                self.state.rooms[self.state.current_room_id]["items"].append(iid)
                self.state.turn_counter += 1
                return f"You drop the {self.items_def[iid].name}."
        return "You don't have that item."

    def equip_item(self, item_name: str) -> str:
        inv = self.state.player.inventory
        for iid in inv:
            if self.items_def[iid].name.lower() == item_name.lower():
                item = self.items_def[iid]
                if item.type == "weapon":
                    self.state.player.equipped_weapon = iid
                    return f"You wield the {item.name}."
                elif item.type == "armor":
                    self.state.player.equipped_armor = iid
                    return f"You don the {item.name}."
                else:
                    return "That item cannot be equipped."
        return "You don't have that item."

    def use_item(self, item_name: str) -> str:
        inv = self.state.player.inventory
        for iid in inv:
            if self.items_def[iid].name.lower() == item_name.lower():
                item = self.items_def[iid]
                if item.type == "healing":
                    heal_amount = item.healing
                    player = self.state.player
                    new_hp = min(player.max_health, player.health + heal_amount)
                    player.health = new_hp
                    inv.remove(iid)  # consumable
                    self.state.turn_counter += 1
                    return f"You drink the {item.name} and recover {heal_amount} HP."
                else:
                    return "You can't use that now."
        return "You don't have that item."

    def examine_item(self, item_name: str) -> str:
        # Look in inventory first, then room
        for iid in (
            self.state.player.inventory
            + self.state.rooms[self.state.current_room_id]["items"]
        ):
            if self.items_def[iid].name.lower() == item_name.lower():
                item = self.items_def[iid]
                lines = [f"{item.name}: {item.description}"]
                if item.type == "weapon":
                    lines.append(f"Attack bonus: {item.attack}")
                if item.type == "armor":
                    lines.append(f"Defense bonus: {item.defense}")
                if item.type == "healing":
                    lines.append(f"Heals: {item.healing} HP")
                return "\n".join(lines)
        return "You see no such item."

    def talk_to(self, npc_name: str) -> str:
        room_npcs = self.state.rooms[self.state.current_room_id]["npcs"]
        for nid in room_npcs:
            if self.npcs_def[nid].name.lower() == npc_name.lower():
                progress = self.state.npc_dialogue_progress.get(nid, 0)
                npc = self.npcs_def[nid]
                if progress >= len(npc.dialogues):
                    return "They have nothing more to say."
                dialogue = npc.dialogues[progress]
                # Advance dialogue index
                next_index = (
                    dialogue.next if dialogue.next is not None else progress + 1
                )
                self.state.npc_dialogue_progress[nid] = next_index
                self.state.turn_counter += 1
                return f'{npc.name} says: "{dialogue.text}"'
        return "No one by that name here."

    def attack_monster(self, monster_name: str) -> str:
        room_state = self.state.rooms[self.state.current_room_id]
        for mid in room_state["monsters"]:
            if self.monsters_def[mid].name.lower() == monster_name.lower():
                monster = self.monsters_def[mid]
                # Clone mutable monster stats for this combat instance
                combat_monster = Monster(
                    id=monster.id,
                    name=monster.name,
                    description=monster.description,
                    health=monster.health,
                    max_health=monster.max_health,
                    attack=monster.attack,
                    defense=monster.defense,
                    behavior=monster.behavior,
                )
                result: CombatResult = self.combat_engine.start_combat(
                    self.state.player, combat_monster
                )
                # Apply post‑combat changes
                if result.victory:
                    room_state["monsters"].remove(mid)
                    self.state.defeated_monsters.append(mid)
                else:
                    # Player died – handled by caller
                    pass
                self.state.turn_counter += 1
                return result.message
        return "No such creature here."

    def flee(self) -> str:
        # Simple implementation: just end combat, no penalty.
        self.state.turn_counter += 1
        return "You retreat to safety."

    def show_status(self) -> str:
        p = self.state.player
        lines = [
            f"HP: {p.health}/{p.max_health}",
            f"Attack: {p.effective_attack(self.items_def)}",
            f"Defense: {p.effective_defense(self.items_def)}",
            "Inventory: "
            + (", ".join([self.items_def[i].name for i in p.inventory]) or "empty"),
        ]
        if p.equipped_weapon:
            lines.append(f"Weapon: {self.items_def[p.equipped_weapon].name}")
        if p.equipped_armor:
            lines.append(f"Armor: {self.items_def[p.equipped_armor].name}")
        return "\n".join(lines)

    def help_text(self) -> str:
        return (
            "Commands:\n"
            "  go <direction>          – move north/south/east/west\n"
            "  look                    – redisplay the room description\n"
            "  take <item>             – pick up an item\n"
            "  drop <item>             – leave an item behind\n"
            "  equip <item>            – wield a weapon or wear armor\n"
            "  use <item>              – consume a healing item\n"
            "  examine <item>          – read an item's description\n"
            "  talk to <npc>           – converse with a character\n"
            "  attack <monster>        – fight a creature\n"
            "  flee                    – escape from combat\n"
            "  status                  – view health and equipment\n"
            "  save <filename>         – write game state to file\n"
            "  load <filename>         – restore game state from file\n"
            "  help                    – show this help\n"
            "  quit                    – exit the game"
        )

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        print("Welcome to the Adventure!\n")
        print(self.describe_current_room())
        while True:
            raw = input("\n> ")
            cmd: Command = parse_command(raw)

            if cmd.name == "empty":
                continue
            elif cmd.name in ("quit", "exit"):
                print("Thanks for playing!")
                break
            elif cmd.name == "help":
                print(self.help_text())
            elif cmd.name == "look":
                print(self.describe_current_room())
            elif cmd.name == "go" and cmd.args:
                print(self.move_player(cmd.args[0]))
            elif cmd.name == "take" and cmd.args:
                print(self.take_item(" ".join(cmd.args)))
            elif cmd.name == "drop" and cmd.args:
                print(self.drop_item(" ".join(cmd.args)))
            elif cmd.name == "equip" and cmd.args:
                print(self.equip_item(" ".join(cmd.args)))
            elif cmd.name == "use" and cmd.args:
                print(self.use_item(" ".join(cmd.args)))
            elif cmd.name == "examine" and cmd.args:
                print(self.examine_item(" ".join(cmd.args)))
            elif cmd.name == "talk" and cmd.args and cmd.args[0] == "to":
                print(self.talk_to(" ".join(cmd.args[1:])))
            elif cmd.name == "attack" and cmd.args:
                result_msg = self.attack_monster(" ".join(cmd.args))
                print(result_msg)
                if self.state.player.health <= 0:
                    print("\nYou have perished. Game over.")
                    break
                # Check win condition: final boss defeated
                if "final_boss" in self.state.defeated_monsters:
                    print(
                        "\nCongratulations! You have vanquished the Dark Lord and restored peace."
                    )
                    break
            elif cmd.name == "flee":
                print(self.flee())
            elif cmd.name == "status":
                print(self.show_status())
            elif cmd.name == "save" and cmd.args:
                try:
                    save_game(self.state, cmd.args[0])
                    print(f"Game saved to {cmd.args[0]}")
                except Exception as e:
                    print(f"Failed to save: {e}")
            elif cmd.name == "load" and cmd.args:
                try:
                    self.state = load_game(cmd.args[0])
                    print(f"Game loaded from {cmd.args[0]}")
                    print(self.describe_current_room())
                except Exception as e:
                    print(f"Failed to load: {e}")
            else:
                print("I don't understand that command. Type 'help' for a list.")
