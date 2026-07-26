from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class Direction(Enum):
    NORTH = "north"
    SOUTH = "south"
    EAST = "east"
    WEST = "west"


class CommandType(Enum):
    MOVE = "move"
    TAKE = "take"
    DROP = "drop"
    USE = "use"
    EXAMINE = "examine"
    TALK = "talk"
    ATTACK = "attack"
    FLEE = "flee"
    LOOK = "look"
    STATUS = "status"
    HELP = "help"
    QUIT = "quit"
    SAVE = "save"
    LOAD = "load"
    INVENTORY = "inventory"


@dataclass
class Room:
    id: str
    name: str
    description: str
    connections: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    npcs: List[str] = field(default_factory=list)
    monsters: List[str] = field(default_factory=list)


@dataclass
class Item:
    id: str
    name: str
    type: str  # weapon, armor, healing, key
    stats: Dict[str, int] = field(default_factory=dict)
    description: str = ""


@dataclass
class NPC:
    id: str
    name: str
    dialogue: List[str] = field(default_factory=list)
    quest_flags: List[str] = field(default_factory=list)


@dataclass
class Monster:
    id: str
    name: str
    health: int
    attack: int
    behavior: str  # aggressive, passive, guard
    loot: List[str] = field(default_factory=list)
    phases: List[Dict] = field(default_factory=list)


@dataclass
class Player:
    location: str
    health: int
    max_health: int
    base_attack: int
    inventory: List[str] = field(default_factory=list)
    equipment: Dict[str, Optional[str]] = field(
        default_factory=lambda: {"weapon": None, "armor": None}
    )
    flags: Dict[str, bool] = field(default_factory=dict)


@dataclass
class Command:
    type: CommandType
    target: Optional[str] = None
    args: Dict[str, Optional[str]] = field(default_factory=dict)
