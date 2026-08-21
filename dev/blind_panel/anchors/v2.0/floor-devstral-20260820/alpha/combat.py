from typing import Dict, List, Optional
from models import Player, Monster, Item
from game_state import GameState

class CombatEngine:
    def __init__(self, player: Player, monster: Monster, game_state: GameState):
        self.player = player
        self.monster = monster
        self.game_state = game_state

    def calculate_damage(self, attacker: Player, defender: Monster) -> int:
        base_damage = attacker.attack
        if "weapon" in self.game_state.equipment:
            weapon = self.game_state.equipment["weapon"]
            base_damage += weapon.stats.get("attack", 0)
        defense = getattr(defender, 'defense', 0)
        return max(1, base_damage - defense)

    def player_turn(self) -> bool:
        damage = self.calculate_damage(self.player, self.monster)
        self.monster.health -= damage

        if self.monster.health <= 0:
            return True  # Monster defeated

        return False  # Monster still alive

    def monster_turn(self) -> bool:
        damage = self.monster.attack
        if "armor" in self.game_state.equipment:
            armor = self.game_state.equipment["armor"]
            damage -= armor.stats.get("defense", 0)
        damage = max(1, damage)

        self.player.health -= damage

        if self.player.health <= 0:
            return True  # Player defeated

        return False  # Player still alive

    def fight(self) -> str:
        while True:
            # Player's turn
            if self.player_turn():
                return f"You defeated the {self.monster.name}!"

            # Monster's turn
            if self.monster_turn():
                return "You have been defeated."

            # Check for special items that might affect combat
            for item in self.game_state.inventory:
                if item.id == "silver_locket" and self.monster.name.lower() == "boss":
                    damage = self.calculate_damage(self.player, self.monster) * 2
                    self.monster.health -= damage
                    if self.monster.health <= 0:
                        return f"You defeated the {self.monster.name} using the silver locket!"

            # Check if monster is defeated after special item effect
            if self.monster.health <= 0:
                return f"You defeated the {self.monster.name}!"
