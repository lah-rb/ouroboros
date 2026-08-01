"""The game engine: ties world content, player state, the parser, and
combat together into a playable command loop.

GameEngine owns all *runtime* state:
  - self.world     -- content loaded fresh from YAML (reloaded on every
                       new game so a restart never inherits a previous
                       run's looted rooms or dead monsters)
  - self.player     -- the Player (location, stats, inventory, equipment,
                        defeated monsters, met NPCs)
  - self.monsters    -- monster_id -> live Monster instance, persists
                         health across encounters within one run
  - self.combat       -- the current Combat, or None outside a fight

Saving/loading serializes exactly the pieces that can change during play:
the player, every monster's current state, and every room's remaining
item list.
"""

import json
import random
from pathlib import Path

from . import ui
from .combat import Combat, FLEE_SUCCESS_CHANCE
from .monsters import create_monster
from .parser import parse
from .player import Player
from .world import World

SAVE_PATH = Path(__file__).resolve().parent.parent / "savegame.json"

# Verbs that still make sense while a fight is underway. Everything else
# is refused with a reminder while self.combat is set.
COMBAT_ALLOWED_VERBS = {
    "attack", "flee", "use", "equip", "unequip",
    "examine", "look", "inventory", "status", "help", "quit",
}

HELP_TEXT = """
What you can type:

  Movement
    go north / south / east / west / up / down   (or just: north, n, s, e, w, u, d)

  Items
    take <item>              pick up an item from the room
    drop <item>                leave an item behind
    inventory  (i)               list what you're carrying
    examine <item/monster/self>   take a closer look at something
    equip <item>                  wield a weapon, wear armor, or wear a trinket
    unequip <item/slot>            take off equipped gear
    use <item>                      drink a potion, eat food, etc. (works mid-fight)

  People
    talk to <name>              greet someone; they'll list what you can ask about
    ask <name> about <topic>      dig into a specific topic

  Combat
    attack                          strike whatever you're fighting
    flee                             attempt to escape the fight

  Other
    look                            describe your surroundings again
    status  (st)                     see your health, gear, and location
    map                               list the exits from where you're standing
    save / load                        save or load your progress
    help                                show this list again
    quit                                 exit the game
""".rstrip()


class GameEngine:
    def __init__(self):
        self.world = World.load()
        self.player = None
        self.monsters = {}
        self.combat = None
        self.running = True
        self.game_over = False
        self.victory = False

        self.dispatch = {
            "go": self.cmd_go,
            "take": self.cmd_take,
            "drop": self.cmd_drop,
            "use": self.cmd_use,
            "examine": self.cmd_examine,
            "look": self.cmd_look,
            "equip": self.cmd_equip,
            "unequip": self.cmd_unequip,
            "talk": self.cmd_talk,
            "ask": self.cmd_ask,
            "attack": self.cmd_attack,
            "flee": self.cmd_flee,
            "inventory": self.cmd_inventory,
            "status": self.cmd_status,
            "help": self.cmd_help,
            "quit": self.cmd_quit,
            "save": self.cmd_save,
            "load": self.cmd_load,
            "map": self.cmd_map,
        }

    # -- setup / top-level loop --------------------------------------------

    def new_game(self):
        # Reload the world fresh so a restart never carries over looted
        # rooms, dead monsters, or a moved player from a previous run.
        self.world = World.load()
        self.player = Player()
        self.player.world = self.world
        self.monsters = {
            mid: create_monster(mid, tmpl)
            for mid, tmpl in self.world.monster_templates.items()
        }
        self.combat = None
        self.game_over = False
        self.victory = False

    def start(self):
        choice = ui.title_screen()
        if choice == "load":
            if not self.load_game():
                print("No save file found -- starting a new game instead.")
                self.new_game()
        else:
            self.new_game()
        self.main_loop()

    def main_loop(self):
        print(ui.divider())
        self.look()
        while self.running:
            try:
                raw = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\nFarewell, wanderer.")
                break

            cmd = parse(raw)
            self.handle_command(cmd)

            if not self.running:
                break
            if self.game_over:
                if not self.post_game_menu():
                    break

    def post_game_menu(self):
        while True:
            choice = input("\nType 'restart' to play again, or 'quit' to exit: ").strip().lower()
            if choice in ("restart", "r", "new", "new game"):
                self.new_game()
                print(ui.divider())
                self.look()
                return True
            if choice in ("quit", "q", "exit"):
                print("\nThanks for playing The Sunstone Wyrm!")
                return False
            print("Please type 'restart' or 'quit'.")

    def handle_command(self, cmd):
        verb = cmd.verb
        if not verb:
            return
        if self.combat and verb not in COMBAT_ALLOWED_VERBS:
            print(f"You're locked in combat with {self.combat.monster.name}! "
                  f"(try: attack, flee, use <item>, equip <item>)")
            return
        handler = self.dispatch.get(verb)
        if not handler:
            print(f"I don't understand '{cmd.raw}'. Type 'help' for a list of commands.")
            return
        handler(cmd.args_text)

    # -- item / npc name resolution ------------------------------------------

    def resolve_item(self, fragment, candidate_ids):
        fragment = (fragment or "").strip().lower()
        if not fragment:
            return None
        candidates = list(dict.fromkeys(cid for cid in candidate_ids if cid))
        for item_id in candidates:
            if item_id == fragment or item_id.replace("_", " ") == fragment:
                return item_id
        for item_id in candidates:
            item = self.world.get_item(item_id)
            if item and fragment in item.name.lower():
                return item_id
        for item_id in candidates:
            if fragment in item_id.replace("_", " "):
                return item_id
        return None

    def npc_name_matches(self, fragment, npc):
        if not fragment:
            return True
        fragment = fragment.strip().lower()
        return fragment in npc.name.lower() or fragment in npc.id.lower()

    # -- looking around -------------------------------------------------------

    def describe_room(self):
        room = self.world.get_room(self.player.current_room)
        lines = [ui.header(room.name), room.description]

        if room.items:
            names = [self.world.get_item(i).name for i in room.items]
            lines.append("You see here: " + ", ".join(names) + ".")

        if room.npc:
            npc = self.world.get_npc(room.npc)
            lines.append(f"{npc.name} is here.")

        if room.monster:
            if room.monster in self.player.defeated_monsters:
                monster = self.monsters[room.monster]
                lines.append(f"The defeated {monster.name} lies still nearby.")
            else:
                monster = self.monsters[room.monster]
                lines.append(f"{monster.name} is here! {monster.description}")

        exits = ", ".join(sorted(room.exits.keys())) if room.exits else "none"
        lines.append(f"Exits: {exits}")
        return "\n".join(lines)

    def look(self):
        print(self.describe_room())

    def cmd_look(self, args_text=""):
        self.look()

    def cmd_map(self, args_text=""):
        room = self.world.get_room(self.player.current_room)
        if not room.exits:
            print("There are no obvious paths from here.")
            return
        bits = [f"{d} (to {self.world.get_room(r).name})" for d, r in sorted(room.exits.items())]
        print("From here you can go: " + ", ".join(bits))

    # -- movement -------------------------------------------------------------

    def cmd_go(self, args_text=""):
        direction = (args_text or "").strip()
        if not direction:
            print("Go where? Try a direction like north, south, east, or west.")
            return
        room = self.world.get_room(self.player.current_room)
        dest = room.exits.get(direction)
        if not dest:
            print(f"You can't go {direction} from here.")
            return
        self.player.current_room = dest
        self.look()

    # -- items ------------------------------------------------------------------

    def cmd_take(self, args_text=""):
        name = (args_text or "").strip()
        if not name:
            print("Take what?")
            return
        room = self.world.get_room(self.player.current_room)
        item_id = self.resolve_item(name, room.items)
        if not item_id:
            print(f"There's no '{name}' here to take.")
            return
        room.items.remove(item_id)
        self.player.add_item(item_id)
        print(f"You take the {self.world.get_item(item_id).name}.")

    def cmd_drop(self, args_text=""):
        name = (args_text or "").strip()
        if not name:
            print("Drop what?")
            return
        item_id = self.resolve_item(name, list(self.player.inventory.keys()))
        if not item_id:
            print(f"You aren't carrying a '{name}'.")
            return
        self.player.remove_item(item_id, 1)
        room = self.world.get_room(self.player.current_room)
        room.items.append(item_id)
        print(f"You drop the {self.world.get_item(item_id).name}.")

    def cmd_inventory(self, args_text=""):
        if not self.player.inventory:
            print("Your pack is empty.")
            return
        print("You are carrying:")
        for item_id, count in self.player.inventory.items():
            item = self.world.get_item(item_id)
            qty = f" x{count}" if count > 1 else ""
            print(f"  - {item.name}{qty}")

    def cmd_use(self, args_text=""):
        name = (args_text or "").strip()
        if not name:
            print("Use what? Try 'use' followed by something in your inventory.")
            return
        item_id = self.resolve_item(name, list(self.player.inventory.keys()))
        if not item_id:
            print(f"You aren't carrying anything like '{name}'.")
            return
        item = self.world.get_item(item_id)
        if item.type != "consumable":
            print(f"The {item.name} isn't something you use like that. Maybe 'equip' it instead?")
            return
        self.player.remove_item(item_id, 1)
        healed = self.player.heal(item.heal_amount)
        print(f"You use the {item.name} and recover {healed} health. "
              f"({self.player.health}/{self.player.max_health} HP)")
        if self.combat:
            self.run_monster_turn()

    def cmd_equip(self, args_text=""):
        name = (args_text or "").strip()
        if not name:
            print("Equip what?")
            return
        item_id = self.resolve_item(name, list(self.player.inventory.keys()))
        if not item_id:
            print(f"You aren't carrying a '{name}'.")
            return
        item = self.world.get_item(item_id)
        if item.slot not in ("weapon", "armor", "trinket"):
            print(f"The {item.name} isn't something you can equip. Try 'use' instead?")
            return

        self.player.remove_item(item_id, 1)
        previous = self.player.equipment.get(item.slot)
        if previous:
            self.player.add_item(previous, 1)
        self.player.equipment[item.slot] = item_id

        bonuses = []
        if item.attack_bonus:
            bonuses.append(f"+{item.attack_bonus} attack")
        if item.defense_bonus:
            bonuses.append(f"+{item.defense_bonus} defense")
        bonus_text = f" ({', '.join(bonuses)})" if bonuses else ""
        print(f"You equip the {item.name}.{bonus_text}")
        if item.special == "ashwyrm_weakness":
            print("The stone feels warm against your skin, humming faintly, as if it "
                  "recognizes an old enemy.")
        if previous:
            print(f"You put away the {self.world.get_item(previous).name}.")

    def cmd_unequip(self, args_text=""):
        name = (args_text or "").strip().lower()
        if not name:
            print("Unequip what? (weapon, armor, or trinket)")
            return
        slot = name if name in self.player.equipment else None
        if slot is None:
            equipped_ids = [v for v in self.player.equipment.values() if v]
            item_id = self.resolve_item(name, equipped_ids)
            if item_id:
                for s, v in self.player.equipment.items():
                    if v == item_id:
                        slot = s
                        break
        if not slot or not self.player.equipment.get(slot):
            print("You don't have anything like that equipped.")
            return
        item_id = self.player.equipment[slot]
        self.player.equipment[slot] = None
        self.player.add_item(item_id, 1)
        print(f"You unequip the {self.world.get_item(item_id).name}.")

    def cmd_examine(self, args_text=""):
        name = (args_text or "").strip()
        if not name:
            self.look()
            return
        if name in ("self", "me", "myself"):
            self.cmd_status()
            return

        room = self.world.get_room(self.player.current_room)
        equipped_ids = [v for v in self.player.equipment.values() if v]
        pool = list(self.player.inventory.keys()) + room.items + equipped_ids
        item_id = self.resolve_item(name, pool)
        if item_id:
            item = self.world.get_item(item_id)
            print(f"{item.name}: {item.description}")
            return

        if room.npc:
            npc = self.world.get_npc(room.npc)
            if self.npc_name_matches(name, npc):
                print(f"{npc.name}: {npc.description}")
                return

        if room.monster and room.monster not in self.player.defeated_monsters:
            monster = self.monsters[room.monster]
            if name in monster.name.lower() or name in monster.id.lower():
                print(f"{monster.name}: {monster.description}")
                return

        print(f"You don't see a '{name}' here.")

    # -- conversation ---------------------------------------------------------

    def cmd_talk(self, args_text=""):
        name = (args_text or "").strip()
        room = self.world.get_room(self.player.current_room)
        if not room.npc:
            print("There's no one here to talk to.")
            return
        npc = self.world.get_npc(room.npc)
        if not self.npc_name_matches(name, npc):
            print(f"There's no one called '{name}' here.")
            return

        first_time = npc.id not in self.player.met_npcs
        self.player.met_npcs.add(npc.id)
        print(ui.header(npc.name))
        print(npc.greeting if first_time else npc.repeat_greeting)
        topics = ", ".join(sorted(npc.topics.keys()))
        print(f"(You can ask {npc.name} about: {topics})")

    def cmd_ask(self, args_text=""):
        text = (args_text or "").strip()
        if " about " not in text:
            print("Try: ask <name> about <topic>")
            return
        who, topic = text.split(" about ", 1)
        who, topic = who.strip(), topic.strip()

        room = self.world.get_room(self.player.current_room)
        if not room.npc:
            print("There's no one here to ask.")
            return
        npc = self.world.get_npc(room.npc)
        if not self.npc_name_matches(who, npc):
            print(f"There's no one called '{who}' here.")
            return

        self.player.met_npcs.add(npc.id)
        response = None
        if topic:
            for key, text_ in npc.topics.items():
                if topic == key or topic in key or key in topic:
                    response = text_
                    break
        print(f'"{response if response else npc.default_response}"')

    # -- combat -----------------------------------------------------------------

    def cmd_attack(self, args_text=""):
        if not self.combat:
            room = self.world.get_room(self.player.current_room)
            monster_id = room.monster
            if not monster_id or monster_id in self.player.defeated_monsters:
                print("There's nothing here to fight.")
                return
            monster = self.monsters[monster_id]
            if not monster.is_alive:
                self.player.defeated_monsters.add(monster_id)
                print("There's nothing here to fight.")
                return
            self.combat = Combat(self.player, monster)
            print(ui.divider())
            print(f"You engage {monster.name} in combat!")
            print(monster.description)
        self.resolve_player_attack()

    def resolve_player_attack(self):
        combat = self.combat
        monster = combat.monster
        for line in combat.player_attack():
            print(line)
        if monster.health <= 0:
            self.end_combat_victory()
            return
        self.run_monster_turn()

    def run_monster_turn(self):
        if not self.combat:
            return
        combat = self.combat
        monster_name = combat.monster.name
        lines, fled = combat.monster_turn()
        for line in lines:
            print(line)
        if fled:
            print(f"{monster_name} has fled the fight! It may still be found here, "
                  f"wounded, if you go looking for it again.")
            self.combat = None
            return
        if not self.player.is_alive:
            self.end_combat_defeat()
            return
        print(f"(Your HP: {self.player.health}/{self.player.max_health})")

    def cmd_flee(self, args_text=""):
        if not self.combat:
            print("There's nothing to flee from.")
            return
        monster = self.combat.monster
        if random.random() < FLEE_SUCCESS_CHANCE:
            print(f"You break away from {monster.name} and beat a hasty retreat!")
            self.combat = None
        else:
            print(f"You try to flee, but {monster.name} cuts off your escape!")
            self.run_monster_turn()

    def end_combat_victory(self):
        monster = self.combat.monster
        room = self.world.get_room(self.player.current_room)
        print(ui.divider())
        print(f"You have defeated {monster.name}!")
        if room.monster:
            self.player.defeated_monsters.add(room.monster)
        is_boss = room.monster == "ashwyrm"
        self.combat = None
        if is_boss:
            self.victory = True
            self.game_over = True
            print(ui.victory_screen())
        else:
            print("The way forward is clear.")

    def end_combat_defeat(self):
        monster_name = self.combat.monster.name
        print(ui.divider())
        print(f"{monster_name} has bested you...")
        self.combat = None
        self.game_over = True
        self.victory = False
        print(ui.defeat_screen())

    # -- status / help / quit ----------------------------------------------------

    def cmd_status(self, args_text=""):
        room = self.world.get_room(self.player.current_room)
        print(ui.header("Status"))
        print(f"Location: {room.name}")
        print(f"Health: {self.player.health}/{self.player.max_health}")
        print(f"Attack: {self.player.total_attack()} (base {self.player.base_attack})")
        print(f"Defense: {self.player.total_defense()} (base {self.player.base_defense})")
        weapon = self.player.equipment.get("weapon")
        armor = self.player.equipment.get("armor")
        trinket = self.player.equipment.get("trinket")
        print(f"Weapon: {self.world.get_item(weapon).name if weapon else 'none (bare fists)'}")
        print(f"Armor: {self.world.get_item(armor).name if armor else 'none'}")
        print(f"Trinket: {self.world.get_item(trinket).name if trinket else 'none'}")
        print(f"Monsters defeated: {len(self.player.defeated_monsters)}")
        if self.combat:
            m = self.combat.monster
            print(f"In combat with: {m.name} ({m.health}/{m.max_health} HP)")

    def cmd_help(self, args_text=""):
        print(HELP_TEXT)

    def cmd_quit(self, args_text=""):
        print("Farewell, wanderer.")
        self.running = False

    # -- save / load ----------------------------------------------------------------

    def cmd_save(self, args_text=""):
        data = {
            "player": self.player.to_dict(),
            "monsters": {mid: m.to_dict() for mid, m in self.monsters.items()},
            "room_items": {rid: list(r.items) for rid, r in self.world.rooms.items()},
        }
        with open(SAVE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Game saved to {SAVE_PATH.name}.")

    def load_game(self):
        if not SAVE_PATH.exists():
            return False
        with open(SAVE_PATH, encoding="utf-8") as f:
            data = json.load(f)

        self.world = World.load()
        self.player = Player.from_dict(data["player"])
        self.player.world = self.world

        self.monsters = {}
        for mid, tmpl in self.world.monster_templates.items():
            monster = create_monster(mid, tmpl)
            state = data.get("monsters", {}).get(mid)
            if state:
                monster.load_state(state)
            self.monsters[mid] = monster

        for rid, items in data.get("room_items", {}).items():
            if rid in self.world.rooms:
                self.world.rooms[rid].items = list(items)

        self.combat = None
        self.game_over = False
        self.victory = False
        print("Game loaded.")
        return True

    def cmd_load(self, args_text=""):
        if not self.load_game():
            print("No save file found.")
            return
        self.look()
