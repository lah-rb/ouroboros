"""Core data classes for the adventure game."""

from __future__ import annotations
from typing import List, Dict, Optional


class Player:
    """Represents the player character and their mutable stats.

    Args:
        location: Current room identifier.
        health: Current hit points (must be ≤ max_health).
        max_health: Maximum hit points.
        attack: Base attack value (modified by equipped weapon).
        defense: Base defense value (modified by equipped armor).
        inventory: List of item identifiers carried.
        equipped_weapon: Identifier of the equipped weapon, or None.
        equipped_armor: Identifier of the equipped armor, or None.

    >>> p = Player(location="entrance", health=10, max_health=10,
    ...            attack=2, defense=1, inventory=[], equipped_weapon=None,
    ...            equipped_armor=None)
    >>> p.health
    10
    """

    def __init__(
        self,
        location: str,
        health: int,
        max_health: int,
        attack: int,
        defense: int,
        inventory: List[str],
        equipped_weapon: Optional[str] = None,
        equipped_armor: Optional[str] = None,
    ) -> None:
        if health > max_health:
            raise ValueError("health must be less than or equal to max_health")
        self.location = location
        self.health = health
        self.max_health = max_health
        self.attack = attack
        self.defense = defense
        self.inventory = list(inventory)
        self.equipped_weapon = equipped_weapon
        self.equipped_armor = equipped_armor


class Room:
    """Static definition of a location in the world.

    Args:
        id: Unique room identifier.
        name: Human‑readable name.
        description: Long description shown on look.
        connections: Mapping direction → destination room id.
        items: List of item identifiers present initially.
        npcs: List of NPC identifiers present initially.
        monsters: List of monster identifiers present initially.

    >>> r = Room(id="entrance", name="Entrance Hall",
    ...          description="A dim hall.", connections={"north":"corridor"},
    ...          items=[], npcs=[], monsters=[])
    >>> r.connections["north"]
    'corridor'
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        connections: Dict[str, str],
        items: List[str],
        npcs: List[str],
        monsters: List[str],
    ) -> None:
        # Store provided values. Use shallow copies for mutable containers
        # to prevent accidental external mutation of the internal state.
        self.id = id
        self.name = name
        self.description = description
        self.connections = dict(connections)
        self.items = list(items)
        self.npcs = list(npcs)
        self.monsters = list(monsters)


class Item:
    """Base class for all items.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Text shown when examined.
        type: One of "weapon", "armor", "healing", or "generic".
        attack_bonus: Added to player.attack if equipped as weapon.
        defense_bonus: Added to player.defense if equipped as armor.
        heal_amount: Restored health when used if healing item.

    >>> i = Item(id="sword", name="Rusty Sword",
    ...          description="A dull blade.", type="weapon",
    ...          attack_bonus=2, defense_bonus=0, heal_amount=0)
    >>> i.type
    'weapon'
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        type: str,
        attack_bonus: int,
        defense_bonus: int,
        heal_amount: int,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.type = type
        self.attack_bonus = attack_bonus
        self.defense_bonus = defense_bonus
        self.heal_amount = heal_amount


class Weapon:
    """Weapon item that can be equipped.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Flavor text.
        attack_bonus: Bonus added to player.attack when equipped.

    >>> w = Weapon(id="sword", name="Sword",
    ...            description="Sharp.", attack_bonus=3)
    >>> w.attack_bonus
    3
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        attack_bonus: int,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.attack_bonus = attack_bonus


class Armor:
    """Armor item that can be equipped.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Flavor text.
        defense_bonus: Bonus added to player.defense when equipped.

    >>> a = Armor(id="shield", name="Shield",
    ...            description="Sturdy.", defense_bonus=2)
    >>> a.defense_bonus
    2
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        defense_bonus: int,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.defense_bonus = defense_bonus


class HealingItem:
    """Consumable item that restores health.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Flavor text.
        heal_amount: Amount of HP restored when used.

    >>> h = HealingItem(id="potion", name="Potion",
    ...                  description="Heals.", heal_amount=5)
    >>> h.heal_amount
    5
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        heal_amount: int,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.heal_amount = heal_amount


class NPC:
    """Non‑player character providing dialogue.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Short description shown in room.
        dialogue: Ordered list of lines the NPC will say.

    >>> n = NPC(id="old_man", name="Old Man",
    ...         description="A frail old man.", dialogue=["Hi"])
    >>> n.dialogue[0]
    'Hi'
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        dialogue: List[str],
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.dialogue = dialogue


class Monster:
    """Standard hostile creature.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Flavor text.
        health: Current hit points.
        attack: Damage dealt per strike.
        behavior: Simple AI tag (e.g., "aggressive").
        location: Room id where the monster starts.

    >>> m = Monster(id="goblin", name="Goblin",
    ...             description="Small.", health=10, attack=2,
    ...             behavior="aggressive", location="corridor")
    >>> m.health
    10
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        health: int,
        attack: int,
        behavior: str,
        location: str,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.health = health
        self.attack = attack
        self.behavior = behavior
        self.location = location


class Boss:
    """Final adversary with two combat phases.

    Args:
        id: Unique identifier.
        name: Display name.
        description: Flavor text.
        health_phase1: Hit points for first phase.
        health_phase2: Hit points for second phase (after transition).
        attack_phase1: Damage per strike in first phase.
        attack_phase2: Damage per strike in second phase.
        weakness_item_id: Item identifier that can trigger phase transition or extra damage.
        location: Starting room id.

    >>> b = Boss(id="lich", name="Lich King",
    ...          description="Ancient.", health_phase1=40,
    ...          health_phase2=30, attack_phase1=6,
    ...          attack_phase2=8, weakness_item_id="amulet",
    ...          location="throne_room")
    >>> b.health_phase1
    40
    """

    def __init__(
        self,
        id: str,
        name: str,
        description: str,
        health_phase1: int,
        health_phase2: int,
        attack_phase1: int,
        attack_phase2: int,
        weakness_item_id: str,
        location: str,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.health_phase1 = health_phase1
        self.health_phase2 = health_phase2
        self.attack_phase1 = attack_phase1
        self.attack_phase2 = attack_phase2
        self.weakness_item_id = weakness_item_id
        self.location = location
