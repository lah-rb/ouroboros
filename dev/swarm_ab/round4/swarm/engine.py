"""
Main game engine handling the loop, command dispatch, mutable state,
and coordination of subsystems (world data, combat, save/load).

The engine respects the canonical data contracts:
- Player (models.Player)
- RoomState: {items: list[str], monsters: list[str], visited: bool}
- NPCState: {dialogue_index: int}
- MonsterState: {health: int, alive: bool, phase: int (optional)}
- GameSaveJSON: {"player": Player, "rooms": ..., "npcs": ..., "monsters": ...}
"""

from __future__ import annotations

from typing import Dict, Any, List, Optional

from models import Player, Room, Item, NPC, Monster, Boss
from world_loader import load_world
from parser import parse_command
from combat import CombatEngine
from save_load import save_game, load_game


class GameEngine:
    """Core engine coordinating world state, player actions and combat."""

    def __init__(self, world_data: Dict[str, Dict[str, Any]]) -> None:
        """
        Initialise the engine with static world definitions.

        Args:
            world_data: Mapping produced by ``world_loader.load_world``.
                Expected keys: "rooms", "items", "npcs", "monsters", optional "bosses".
                Values are mappings of identifier → model instance.
        """
        # Static look‑ups
        self.rooms: Dict[str, Room] = world_data["rooms"]
        self.items: Dict[str, Item] = world_data["items"]
        self.npcs: Dict[str, NPC] = world_data["npcs"]
        self.monsters: Dict[str, Monster] = world_data.get("monsters", {})
        self.bosses: Dict[str, Boss] = world_data.get("bosses", {})

        # Mutable runtime state
        self.room_state: Dict[str, Dict[str, Any]] = {}
        for rid, room in self.rooms.items():
            self.room_state[rid] = {
                "items": list(room.items),
                "monsters": list(room.monsters),
                "visited": False,
            }

        self.npc_state: Dict[str, Dict[str, int]] = {
            nid: {"dialogue_index": 0} for nid in self.npcs
        }

        self.monster_state: Dict[str, Dict[str, Any]] = {}
        for mid, mon in self.monsters.items():
            self.monster_state[mid] = {"health": mon.health, "alive": True}
        for bid, boss in self.bosses.items():
            self.monster_state[bid] = {
                "health": boss.health_phase1,
                "alive": True,
                "phase": 1,
            }

        # Player initialisation – allow external injection via world_data['player']
        if "player" in world_data:
            self.player: Player = world_data["player"]
        else:
            # Default start location is the first room defined
            start_room_id = next(iter(self.rooms))
            self.player = Player(
                location=start_room_id,
                health=10,
                max_health=10,
                attack=2,
                defense=1,
                inventory=[],
                equipped_weapon=None,
                equipped_armor=None,
            )

        # Track equipped bonuses so they can be removed when changing gear
        self._equipped_weapon_bonus: int = 0
        self._equipped_armor_bonus: int = 0

    # ------------------------------------------------------------------ #
    # Helper utilities
    # ------------------------------------------------------------------ #

    def _current_room(self) -> Room:
        return self.rooms[self.player.location]

    def _current_room_state(self) -> Dict[str, Any]:
        return self.room_state[self.player.location]

    def _apply_equipment(self, item: Item) -> None:
        """Equip a weapon or armor, adjusting player stats."""
        if item.type == "weapon":
            # Remove previous weapon bonus
            self.player.attack -= self._equipped_weapon_bonus
            # Apply new bonus
            self.player.attack += item.attack_bonus
            self._equipped_weapon_bonus = item.attack_bonus
            self.player.equipped_weapon = item.id
        elif item.type == "armor":
            self.player.defense -= self._equipped_armor_bonus
            self.player.defense += item.defense_bonus
            self._equipped_armor_bonus = item.defense_bonus
            self.player.equipped_armor = item.id

    def _remove_equipment(self, item: Item) -> None:
        """Unequip weapon or armor (used when dropping)."""
        if item.type == "weapon" and self.player.equipped_weapon == item.id:
            self.player.attack -= self._equipped_weapon_bonus
            self._equipped_weapon_bonus = 0
            self.player.equipped_weapon = None
        elif item.type == "armor" and self.player.equipped_armor == item.id:
            self.player.defense -= self._equipped_armor_bonus
            self._equipped_armor_bonus = 0
            self.player.equipped_armor = None

    def _build_save_state(self) -> Dict[str, Any]:
        """Construct a dict adhering to the GameSaveJSON contract."""
        return {
            "player": self.player,
            "rooms": self.room_state,
            "npcs": self.npc_state,
            "monsters": self.monster_state,
        }

    def _load_save_state(self, state: Dict[str, Any]) -> None:
        """Restore mutable state from a saved dict."""
        # Player
        saved_player: Player = state["player"]
        self.player.location = saved_player.location
        self.player.health = saved_player.health
        self.player.max_health = saved_player.max_health
        self.player.attack = saved_player.attack
        self.player.defense = saved_player.defense
        self.player.inventory = list(saved_player.inventory)
        self.player.equipped_weapon = saved_player.equipped_weapon
        self.player.equipped_armor = saved_player.equipped_armor

        # Re‑apply equipment bonuses based on equipped items
        self._equipped_weapon_bonus = 0
        self._equipped_armor_bonus = 0
        if self.player.equipped_weapon:
            weap = self.items.get(self.player.equipped_weapon)
            if weap and weap.type == "weapon":
                self._apply_equipment(weap)
        if self.player.equipped_armor:
            arm = self.items.get(self.player.equipped_armor)
            if arm and arm.type == "armor":
                self._apply_equipment(arm)

        # Rooms, NPCs, Monsters
        self.room_state = state["rooms"]
        self.npc_state = state["npcs"]
        self.monster_state = state["monsters"]

    # ------------------------------------------------------------------ #
    # Command handling
    # ------------------------------------------------------------------ #

    def _handle_move(self, direction: str) -> str:
        room = self._current_room()
        if direction not in room.connections:
            return f"You can't go {direction} from here."
        new_room_id = room.connections[direction]
        self.player.location = new_room_id
        self.room_state[new_room_id]["visited"] = True
        new_room = self.rooms[new_room_id]
        return f"You move {direction} to {new_room.name}.\n{new_room.description}"

    def _handle_look(self) -> str:
        room = self._current_room()
        state = self._current_room_state()
        lines: List[str] = [f"{room.name}\n{room.description}"]
        if state["items"]:
            lines.append("You see the following items: " + ", ".join(state["items"]))
        if state["monsters"]:
            lines.append("Monsters present: " + ", ".join(state["monsters"]))
        if room.npcs:
            lines.append("People here: " + ", ".join(room.npcs))
        return "\n".join(lines)

    def _handle_status(self) -> str:
        eq_weapon = self.player.equipped_weapon or "none"
        eq_armor = self.player.equipped_armor or "none"
        inv = ", ".join(self.player.inventory) if self.player.inventory else "empty"
        return (
            f"Health: {self.player.health}/{self.player.max_health}\n"
            f"Attack: {self.player.attack} (weapon: {eq_weapon})\n"
            f"Defense: {self.player.defense} (armor: {eq_armor})\n"
            f"Inventory: {inv}"
        )

    def _handle_take(self, item_id: str) -> str:
        state = self._current_room_state()
        if item_id not in state["items"]:
            return f"There is no '{item_id}' here."
        state["items"].remove(item_id)
        self.player.inventory.append(item_id)
        return f"You take the {item_id}."

    def _handle_drop(self, item_id: str) -> str:
        if item_id not in self.player.inventory:
            return f"You don't have '{item_id}'."
        self.player.inventory.remove(item_id)
        self._remove_equipment(self.items[item_id])
        self._current_room_state()["items"].append(item_id)
        return f"You drop the {item_id}."

    def _handle_use(self, item_id: str) -> str:
        if item_id not in self.player.inventory:
            return f"You don't have '{item_id}'."
        item = self.items.get(item_id)
        if not item:
            return f"Unknown item '{item_id}'."

        if item.type == "healing":
            heal_amount = item.heal_amount or 5
            new_hp = min(self.player.max_health, self.player.health + heal_amount)
            self.player.health = new_hp
            self.player.inventory.remove(item_id)
            return f"You use the {item_id} and recover {heal_amount} health."
        elif item.type == "weapon":
            self._apply_equipment(item)
            return f"You equip the weapon '{item_id}'."
        elif item.type == "armor":
            self._apply_equipment(item)
            return f"You equip the armor '{item_id}'."
        else:
            return f"The {item_id} cannot be used."

    def _handle_examine(self, target_id: str) -> str:
        # Check items first
        if target_id in self.items:
            itm = self.items[target_id]
            return f"{itm.name}: {itm.description}"
        # NPCs
        if target_id in self.npcs:
            npc = self.npcs[target_id]
            return f"{npc.name}: {npc.description}"
        # Monsters / bosses
        if target_id in self.monsters or target_id in self.bosses:
            mon = self.monsters.get(target_id) or self.bosses.get(target_id)
            return f"{mon.name}: {mon.description}"
        return f"There is nothing notable about '{target_id}'."

    def _handle_talk(self, npc_id: str) -> str:
        room = self._current_room()
        if npc_id not in room.npcs:
            return f"{npc_id} is not here."
        state = self.npc_state.get(npc_id, {"dialogue_index": 0})
        idx = state["dialogue_index"]
        dialogue = self.npcs[npc_id].dialogue
        if idx >= len(dialogue):
            return "They have nothing more to say."
        line = dialogue[idx]
        # Advance index for next interaction
        self.npc_state[npc_id]["dialogue_index"] = idx + 1
        return f'{self.npcs[npc_id].name} says: "{line}"'

    def _handle_attack(self, target_id: str) -> str:
        room_state = self._current_room_state()
        if target_id not in room_state["monsters"]:
            return f"There is no '{target_id}' to attack here."
        # Resolve opponent object
        opponent = self.monsters.get(target_id) or self.bosses.get(target_id)
        if not opponent:
            return f"'{target_id}' data missing."

        # Sync opponent health from monster_state
        mon_state = self.monster_state[target_id]
        if isinstance(opponent, Monster):
            opponent.health = mon_state["health"]
        else:  # Boss
            opponent._current_phase = mon_state.get("phase", 1)
            opponent._current_health = mon_state["health"]

        combat = CombatEngine(self.player, opponent)
        combat.start()

        # Update monster_state after combat
        if isinstance(opponent, Monster):
            mon_state["health"] = opponent.health
            mon_state["alive"] = opponent.health > 0
        else:  # Boss
            mon_state["health"] = getattr(opponent, "_current_health", 0)
            mon_state["alive"] = mon_state["health"] > 0
            mon_state["phase"] = getattr(opponent, "_current_phase", 1)

        if not mon_state["alive"]:
            room_state["monsters"].remove(target_id)
            return f"You have defeated {opponent.name}!"
        else:
            return f"The fight ends. Your health is now {self.player.health}/{self.player.max_health}."

    def _handle_save(self, filename: str) -> str:
        state = self._build_save_state()
        try:
            save_game(state, filename)
            return f"Game saved to '{filename}'."
        except OSError as exc:
            return f"Failed to save game: {exc}"

    def _handle_load(self, filename: str) -> str:
        try:
            state = load_game(filename)
            self._load_save_state(state)
            return f"Game loaded from '{filename}'."
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            return f"Failed to load game: {exc}"

    def process_command(self, raw: str) -> Optional[str]:
        """
        Parse and execute a command string.

        Returns a textual response or ``None`` for commands that end the loop.
        """
        try:
            cmd = parse_command(raw)
        except ValueError as exc:
            return f"Error: {exc}"

        action = cmd["action"]

        if action == "quit":
            return None
        if action == "move":
            return self._handle_move(cmd["direction"])
        if action == "look":
            return self._handle_look()
        if action == "status":
            return self._handle_status()
        if action == "help":
            return (
                "Commands: go <dir>, look, status, take <item>, drop <item>, "
                "use <item>, examine <target>, talk to <npc>, attack <monster>, "
                "save [filename], load [filename], quit"
            )
        if action == "take":
            return self._handle_take(cmd["item_id"])
        if action == "drop":
            return self._handle_drop(cmd["item_id"])
        if action == "use":
            return self._handle_use(cmd["item_id"])
        if action == "examine":
            return self._handle_examine(cmd["target_id"])
        if action == "talk":
            return self._handle_talk(cmd["npc_id"])
        if action == "attack":
            return self._handle_attack(cmd["target_id"])
        if action == "save":
            return self._handle_save(cmd["filename"])
        if action == "load":
            return self._handle_load(cmd["filename"])
        if action == "flee":
            # Flee is only meaningful inside combat; here we just acknowledge.
            return "You can't flee now."
        return f"Unimplemented command: {action}"

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Run the interactive game loop until the player quits."""
        print("Welcome to the adventure! Type 'help' for commands.")
        while True:
            try:
                raw = input("> ")
            except EOFError:
                break  # End of input stream
            response = self.process_command(raw)
            if response is None:
                print("Goodbye!")
                break
            if response:
                print(response)


# ---------------------------------------------------------------------- #
# Helper to run the engine directly (useful for manual testing)
# ---------------------------------------------------------------------- #

if __name__ == "__main__":
    world = load_world()
    engine = GameEngine(world)
    engine.start()
