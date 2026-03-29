"""Data models for the text adventure game engine."""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any


@dataclass
class Item:
    """Represents an item in the game world."""
    id: str
    name: str
    description: str
    can_take: bool = True
    can_use: bool = True
    use_description: Optional[str] = None
    location: Optional[str] = None  # Room ID where item is located


@dataclass
class NPC:
    """Represents a non-player character."""
    id: str
    name: str
    description: str
    dialogue: Dict[str, List[str]] = field(default_factory=dict)  # State -> response lines
    initial_state: str = "default"
    current_state: str = "default"
    location: Optional[str] = None  # Room ID where NPC is located


@dataclass
class Room:
    """Represents a room in the game world."""
    id: str
    name: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)  # Direction -> Room ID
    items: List[str] = field(default_factory=list)  # Item IDs in the room
    npcs: List[str] = field(default_factory=list)  # NPC IDs in the room


@dataclass
class GameState:
    """Current state of the game."""
    player_location: str
    inventory: List[str] = field(default_factory=list)  # Item IDs in inventory
    room_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # Room ID -> state changes
    npc_states: Dict[str, str] = field(default_factory=dict)  # NPC ID -> current dialogue state
    completed_actions: List[str] = field(default_factory=list)  # Track completed actions for progression
