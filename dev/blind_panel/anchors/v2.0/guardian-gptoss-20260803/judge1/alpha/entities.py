"""
Core data classes for the text adventure game.
All mutable game state lives in Game; these classes are simple containers.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------- Items ----------
@dataclass
class Item:
    """Base class for all items."""

    id: str
    name: str
    description: str
    type: str  # 'weapon', 'armor', 'healing', 'key', etc.


@dataclass
class Weapon(Item):
    attack_bonus: int = 0


@dataclass
class Armor(Item):
    """Armor item that can modify combat stats.

    Attributes:
        attack_bonus (int): Bonus added to the player's attack when equipped.
        defense_bonus (int): Bonus added to the player's defense when equipped.
    """

    attack_bonus: int = 0
    defense_bonus: int = 0


@dataclass
class HealingItem(Item):
    heal_amount: int = 0


# ---------- NPC ----------
@dataclass
class NPC:
    id: str
    name: str
    dialogue: Dict[str, str]  # node -> text
    triggers: Dict[str, str] = field(default_factory=dict)  # unused for now


# ---------- Monster ----------
@dataclass
class Monster:
    id: str
    name: str
    max_health: int
    health: int
    attack: int
    behavior: str  # 'aggressive', 'defensive', 'coward', 'boss'
    location: str  # room_id


# ---------- Player ----------
@dataclass
class Player:
    """Represents the player character, tracking health and combat stats.

    Attributes:
        health (int): Current health points of the player.
        base_attack (int): Attack value without any equipment bonuses.
        base_defense (int): Defense value without any equipment bonuses.
        weapon (Optional[Weapon]): Currently equipped weapon, if any.
        armor (Optional[Armor]): Currently equipped armor, if any.
    """

    health: int = 20
    base_attack: int = 1
    base_defense: int = 0

    weapon: Optional[Weapon] = None
    armor: Optional[Armor] = None

    @property
    def attack(self) -> int:
        """Total attack including equipped items."""
        bonus = 0
        if self.weapon:
            bonus += self.weapon.attack_bonus
        if self.armor:
            bonus += self.armor.attack_bonus
        return self.base_attack + bonus

    @property
    def defense(self) -> int:
        """Total defense including equipped armor."""
        bonus = 0
        if self.armor:
            bonus += self.armor.defense_bonus
        return self.base_defense + bonus


# ---------- Room ----------
@dataclass
class Room:
    id: str
    name: str
    description: str
    exits: Dict[str, str]  # direction -> room_id
    items: List[str] = field(default_factory=list)  # item ids present
    monsters: List[str] = field(default_factory=list)  # monster ids present
    npcs: List[str] = field(default_factory=list)  # npc ids present
