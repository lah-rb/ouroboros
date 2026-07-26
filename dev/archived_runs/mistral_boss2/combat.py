from enum import Enum, auto
from typing import Union, Optional, Dict
from dataclasses import dataclass
from models import Player, Monster, Boss, Weapon, Armor, HealingItem


class CombatAction(Enum):
    ATTACK = auto()
    USE_ITEM = auto()
    FLEE = auto()


@dataclass
class CombatResult:
    player_health: int
    enemy_health: int
    player_damage: int = 0
    enemy_damage: int = 0
    is_player_defeated: bool = False
    is_enemy_defeated: bool = False
    fled: bool = False
    message: str = ""
    phase_change: bool = False
    weakness_used: bool = False


class CombatEngine:
    def __init__(self, player: Player, enemy: Union[Monster, Boss]):
        self.player = player
        self.enemy = enemy
        self.is_boss = isinstance(enemy, Boss)
        self.phase2 = (
            False if not self.is_boss else enemy.health <= enemy.phase2_health_threshold
        )

    def _calculate_player_attack(
        self, items: Dict[str, Union[Weapon, Armor, HealingItem]]
    ) -> int:
        base = self.player.base_attack
        if EquipmentSlot.WEAPON in self.player.equipped:
            item_id = self.player.equipped[EquipmentSlot.WEAPON]
            if item_id in items and isinstance(items[item_id], Weapon):
                base += items[item_id].damage
        return max(1, base)

    def _calculate_player_defense(
        self, items: Dict[str, Union[Weapon, Armor, HealingItem]]
    ) -> int:
        base = self.player.base_defense
        if EquipmentSlot.ARMOR in self.player.equipped:
            item_id = self.player.equipped[EquipmentSlot.ARMOR]
            if item_id in items and isinstance(items[item_id], Armor):
                base += items[item_id].defense
        return base

    def _calculate_damage(self, attack: int, defense: int) -> int:
        return max(1, attack - defense // 2)

    def resolve_attack(
        self,
        items: Dict[str, Union[Weapon, Armor, HealingItem]],
        weakness_item_id: Optional[str] = None,
    ) -> CombatResult:
        player_attack = self._calculate_player_attack(items)
        player_defense = self._calculate_player_defense(items)

        enemy_attack = self.enemy.attack
        if self.is_boss and isinstance(self.enemy, Boss):
            if self.phase2:
                enemy_attack = self.enemy.phase2_attack

        # Player attacks first
        player_damage = self._calculate_damage(player_attack, self.enemy.defense)
        enemy_damage = self._calculate_damage(enemy_attack, player_defense)

        new_enemy_health = self.enemy.health - player_damage
        new_player_health = self.player.health - enemy_damage

        # Check for weakness
        weakness_used = False
        if self.is_boss and isinstance(self.enemy, Boss) and weakness_item_id:
            if (
                EquipmentSlot.WEAPON in self.player.equipped
                and self.player.equipped[EquipmentSlot.WEAPON] == weakness_item_id
            ):
                player_damage *= 2
                new_enemy_health = self.enemy.health - player_damage
                weakness_used = True

        # Check for phase change (boss only)
        phase_change = False
        if self.is_boss and isinstance(self.enemy, Boss):
            if (
                not self.phase2
                and new_enemy_health <= self.enemy.phase2_health_threshold
            ):
                self.phase2 = True
                phase_change = True

        is_enemy_defeated = new_enemy_health <= 0
        is_player_defeated = new_player_health <= 0

        # Build message
        message_parts = []
        message_parts.append(
            f"You attack the {self.enemy.name} for {player_damage} damage!"
        )
        if weakness_used:
            message_parts[-1] = (
                f"You strike the {self.enemy.name} with the {weakness_item_id.replace('_', ' ').title()} for {player_damage} damage! It's super effective!"
            )

        if not is_enemy_defeated:
            if phase_change:
                message_parts.append(
                    f"The {self.enemy.name} roars in pain and enters phase 2!"
                )
            message_parts.append(
                f"The {self.enemy.name} hits you for {enemy_damage} damage."
            )

        message = " ".join(message_parts)

        return CombatResult(
            player_health=new_player_health,
            enemy_health=new_enemy_health,
            player_damage=player_damage,
            enemy_damage=enemy_damage,
            is_player_defeated=is_player_defeated,
            is_enemy_defeated=is_enemy_defeated,
            message=message,
            phase_change=phase_change,
            weakness_used=weakness_used,
        )

    def use_healing_item(self, item: HealingItem) -> CombatResult:
        new_health = min(self.player.max_health, self.player.health + item.heal_amount)
        return CombatResult(
            player_health=new_health,
            enemy_health=self.enemy.health,
            message=f"You use {item.name} and heal for {item.heal_amount} health.",
        )

    def flee(self) -> CombatResult:
        return CombatResult(
            player_health=self.player.health,
            enemy_health=self.enemy.health,
            fled=True,
            message=f"You attempt to flee from the {self.enemy.name}!",
        )
