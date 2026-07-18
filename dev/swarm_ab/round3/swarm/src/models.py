"""Core data classes representing game entities."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class Item:
    """Base class for all items.

    Attributes:
        id: Unique identifier.
        name: Human‑readable name.
        description: Text shown to the player.
        type: Category string (e.g. "weapon", "armor").
    """
    id: str
    name: str
    description: str
    type: str

@dataclass
class Weapon(Item):
    """Weapon item that adds attack power.

    Attributes:
        attack_bonus: Additional attack points when equipped.
    """
    attack_bonus: int = 0

@dataclass
class Armor(Item):
    """Armor item that adds defense power.

    Attributes:
        defense_bonus: Additional defense points when equipped.
    """
    defense_bonus: int = 0

@dataclass
class HealingItem(Item):
    """Consumable item that restores health.

    Attributes:
        heal_amount: Amount of HP restored when used.
    """
    heal_amount: int = 0

@dataclass
class Monster:
    """A hostile creature.

    Attributes:
        id: Unique identifier.
        name: Display name.
        health: Current hit points.
        max_health: Maximum hit points.
        attack: Damage dealt per combat round.
        description: Flavor text.
    """
    id: str
    name: str
    health: int
    max_health: int
    attack: int
    description: str

@dataclass
class NPC:
    """Non‑player character that can talk.

    Attributes:
        id: Unique identifier.
        name: Display name.
        location: Room id where the NPC resides.
        dialogue: List of sentences the NPC can say in order.
        dialogue_index: Index of next line to present.
    """
    id: str
    name: str
    location: str
    dialogue: List[str]
    dialogue_index: int = 0

@dataclass
class Room:
    """A location in the game world.

    Attributes:
        id: Unique identifier.
        name: Display name.
        description: Text shown when player looks.
        connections: Mapping from direction name to target room id.
        items: List of Item objects currently in the room.
        npcs: List of NPC objects currently in the room.
        monster: Optional Monster present in the room.
    """
    id: str
    name: str
    description: str
    connections: Dict[str, str]
    items: List[Item] = field(default_factory=list)
    npcs: List[NPC] = field(default_factory=list)
    monster: Optional[Monster] = None

@dataclass
class Player:
    """The human‑controlled character.

    Attributes:
        location: Current room id.
        health: Current hit points.
        max_health: Maximum hit points.
        attack: Base attack value (modified by equipped weapon).
        defense: Base defense value (modified by equipped armor).
        inventory: List of Item objects carried.
        equipped: Mapping with keys 'weapon' and 'armor' to the equipped Item or None.
    """
    location: str
    health: int
    max_health: int
    attack: int
    defense: int
    inventory: List[Item] = field(default_factory=list)
    equipped: Dict[str, Optional[Item]] = field(default_factory=lambda: {"weapon": None, "armor": None})

@dataclass
class GameState:
    """Aggregated mutable state of a running game.

    Attributes:
        player: The Player instance.
        rooms: Mapping from room id to Room instance.
        npcs: Mapping from npc id to NPC instance.
        monsters: Mapping from monster id to Monster instance.
    """
    player: Player
    rooms: Dict[str, Room]
    npcs: Dict[str, NPC]
    monsters: Dict[str, Monster]
