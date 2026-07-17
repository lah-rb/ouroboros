"""
Core data models for the text adventure game.
All classes are simple dataclasses that can be serialized to/from dicts.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional


@dataclass
class Item:
    id: str
    name: str
    type: str  # e.g., "weapon", "armor", "healing", "key"
    description: str
    attack: int = 0
    defense: int = 0
    healing: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Item":
        return Item(**data)


@dataclass
class NPCDialogue:
    text: str
    next: Optional[int]  # index of next dialogue entry or None


@dataclass
class NPC:
    id: str
    name: str
    dialogues: List[NPCDialogue]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "dialogues": [{"text": d.text, "next": d.next} for d in self.dialogues],
        }

    @staticmethod
    def from_dict(data: dict) -> "NPC":
        dialogues = [
            NPCDialogue(text=entry["text"], next=entry.get("next"))
            for entry in data["dialogues"]
        ]
        return NPC(id=data["id"], name=data["name"], dialogues=dialogues)


@dataclass
class Monster:
    id: str
    name: str
    description: str
    health: int
    max_health: int
    attack: int
    defense: int
    behavior: str  # e.g., "aggressive", "defensive"

    def is_alive(self) -> bool:
        return self.health > 0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Monster":
        data = data.copy()
        data.setdefault("max_health", data["health"])
        return Monster(**data)


@dataclass
class Room:
    id: str
    name: str
    description: str
    connections: Dict[str, str]  # direction -> room_id
    items: List[str] = field(default_factory=list)  # item ids present in the room
    npcs: List[str] = field(default_factory=list)  # npc ids present
    monsters: List[str] = field(default_factory=list)  # monster ids present

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "connections": self.connections,
            "items": self.items,
            "npcs": self.npcs,
            "monsters": self.monsters,
        }

    @staticmethod
    def from_dict(data: dict) -> "Room":
        return Room(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            connections=data.get("connections", {}),
            items=data.get("items", []),
            npcs=data.get("npcs", []),
            monsters=data.get("monsters", []),
        )


@dataclass
class Player:
    health: int
    max_health: int
    attack: int
    defense: int
    inventory: List[str] = field(default_factory=list)  # item ids
    equipped_weapon: Optional[str] = None  # item id
    equipped_armor: Optional[str] = None  # item id

    def effective_attack(self, items: Dict[str, Item]) -> int:
        base = self.attack
        if self.equipped_weapon:
            weapon = items.get(self.equipped_weapon)
            if weapon:
                base += weapon.attack
        return base

    def effective_defense(self, items: Dict[str, Item]) -> int:
        base = self.defense
        if self.equipped_armor:
            armor = items.get(self.equipped_armor)
            if armor:
                base += armor.defense
        return base

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Player":
        return Player(**data)


@dataclass
class GameState:
    current_room_id: str
    player: Player
    rooms: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    # rooms[room_id] = {"items": [...], "monsters": [...], "npcs": [...]}
    npc_dialogue_progress: Dict[str, int] = field(default_factory=dict)
    defeated_monsters: List[str] = field(default_factory=list)
    turn_counter: int = 0

    def to_dict(self) -> dict:
        return {
            "current_room_id": self.current_room_id,
            "player": self.player.to_dict(),
            "rooms": self.rooms,
            "npc_dialogue_progress": self.npc_dialogue_progress,
            "defeated_monsters": self.defeated_monsters,
            "turn_counter": self.turn_counter,
        }

    @staticmethod
    def from_dict(data: dict) -> "GameState":
        player = Player.from_dict(data["player"])
        return GameState(
            current_room_id=data["current_room_id"],
            player=player,
            rooms=data.get("rooms", {}),
            npc_dialogue_progress=data.get("npc_dialogue_progress", {}),
            defeated_monsters=data.get("defeated_monsters", []),
            turn_counter=data.get("turn_counter", 0),
        )
