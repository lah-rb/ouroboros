from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Room:
    """Represents a game location."""

    id: str
    name: str
    description: str
    exits: Dict[str, str]  # direction -> room_id
    items: List[str]  # list of item IDs
    npcs: List[str]  # list of NPC IDs


@dataclass
class Item:
    """Represents a game item."""

    id: str
    name: str
    description: str
    can_take: bool
    can_use: bool


@dataclass
class NPC:
    """Represents a non-player character."""

    id: str
    name: str
    description: str
    dialogue: Dict[str, List[str]]  # topic -> [responses]


@dataclass
class GameState:
    """Represents the current state of the game."""

    current_room: str
    inventory: List[str] = field(default_factory=list)
    completed_actions: List[str] = field(default_factory=list)
    flags: Dict[str, bool] = field(default_factory=dict)
    room_descriptions_seen: Dict[str, bool] = field(default_factory=dict)


@dataclass
class Command:
    """Represents a parsed player command."""

    verb: str
    noun: Optional[str] = None
    raw: str = ""
    valid: bool = True
    error_message: Optional[str] = None
