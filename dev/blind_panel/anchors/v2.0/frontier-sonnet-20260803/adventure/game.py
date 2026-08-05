"""The Game class: orchestrates The Ashen Keep from title screen to
victory or defeat.

Game state lives in two places: self.player (stats, inventory,
location, progress flags) and self.rooms (the world graph, including
each room's remaining items and its monster's current HP/phase). Both
are captured by to_dict()/load_from_data() for save/load.

The game is always in exactly one of four modes:
  "explore"    -- free movement and exploration
  "combat"     -- locked in a fight, only combat-relevant verbs work
  "over_win"   -- the Ashen King has fallen
  "over_lose"  -- the player has died
"""

import json
import os
import random

from .world import build_world, START_ROOM
from .player import Player
from .items import ITEMS
from .parser import parse, HELP_TEXT

SAVE_FILE = "savegame.json"
WIDTH = 70


class Game:
    def __init__(self):
        self.reset()

    def reset(self):
        self.rooms = build_world()
        self.player = Player()
        self.player.location = START_ROOM
        self.player.previous_location = START_ROOM
        self.mode = "explore"
        self.current_monster = None
        self.running = True

    # ------------------------------------------------------------
    # top-level flow
    # ------------------------------------------------------------

    def run(self):
        self.show_title_screen()
        if not self.running:
            return
        self.enter_room(self.player.location)
        while self.running:
            if self.mode == "over_win":
                self.show_victory_screen()
                break
            if self.mode == "over_lose":
                if self.show_defeat_screen():
                    self.reset()
                    self.enter_room(self.player.location)
                    continue
                break
            try:
                raw = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\n\nFarewell, wanderer.")
                break
            verb, rest = parse(raw)
            if not verb:
                continue
            self.dispatch(verb, rest)

    def show_title_screen(self):
        banner = r"""
==============================================================
   T H E   A S H E N   K E E P
   -- a text adventure --
==============================================================
"""
        while True:
            print(banner)
            print("A tyrant of ash and cinder rules these ruined halls.")
            print("Two ghosts of the keep remember how he can be undone --")
            print("if you're willing to listen.\n")
            print("  [N] New Game")
            print("  [L] Load Game")
            print("  [Q] Quit")
            choice = input("\nChoose an option: ").strip().lower()
            if choice in ("n", "new", "new game"):
                return
            elif choice in ("l", "load", "load game"):
                if self.load_game():
                    print("\nGame loaded. Picking up where you left off...")
                    return
                print("\nNo valid save file found to load.\n")
            elif choice in ("q", "quit", "exit"):
                print("\nFarewell.")
                self.running = False
                return
            else:
                print("\nI didn't understand that. Try N, L, or Q.\n")

    # ------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------

    def current_room(self):
        return self.rooms[self.player.location]

    def dispatch(self, verb, rest):
        if verb == "quit":
            self.cmd_quit()
            return
        if verb == "help":
            self.cmd_help()
            return
        if verb == "status":
            self.cmd_status()
            return
        if verb == "look":
            self.cmd_look()
            return
        if verb == "inventory":
            self.cmd_inventory()
            return
        if verb == "save":
            self.save_game()
            return
        if verb == "load":
            if self.mode == "combat":
                print("\nYou can't load a game mid-fight. Flee first.")
                return
            if self.load_game():
                print("\nGame loaded.")
                self.describe_room(self.current_room())
            else:
                print("\nNo valid save file found to load.")
            return

        if self.mode == "combat":
            self.dispatch_combat(verb, rest)
        else:
            self.dispatch_explore(verb, rest)

    def dispatch_explore(self, verb, rest):
        if verb == "go":
            self.cmd_go(rest)
        elif verb == "take":
            self.cmd_take(rest)
        elif verb == "drop":
            self.cmd_drop(rest)
        elif verb == "use":
            self.cmd_use(rest)
        elif verb == "equip":
            self.cmd_equip(rest)
        elif verb == "examine":
            self.cmd_examine(rest)
        elif verb == "talk":
            self.cmd_talk(rest)
        elif verb == "attack":
            print("\nThere's nothing here to fight.")
        elif verb == "flee":
            print("\nThere's nothing to flee from right now.")
        else:
            print(
                f"\nI don't know how to '{verb}'. Type 'help' for a list of commands."
            )

    def dispatch_combat(self, verb, rest):
        monster = self.current_monster
        if verb == "attack":
            self.combat_player_attack()
        elif verb == "flee":
            self.combat_player_flee()
        elif verb == "use":
            self.cmd_use(rest, in_combat=True)
        elif verb == "equip":
            self.cmd_equip(rest, in_combat=True)
        elif verb == "examine":
            self.cmd_examine(rest)
        elif verb == "go":
            name = monster.name if monster else "something"
            print(
                f"\nYou can't just walk away -- the {name} won't let you. (Try 'flee'.)"
            )
        elif verb in ("take", "drop", "talk"):
            print("\nNot while you're fighting for your life! (attack or flee)")
        else:
            print(
                f"\nIn a fight you can: attack, flee, use <item>, equip <item>, "
                f"examine <item>, or status. ('{verb}' won't help you now.)"
            )

    # ------------------------------------------------------------
    # exploration commands
    # ------------------------------------------------------------

    def cmd_go(self, direction):
        if direction not in ("north", "south", "east", "west"):
            print("\nGo where? Try north, south, east, or west.")
            return
        room = self.current_room()
        dest_key = room.exits.get(direction)
        if not dest_key:
            print(f"\nYou can't go {direction} from here.")
            return
        self.player.previous_location = self.player.location
        self.player.location = dest_key
        self.enter_room(dest_key)

    def enter_room(self, key):
        room = self.rooms[key]
        self.describe_room(room)
        room.visited = True
        if room.has_living_monster():
            self.start_combat(room)

    def describe_room(self, room):
        print()
        print("-" * WIDTH)
        print(room.name.upper())
        print("-" * WIDTH)
        print(room.description)
        if room.items:
            names = ", ".join(ITEMS[k].name for k in room.items)
            print(f"\nYou see here: {names}.")
        if room.npc is not None:
            first_word = room.npc.name.split()[0].lower()
            print(f"\n{room.npc.name} is here. (try: talk to {first_word})")
        if room.monster is not None and room.monster.is_alive():
            print(f"\nA {room.monster.name} blocks your way!")
        print(f"\nExits: {room.exit_list_text()}")

    def cmd_look(self):
        self.describe_room(self.current_room())

    def cmd_take(self, rest):
        if not rest:
            print("\nTake what?")
            return
        room = self.current_room()
        key = self._resolve_item(rest, room.items)
        if not key:
            print(f"\nThere's no '{rest}' here to take.")
            return
        room.items.remove(key)
        self.player.inventory.append(key)
        print(f"\nYou take the {ITEMS[key].name}.")

    def cmd_drop(self, rest):
        if not rest:
            print("\nDrop what?")
            return
        key = self._resolve_item(rest, self.player.inventory)
        if not key:
            print(f"\nYou aren't carrying a '{rest}'.")
            return
        if key == self.player.weapon:
            self.player.weapon = None
            print("You lower your weapon and let it fall -- you're unarmed now.")
        if key == self.player.armor:
            self.player.armor = None
            print("You shrug off your armor as you drop it.")
        self.player.inventory.remove(key)
        self.current_room().items.append(key)
        print(f"\nYou drop the {ITEMS[key].name}.")

    def cmd_talk(self, rest):
        room = self.current_room()
        if not room.npc:
            print("\nThere's no one here to talk to.")
            return
        if rest and not self._npc_matches(rest, room.npc):
            print(f"\nThere's no '{rest}' here to talk to.")
            return
        print()
        for line in room.npc.talk(self.player):
            print(line)

    # ------------------------------------------------------------
    # shared commands (item use/equip/examine work in and out of combat)
    # ------------------------------------------------------------

    def cmd_use(self, rest, in_combat=False):
        if not rest:
            print("\nUse what?")
            return
        key = self._resolve_item(rest, self.player.inventory)
        if not key:
            print(f"\nYou aren't carrying a '{rest}'.")
            return
        item = ITEMS[key]
        if item.item_type != "consumable":
            print(
                f"\nYou can't use the {item.name} like that. (Try 'equip' or 'examine'.)"
            )
            return
        if in_combat and self._apply_poison():
            return
        healed = self.player.heal(item.heal_amount)
        self.player.inventory.remove(key)
        print(
            f"\nYou drink the {item.name}, recovering {healed} HP. ({self.player.hp}/{self.player.max_hp} HP)"
        )
        if in_combat:
            self._monster_turn_and_check()

    def cmd_equip(self, rest, in_combat=False):
        if not rest:
            print("\nEquip what?")
            return
        key = self._resolve_item(rest, self.player.inventory)
        if not key:
            print(f"\nYou aren't carrying a '{rest}'.")
            return
        item = ITEMS[key]
        if item.item_type not in ("weapon", "armor"):
            print(f"\nThe {item.name} isn't something you can equip.")
            return
        if in_combat and self._apply_poison():
            return
        if item.item_type == "weapon":
            self.player.weapon = key
            print(
                f"\nYou equip the {item.name}. (Attack power: {self.player.total_attack()})"
            )
        else:
            self.player.armor = key
            print(
                f"\nYou equip the {item.name}. (Defense: {self.player.total_defense()})"
            )
        if in_combat:
            self._monster_turn_and_check()

    def cmd_examine(self, rest):
        room = self.current_room()
        if not rest:
            print("\n" + room.description)
            return
        rest_l = rest.lower()

        if rest_l in ("self", "me", "myself"):
            self.cmd_status()
            return
        if rest_l in ("room", "surroundings", "area", "here"):
            print("\n" + room.description)
            return
        if room.npc and self._npc_matches(rest_l, room.npc):
            print(f"\n{room.npc.description}")
            return
        if (
            room.monster
            and room.monster.is_alive()
            and rest_l in room.monster.name.lower()
        ):
            print(f"\n{room.monster.description}")
            return

        key = self._resolve_item(rest_l, room.items) or self._resolve_item(
            rest_l, self.player.inventory
        )
        if key:
            item = ITEMS[key]
            tags = []
            if key == self.player.weapon:
                tags.append("equipped weapon")
            if key == self.player.armor:
                tags.append("equipped armor")
            suffix = f" ({', '.join(tags)})" if tags else ""
            print(f"\n{item.name}{suffix}: {item.description}")
            return

        print(f"\nYou don't see a '{rest}' here.")

    def cmd_status(self):
        p = self.player
        room = self.current_room()
        weapon = ITEMS[p.weapon].name if p.weapon else "None (bare fists)"
        armor = ITEMS[p.armor].name if p.armor else "None"
        print()
        print("=" * WIDTH)
        print("STATUS")
        print("=" * WIDTH)
        hp_line = f"Health:    {p.hp}/{p.max_hp} HP"
        if p.poisoned_turns > 0:
            hp_line += "  [POISONED]"
        print(hp_line)
        print(f"Attack:    {p.total_attack()}  (weapon: {weapon})")
        print(f"Defense:   {p.total_defense()}  (armor: {armor})")
        print(f"Location:  {room.name}")
        if self.mode == "combat" and self.current_monster:
            m = self.current_monster
            print(f"Fighting:  {m.name} ({m.hp}/{m.max_hp} HP)")
        if p.defeated_monsters:
            print(f"Defeated:  {', '.join(p.defeated_monsters)}")
        print("=" * WIDTH)

    def cmd_inventory(self):
        if not self.player.inventory:
            print("\nYou are carrying nothing.")
            return
        print("\nYou are carrying:")
        for key, count in self.player.item_counts().items():
            item = ITEMS[key]
            tags = []
            if key == self.player.weapon:
                tags.append("equipped")
            if key == self.player.armor:
                tags.append("equipped")
            tag = f" [{tags[0]}]" if tags else ""
            suffix = f" x{count}" if count > 1 else ""
            print(f"  - {item.name}{suffix}{tag}")

    def cmd_help(self):
        print(HELP_TEXT)

    def cmd_quit(self):
        confirm = input("\nAre you sure you want to quit? (y/n): ").strip().lower()
        if confirm.startswith("y"):
            print("\nFarewell, wanderer.")
            self.running = False
        else:
            print("\nContinuing your journey...")

    # ------------------------------------------------------------
    # combat
    # ------------------------------------------------------------

    def start_combat(self, room):
        monster = room.monster
        self.mode = "combat"
        self.current_monster = monster
        print()
        print(monster.intro_text)
        print(
            f"({monster.name}: {monster.hp}/{monster.max_hp} HP) "
            f"-- attack, flee, use <item>, or equip <item>"
        )

    def _apply_poison(self):
        """Tick poison damage at the start of a combat action. Returns
        True if this killed the player (in which case the calling
        command should stop, having already been resolved by the
        poison tick).
        """
        if self.player.poisoned_turns > 0:
            self.player.poisoned_turns -= 1
            self.player.take_damage(3)
            print("\nPoison courses through you for 3 damage.")
            if not self.player.is_alive():
                self.trigger_defeat()
                return True
        return False

    def _monster_turn_and_check(self):
        monster = self.current_monster
        if monster is None or not monster.is_alive():
            return
        self.player.turns_taken += 1
        for line in monster.take_turn(self.player, self.player.turns_taken):
            print(line)
        if not self.player.is_alive():
            self.trigger_defeat()

    def combat_player_attack(self):
        if self._apply_poison():
            return
        monster = self.current_monster
        mult = monster.player_attack_multiplier(self.player.weapon)
        variance = random.randint(-1, 2)
        base = max(1, self.player.total_attack() + variance)
        dmg = max(1, int(round(base * mult)))
        monster.take_hit(dmg)
        weapon_name = (
            ITEMS[self.player.weapon].name if self.player.weapon else "bare fists"
        )
        print()
        if mult > 1.0:
            print(
                f"Your {weapon_name} flares bright -- a searing strike for {dmg} damage!"
            )
        elif 0 < mult < 1.0:
            print(
                f"Your {weapon_name} connects, but the blow feels blunted -- {dmg} damage."
            )
        else:
            print(f"You strike with your {weapon_name} for {dmg} damage!")
        print(f"({monster.name}: {monster.hp}/{monster.max_hp} HP)")

        if not monster.is_alive():
            self.trigger_victory_over_monster(monster)
            return
        self._monster_turn_and_check()

    def combat_player_flee(self):
        if self._apply_poison():
            return
        if random.random() < 0.65:
            print("\nYou break away and flee back the way you came!")
            self.player.poisoned_turns = 0
            self.player.location = self.player.previous_location
            self.mode = "explore"
            self.current_monster = None
            self.describe_room(self.current_room())
        else:
            print("\nYou try to flee, but there's no opening!")
            self._monster_turn_and_check()

    def trigger_victory_over_monster(self, monster):
        print(f"\n{monster.defeat_text}")
        self.player.poisoned_turns = 0
        if monster.key not in self.player.defeated_monsters:
            self.player.defeated_monsters.append(monster.key)
        self.mode = "explore"
        self.current_monster = None
        if monster.key == "ashen_king":
            self.mode = "over_win"
            return
        print(f"\nExits: {self.current_room().exit_list_text()}")

    def trigger_defeat(self):
        self.mode = "over_lose"

    # ------------------------------------------------------------
    # end screens
    # ------------------------------------------------------------

    def show_victory_screen(self):
        print()
        print("=" * WIDTH)
        print("VICTORY")
        print("=" * WIDTH)
        print(
            "The last ember in the Ashen King's crown gutters out. The "
            "keep is silent -- truly silent -- for the first time in "
            "years. Sunlight finds its way through the throne room's "
            "broken roof and touches the floor for the first time since "
            "the Keep fell."
        )
        print(
            f"\nYou defeated {len(self.player.defeated_monsters)} foes in "
            f"{self.player.turns_taken} combat rounds."
        )
        print("=" * WIDTH)
        print("\nThanks for playing THE ASHEN KEEP.")

    def show_defeat_screen(self):
        print()
        print("=" * WIDTH)
        print("YOU HAVE FALLEN")
        print("=" * WIDTH)
        foe = self.current_monster.name if self.current_monster else "the dark"
        print(f"The {foe} was too much for you.")
        print(
            "Your story ends here, in the ash of the keep -- one more "
            "shadow among many."
        )
        print("=" * WIDTH)
        choice = input("\nWould you like to restart? (y/n): ").strip().lower()
        return choice.startswith("y")

    # ------------------------------------------------------------
    # matching helpers
    # ------------------------------------------------------------

    def _resolve_item(self, rest, keys):
        """Match free text against a list of item keys, trying an
        exact match first, then falling back to substring matching so
        "sword" finds "Iron Longsword" and "brand" finds "Sunfire
        Brand".
        """
        rest = rest.lower().strip()
        if not rest:
            return None
        for k in keys:
            item = ITEMS[k]
            if rest == k or rest == k.replace("_", " ") or rest == item.name.lower():
                return k
        for k in keys:
            item = ITEMS[k]
            if rest in item.name.lower() or rest in k.replace("_", " "):
                return k
        return None

    def _npc_matches(self, rest, npc):
        rest = rest.lower().strip()
        if not rest:
            return True
        return rest in npc.name.lower() or rest in npc.key

    # ------------------------------------------------------------
    # save / load
    # ------------------------------------------------------------

    def to_dict(self):
        rooms_state = {}
        for key, room in self.rooms.items():
            entry = {"items": list(room.items), "visited": room.visited}
            m = room.monster
            if m is not None:
                entry["monster_hp"] = m.hp
                if hasattr(m, "phase"):
                    entry["monster_phase"] = m.phase
                if hasattr(m, "guard_active"):
                    entry["guard_active"] = m.guard_active
                if hasattr(m, "turns_taken"):
                    entry["monster_turns_taken"] = m.turns_taken
            rooms_state[key] = entry
        return {"player": self.player.to_dict(), "rooms": rooms_state}

    def load_from_data(self, data):
        self.rooms = build_world()
        self.player = Player.from_dict(data.get("player", {}))
        for key, entry in data.get("rooms", {}).items():
            room = self.rooms.get(key)
            if not room:
                continue
            room.items = list(entry.get("items", room.items))
            room.visited = entry.get("visited", room.visited)
            m = room.monster
            if m is not None:
                if "monster_hp" in entry:
                    m.hp = entry["monster_hp"]
                if "monster_phase" in entry and hasattr(m, "phase"):
                    m.phase = entry["monster_phase"]
                if "guard_active" in entry and hasattr(m, "guard_active"):
                    m.guard_active = entry["guard_active"]
                if "monster_turns_taken" in entry and hasattr(m, "turns_taken"):
                    m.turns_taken = entry["monster_turns_taken"]
        self.mode = "explore"
        self.current_monster = None

    def save_game(self):
        if self.mode == "combat":
            print("\nYou can't save in the middle of a fight!")
            return
        try:
            with open(SAVE_FILE, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
            print(f"\nGame saved to {SAVE_FILE}.")
        except OSError as exc:
            print(f"\nCould not save game: {exc}")

    def load_game(self):
        if not os.path.exists(SAVE_FILE):
            return False
        try:
            with open(SAVE_FILE, "r") as f:
                data = json.load(f)
            self.load_from_data(data)
            return True
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return False
