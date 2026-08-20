"""Core entity definitions for the adventure game.

This module defines the data classes used throughout the project:
Player, Monster, Item, NPC, and Room.  Each class provides methods to
serialize to a plain ``dict`` matching the contracts described in the
project blueprint, as well as classmethods to construct an instance from
such a dictionary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ItemType(str, Enum):
    """Allowed item categories."""

    WEAPON = "weapon"
    ARMOR = "armor"
    CONSUMABLE = "consumable"
    KEY = "key"


@dataclass
class Item:
    """A game item."""

    id: str
    name: str
    type: ItemType
    stats: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the item to a dict matching the world.yaml schema."""
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type.value,
            "stats": self.stats,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Item":
        """Create an Item from a dict (e.g. loaded from YAML)."""
        return cls(
            id=data["id"],
            name=data["name"],
            type=ItemType(data["type"]),
            stats=data.get("stats", {}),
            description=data.get("description", ""),
        )

    # -----------------------------------------------------------------
    # Helper methods for item usage
    # -----------------------------------------------------------------
    def is_consumable(self) -> bool:
        """Return True if the item is a consumable (e.g., healing item)."""
        return self.type.name == "CONSUMABLE"

    def get_heal_amount(self) -> int:
        """Extract the heal amount from stats; default to 0 if not present."""
        return int(self.stats.get("heal", 0))

    def apply_to_player(self, player: "Player") -> bool:
        """
        Apply this item's effect to a player.

        For consumable items with a ``heal`` stat, increase the player's health
        (capped at ``player.max_health``) and return True. If the item is not
        consumable or has no heal value, do nothing and return False.
        """
        if not self.is_consumable():
            return False

        heal = self.get_heal_amount()
        if heal <= 0:
            return False

        # Apply healing, respecting max health
        new_health = min(player.health + heal, player.max_health)
        player.health = new_health
        return True


@dataclass
class DialogueEntry:
    """A single dialogue trigger and its associated lines."""

    trigger: str
    lines: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {"trigger": self.trigger, "lines": self.lines}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DialogueEntry":
        return cls(trigger=data["trigger"], lines=data.get("lines", []))


@dataclass
class NPC:
    """A non‑player character."""

    id: str
    name: str
    dialogue: List[DialogueEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "dialogue": [d.to_dict() for d in self.dialogue],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "NPC":
        dialogue_data = data.get("dialogue", [])
        dialogue = [DialogueEntry.from_dict(d) for d in dialogue_data]
        return cls(id=data["id"], name=data["name"], dialogue=dialogue)


@dataclass
class Monster:
    """A monster that can engage the player in combat."""

    id: str
    name: str
    health: int
    attack: int
    behavior: str
    drops: List[str] = field(default_factory=list)

    max_health: int = field(init=False)

    def __post_init__(self) -> None:
        self.max_health = self.health

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "health": self.health,
            "attack": self.attack,
            "behavior": self.behavior,
            "drops": self.drops,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Monster":
        return cls(
            id=data["id"],
            name=data["name"],
            health=data["health"],
            attack=data["attack"],
            behavior=data["behavior"],
            drops=data.get("drops", []),
        )


@dataclass
class Room:
    """A location in the game world."""

    id: str
    name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)  # direction → room_id
    items: List[str] = field(default_factory=list)       # item ids present
    npcs: List[str] = field(default_factory=list)        # npc ids present
    monster: Optional[str] = None                        # monster id or None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the room to a dict matching the world.yaml schema."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "exits": self.exits,
            "items": self.items,
            "npcs": self.npcs,
            "monster": self.monster,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Room":
        """Create a Room from a dict (e.g. loaded from YAML)."""
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            exits=data.get("exits", {}),
            items=data.get("items", []),
            npcs=data.get("npcs", []),
            monster=data.get("monster"),
        )


@dataclass
class Player:
    """The player character."""

    name: str
    health: int
    max_health: int
    attack: int
    defense: int
    location: str                     # current room id
    inventory: List[str] = field(default_factory=list)  # item ids
    equipment: Dict[str, Optional[str]] = field(
        default_factory=lambda: {"weapon": None, "armor": None}
    )
    flags: Dict[str, bool] = field(default_factory=dict)

    @classmethod
    def create(cls, name: str, location: str) -> "Player":
        """Factory for a fresh player with sensible defaults."""
        return cls(
            name=name,
            health=100,
            max_health=100,
            attack=10,
            defense=5,
            location=location,
            inventory=[],
            equipment={"weapon": None, "armor": None},
            flags={},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the player to the contract‑defined ``player_state`` dict."""
        return {
            "name": self.name,
            "health": self.health,
            "max_health": self.max_health,
            "attack": self.attack,
            "defense": self.defense,
            "location": self.location,
            "inventory": self.inventory,
            "equipment": self.equipment,
            "flags": self.flags,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Player":
        """Recreate a Player from a ``player_state`` dict."""
        return cls(
            name=data["name"],
            health=data["health"],
            max_health=data["max_health"],
            attack=data["attack"],
            defense=data["defense"],
            location=data["location"],
            inventory=data.get("inventory", []),
            equipment=data.get("equipment", {"weapon": None, "armor": None}),
            flags=data.get("flags", {}),
        )

    # -----------------------------------------------------------------
    # Helper methods for inventory and equipment management
    # -----------------------------------------------------------------
    def equip(self, item_id: str, slot: str) -> None:
        """Equip an ``item_id`` into the given ``slot`` ('weapon' or 'armor').

        This method only updates the equipment mapping; stat adjustments are
        handled by the GameEngine after calling this method.
        """
        if slot not in ("weapon", "armor"):
            raise ValueError(f"Invalid equipment slot: {slot}")
        self.equipment[slot] = item_id

    def unequip(self, slot: str) -> None:
        """Remove any equipped item from ``slot`` ('weapon' or 'armor').

        This method only clears the equipment mapping; stat adjustments are
        handled by the GameEngine after calling this method.
        """
        if slot not in ("weapon", "armor"):
            raise ValueError(f"Invalid equipment slot: {slot}")
        self.equipment[slot] = None

    def add_item(self, item_id: str) -> None:
        """Add an ``item_id`` to the player's inventory."""
        self.inventory.append(item_id)

    def remove_item(self, item_id: str) -> bool:
        """Remove ``item_id`` from inventory; return True if it was present."""
        try:
            self.inventory.remove(item_id)
            return True
        except ValueError:
            return False

    # -----------------------------------------------------------------
    # Flag helpers (used for tracking NPC interactions, quest progress, etc.)
    # -----------------------------------------------------------------
    def has_flag(self, flag: str) -> bool:
        """Return the boolean value of ``flag`` (defaults to False)."""
        return self.flags.get(flag, False)

    def set_flag(self, flag: str, value: bool = True) -> None:
        """Set ``flag`` to ``value``."""
        self.flags[flag] = value
