"""Data models for the text adventure engine."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Item:
    """Represents an item in the game world."""
    id: str
    name: str
    description: str
    can_take: bool = True
    location: Optional[str] = None  # Room ID where item is located
    in_inventory: bool = False


@dataclass
class NPC:
    """Represents a non-player character."""
    id: str
    name: str
    description: str
    room_id: str
    dialogue_tree: dict = field(default_factory=dict)
    current_state: str = "intro"


@dataclass
class Room:
    """Represents a location in the game world."""
    id: str
    name: str
    description: str
    connections: dict = field(default_factory=dict)  # direction -> room_id
    items: list = field(default_factory=list)  # List of item IDs
    npcs: list = field(default_factory=list)  # List of NPC IDs


@dataclass
class GameState:
    """Current state of the game."""
    player_room: str
    inventory: list = field(default_factory=list)  # List of item IDs
    room_states: dict = field(default_factory=dict)  # room_id -> state
    npc_states: dict = field(default_factory=dict)  # npc_id -> current_state
    completed_actions: list = field(default_factory=list)
