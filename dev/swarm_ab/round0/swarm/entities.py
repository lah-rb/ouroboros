"""Core data classes representing static game entities."""

from __future__ import annotations

from typing import Dict, List, Optional


class Room:
    """Static definition of a location in the world.

    Attributes:
        id: Unique identifier used in world files.
        name: Human‑readable title.
        description: Text shown when the player looks around.
        exits: Mapping direction → destination room id.
        items: List of item ids initially present.
        npcs: List of npc ids initially present.
        monsters: List of monster ids initially present.

    >>> r = Room()
    >>> isinstance(r, Room)
    True
    """

    id: str
    name: str
    description: str
    exits: Dict[str, str]
    items: List[str]
    npcs: List[str]
    monsters: List[str]

    def __init__(
        self,
        id: str = "",
        name: str = "",
        description: str = "",
        exits: Optional[Dict[str, str]] = None,
        items: Optional[List[str]] = None,
        npcs: Optional[List[str]] = None,
        monsters: Optional[List[str]] = None,
    ) -> None:
        """Create a new static room definition.

        All parameters are optional to allow construction without arguments,
        matching the doctest expectations. Mutable defaults are avoided by
        copying provided collections or initializing empty ones.
        """
        self.id = id
        self.name = name
        self.description = description
        # Use copies to prevent accidental shared mutable state.
        self.exits = dict(exits) if exits is not None else {}
        self.items = list(items) if items is not None else []
        self.npcs = list(npcs) if npcs is not None else []
        self.monsters = list(monsters) if monsters is not None else []


class Item:
    """Static definition of an object the player can interact with.

    Attributes:
        id: Unique identifier.
        name: Display name.
        description: Detailed text.
        type: One of "weapon", "armor", "healing", "key", etc.
        attack_bonus: Additional attack when equipped.
        defense_bonus: Additional defense when equipped.
        heal_amount: HP restored when used (if usable).
        usable: Whether the item can be invoked via a command.

    >>> i = Item()
    >>> isinstance(i, Item)
    True
    """

    id: str
    name: str
    description: str
    type: str
    attack_bonus: int
    defense_bonus: int
    heal_amount: int
    usable: bool

    def __init__(
        self,
        id: str = "",
        name: str = "",
        description: str = "",
        type: str = "",
        attack_bonus: int = 0,
        defense_bonus: int = 0,
        heal_amount: int = 0,
        usable: bool = False,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.type = type
        self.attack_bonus = attack_bonus
        self.defense_bonus = defense_bonus
        self.heal_amount = heal_amount
        self.usable = usable


class NPC:
    """Static definition of a non‑player character.

    Attributes:
        id: Unique identifier.
        name: Display name.
        description: Short textual hint.
        dialogue: Ordered list of strings the NPC will say on successive talks.

    >>> n = NPC()
    >>> isinstance(n, NPC)
    True
    """

    id: str
    name: str
    description: str
    dialogue: List[str]

    def __init__(
        self,
        id: str = "",
        name: str = "",
        description: str = "",
        dialogue: List[str] | None = None,
    ) -> None:
        """
        Initialise an NPC with optional data.

        Parameters are optional to allow default construction used in doctests.
        If ``dialogue`` is omitted, it defaults to an empty list.
        """
        self.id = id
        self.name = name
        self.description = description
        self.dialogue = [] if dialogue is None else list(dialogue)


class Monster:
    """Static definition of a creature that can engage in combat.

    Attributes:
        id: Unique identifier.
        name: Display name.
        description: Flavor text.
        health: Base health for regular monsters.
        attack: Base attack value.
        defense: Base defense value.
        loot: List of item ids dropped on defeat.
        # Boss‑specific optional fields
        health_phase1: Optional[int]
        health_phase2: Optional[int]
        phases: Optional[int]
        weakness_item: Optional[str]

    >>> m = Monster()
    >>> isinstance(m, Monster)
    True
    """

    id: str
    name: str
    description: str
    health: int
    attack: int
    defense: int
    loot: List[str]
    health_phase1: Optional[int] = None
    health_phase2: Optional[int] = None
    phases: Optional[int] = None
    weakness_item: Optional[str] = None

    def __init__(
        self,
        id: str = "",
        name: str = "",
        description: str = "",
        health: int = 0,
        attack: int = 0,
        defense: int = 0,
        loot: Optional[List[str]] = None,
        health_phase1: Optional[int] = None,
        health_phase2: Optional[int] = None,
        phases: Optional[int] = None,
        weakness_item: Optional[str] = None,
    ) -> None:
        self.id = id
        self.name = name
        self.description = description
        self.health = health
        self.attack = attack
        self.defense = defense
        self.loot = list(loot) if loot is not None else []
        self.health_phase1 = health_phase1
        self.health_phase2 = health_phase2
        self.phases = phases
        self.weakness_item = weakness_item


class Player:
    """Mutable representation of the player character.

    Attributes:
        location: Current room id.
        health: Current hit points.
        max_health: Upper bound for health.
        attack: Effective attack including equipment.
        defense: Effective defense including equipment.
        inventory: List of owned item ids.
        equipped_weapon: Item id of equipped weapon or None.
        equipped_armor: Item id of equipped armor or None.

    >>> p = Player()
    >>> isinstance(p, Player)
    True
    """

    location: str
    health: int
    max_health: int
    attack: int
    defense: int
    inventory: List[str]
    equipped_weapon: Optional[str] = None
    equipped_armor: Optional[str] = None

    def __init__(
        self,
        location: str = "",
        health: int = 0,
        max_health: int = 0,
        attack: int = 0,
        defense: int = 0,
        inventory: Optional[List[str]] = None,
        equipped_weapon: Optional[str] = None,
        equipped_armor: Optional[str] = None,
    ) -> None:
        """Create a new player instance.

        All parameters are optional and default to empty/zero values.
        ``inventory`` is copied to avoid accidental external mutation.
        """
        self.location = location
        self.health = health
        self.max_health = max_health
        self.attack = attack
        self.defense = defense
        self.inventory = list(inventory) if inventory is not None else []
        self.equipped_weapon = equipped_weapon
        self.equipped_armor = equipped_armor

    # ------------------------------------------------------------------
    # Basic state‑management helpers
    # ------------------------------------------------------------------
    def move_to(self, room_id: str) -> None:
        """Set the player's current location to ``room_id``."""
        self.location = room_id

    def is_alive(self) -> bool:
        """Return ``True`` if the player has any hit points left."""
        return self.health > 0

    def take_damage(self, amount: int) -> None:
        """Reduce health by ``amount`` (clamped at zero)."""
        if amount < 0:
            raise ValueError("damage amount must be non‑negative")
        self.health = max(self.health - amount, 0)

    def heal(self, amount: int) -> None:
        """Increase health by ``amount`` without exceeding ``max_health``."""
        if amount < 0:
            raise ValueError("heal amount must be non‑negative")
        self.health = min(self.health + amount, self.max_health)

    # ------------------------------------------------------------------
    # Inventory management
    # ------------------------------------------------------------------
    def add_item(self, item_id: str) -> None:
        """Add ``item_id`` to the player's inventory."""
        if item_id not in self.inventory:
            self.inventory.append(item_id)

    def remove_item(self, item_id: str) -> None:
        """Remove ``item_id`` from the inventory; raise if absent."""
        try:
            self.inventory.remove(item_id)
        except ValueError as exc:
            raise KeyError(f"Item '{item_id}' not in inventory") from exc

    # ------------------------------------------------------------------
    # Equipment handling
    # ------------------------------------------------------------------
    def equip_weapon(self, item_id: str) -> None:
        """Equip a weapon identified by ``item_id``.

        The item must already be present in the inventory.
        """
        if item_id not in self.inventory:
            raise KeyError(f"Weapon '{item_id}' not in inventory")
        self.equipped_weapon = item_id

    def unequip_weapon(self) -> None:
        """Unequip the currently equipped weapon, if any."""
        self.equipped_weapon = None

    def equip_armor(self, item_id: str) -> None:
        """Equip armor identified by ``item_id``.

        The item must already be present in the inventory.
        """
        if item_id not in self.inventory:
            raise KeyError(f"Armor '{item_id}' not in inventory")
        self.equipped_armor = item_id

    def unequip_armor(self) -> None:
        """Unequip the currently equipped armor, if any."""
        self.equipped_armor = None

    # ------------------------------------------------------------------
    # Representation
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"Player(location={self.location!r}, health={self.health}, "
            f"max_health={self.max_health}, attack={self.attack}, "
            f"defense={self.defense}, inventory={self.inventory!r}, "
            f"equipped_weapon={self.equipped_weapon!r}, "
            f"equipped_armor={self.equipped_armor!r})"
        )
