"""Core data classes for the adventure game."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class Item:
    """Base item definition.

    Args:
        id: Unique identifier.
        name: Human‑readable name.
        description: Text shown to the player.
        type: Category (e.g. "weapon", "armor", "healing", "key").
        stats: Mapping of stat names to integer values (may be empty).

    >>> sword = Item(id="sword", name="Sword", description="A blade.", type="weapon", stats={"attack_bonus": 5})
    >>> sword.id
    'sword'
    """

    id: str
    name: str
    description: str
    type: str
    stats: Dict[str, int] = field(default_factory=dict)


@dataclass
class Weapon(Item):
    """Weapon item; inherits all Item fields.

    The ``attack_bonus`` is stored in ``stats["attack_bonus"]``.
    """

    pass


@dataclass
class Armor(Item):
    """Armor item; inherits all Item fields.

    The ``defense_bonus`` is stored in ``stats["defense_bonus"]``.
    """

    pass


@dataclass
class HealingItem(Item):
    """Healing consumable.

    The amount healed is stored in ``stats["heal_amount"]``.
    """

    pass


@dataclass
class Player:
    """Player mutable state.

    Args:
        health: Current hit points.
        max_health: Upper bound for health.
        attack: Base attack value (modified by equipped weapon).
        defense: Base defense value (modified by equipped armor).
        inventory: List of item IDs carried.
        equipped_weapon: ID of equipped weapon or ``None``.
        equipped_armor: ID of equipped armor or ``None``.

    >>> p = Player(health=20, max_health=20, attack=2, defense=1, inventory=[], equipped_weapon=None, equipped_armor=None)
    >>> p.health
    20
    """

    health: int
    max_health: int
    attack: int
    defense: int
    inventory: List[str] = field(default_factory=list)
    equipped_weapon: Optional[str] = None
    equipped_armor: Optional[str] = None


@dataclass
class Room:
    """Static room definition plus mutable visitation flag.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Long description shown on entry.
        connections: Mapping direction strings (e.g. "north") to target room IDs.
        items: List of item IDs currently in the room.
        npcs: List of NPC IDs present.
        monsters: List of monster IDs present.
        visited: Whether the player has entered this room before.

    >>> r = Room(id="entrance", name="Entrance", description="A hall.", connections={}, items=[], npcs=[], monsters=[], visited=False)
    >>> r.visited
    False
    """

    id: str
    name: str
    description: str
    connections: Dict[str, str] = field(default_factory=dict)
    items: List[str] = field(default_factory=list)
    npcs: List[str] = field(default_factory=list)
    monsters: List[str] = field(default_factory=list)
    visited: bool = False


@dataclass
class NPC:
    """Non‑player character.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Short description.
        dialogue: Ordered list of lines the NPC can say.
        hints: Optional list of hint strings.
        dialogue_index: Index of next line to present.

    >>> npc = NPC(id="old_man", name="Old Man", description="...", dialogue=["Hi"], hints=[], dialogue_index=0)
    >>> npc.dialogue_index
    0
    """

    id: str
    name: str
    description: str
    dialogue: List[str] = field(default_factory=list)
    hints: List[str] = field(default_factory=list)
    dialogue_index: int = 0


@dataclass
class Monster:
    """Creature that can engage in combat.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Text shown when encountered.
        health: Current hit points.
        attack: Base attack value.
        behavior: Simple AI tag (e.g. "aggressive").
        location: Room ID where the monster starts.
        is_boss: Whether this monster is a boss.
        phases: Optional list of phase descriptors for bosses.

    >>> m = Monster(id="goblin", name="Goblin", description="...", health=10, attack=2, behavior="aggressive", location="corridor", is_boss=False)
    >>> m.is_boss
    False
    """

    id: str
    name: str
    description: str
    health: int
    max_health: int = field(init=False)
    attack: int
    behavior: str
    location: str
    is_boss: bool = False
    phases: List[Dict[str, object]] = field(default_factory=list)
    phase: Optional[str] = None

    def __post_init__(self) -> None:
        """Initialize derived attributes after dataclass construction."""
        self.max_health = self.health


@dataclass
class GameState:
    """Canonical mutable game state.

    Attributes:
        player_location: Current room ID of the player.
        player: Player data.
        rooms: Mapping of room_id to Room instances (mutable fields respected).
        npcs: Mapping of npc_id to NPC instances.
        monsters: Mapping of monster_id to Monster instances (mutable health, alive flag, phase).
        turn_counter: Incremented each player action.
        game_over: True when the game has ended.
        victory: True if the player won; False otherwise.
    """

    player_location: str
    player: Player
    rooms: Dict[str, Room] = field(default_factory=dict)
    npcs: Dict[str, NPC] = field(default_factory=dict)
    monsters: Dict[str, Monster] = field(default_factory=dict)
    turn_counter: int = 0
    game_over: bool = False
    victory: bool = False
