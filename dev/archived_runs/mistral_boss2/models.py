from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Union
from enum import Enum, auto


class Direction(Enum):
    NORTH = auto()
    SOUTH = auto()
    EAST = auto()
    WEST = auto()
    UP = auto()
    DOWN = auto()


class EquipmentSlot(Enum):
    WEAPON = auto()
    ARMOR = auto()


@dataclass
class Item:
    id: str
    name: str
    description: str
    type: str


@dataclass
class Weapon(Item):
    damage: int = 0

    def __post_init__(self):
        self.type = "weapon"


@dataclass
class Armor(Item):
    defense: int = 0

    def __post_init__(self):
        self.type = "armor"


@dataclass
class HealingItem(Item):
    heal_amount: int = 0

    def __post_init__(self):
        self.type = "healing"


@dataclass
class NPC:
    id: str
    name: str
    description: str
    dialogue: List[Dict[str, Union[str, int, None]]] = field(default_factory=list)


@dataclass
class Monster:
    id: str
    name: str
    description: str
    health: int = 0
    attack: int = 0
    defense: int = 0


@dataclass
class Boss:
    id: str
    name: str
    description: str
    health: int = 0
    attack: int = 0
    defense: int = 0
    phase2_health_threshold: int = 0
    phase2_attack: int = 0
    weakness_item: Optional[str] = None


@dataclass
class Room:
    id: str
    title: str
    description: str
    exits: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    npcs: List[str] = field(default_factory=list)
    monster: Optional[str] = None
    boss: Optional[str] = None


@dataclass
class Player:
    health: int = 30
    max_health: int = 30
    base_attack: int = 5
    base_defense: int = 2
    inventory: List[str] = field(default_factory=list)
    equipped: Dict[str, str] = field(default_factory=dict)

    @property
    def attack(self) -> int:
        base = self.base_attack
        if 'weapon' in self.equipped:
            # This will be resolved via the items dict in game state
            return base  # actual calculation happens in engine with item lookup
        return base

    @property
    def defense(self) -> int:
        base = self.base_defense
        if 'armor' in self.equipped:
            # This will be resolved via the items dict in game state
            return base  # actual calculation happens in engine with item lookup
        return base


@dataclass
class GameState:
    player: Player = field(default_factory=Player)
    current_room_id: str = ""
    inventory: List[str] = field(default_factory=list)
    equipped: Dict[str, str] = field(default_factory=dict)  # slot_name -> item_id
    room_states: Dict[str, Dict[str, Union[List[str], Optional[str]]]] = field(
        default_factory=dict
    )
    npc_dialogue_progress: Dict[str, int] = field(default_factory=dict)
    defeated_monsters: Set[str] = field(default_factory=set)
    boss_defeated: bool = False
