"""Mutable runtime representation of the entire game world."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from entities import Player


@dataclass
class RoomState:
    """Per‑room dynamic data.

    Attributes:
        visited: Whether the player has entered this room before.
        items: Item ids currently lying in the room.
        monsters: Monster ids currently present (alive).
        npcs: NPC ids currently present.

    >>> rs = RoomState()
    >>> rs.visited is False
    True
    """

    visited: bool = False
    items: List[str] = field(default_factory=list)
    monsters: List[str] = field(default_factory=list)
    npcs: List[str] = field(default_factory=list)


@dataclass
class NPCState:
    """Tracks dialogue progression for an NPC.

    Attributes:
        next_index: Index of the next line to present from ``NPC.dialogue``.
    """

    next_index: int = 0


@dataclass
class MonsterState:
    """Mutable combat data for a monster instance.

    Attributes:
        health: Current hit points.
        phase: Current phase for multi‑phase bosses (default 1).

    >>> ms = MonsterState()
    >>> ms.health == 0
    True
    """

    health: int = 0
    phase: int = 1


@dataclass
class GameState:
    """Top‑level container for all mutable game information.

    Attributes:
        player: The :class:`Player` object.
        rooms_state: Mapping room_id → :class:`RoomState`.
        npcs_dialogue_progress: Mapping npc_id → next dialogue index.
        monsters_state: Mapping monster_id → :class:`MonsterState`.

    >>> gs = GameState()
    >>> isinstance(gs.player, Player) or gs.player is None
    True
    """

    player: Player = field(default_factory=Player)
    rooms_state: Dict[str, RoomState] = field(default_factory=dict)
    npcs_dialogue_progress: Dict[str, int] = field(default_factory=dict)
    monsters_state: Dict[str, MonsterState] = field(default_factory=dict)

    def get_room_state(self, room_id: str) -> RoomState:
        """Return the mutable state for *room_id*, creating it if absent.

        Parameters
        ----------
        room_id: str
            Identifier of the room whose state is requested.

        Returns
        -------
        RoomState
            The state object associated with the given room.

        This method never raises; it always returns a ``RoomState`` instance.
        """
        if room_id not in self.rooms_state:
            self.rooms_state[room_id] = RoomState()
        return self.rooms_state[room_id]

    def get_npc_next_index(self, npc_id: str) -> int:
        """Return the next dialogue line index for *npc_id*.

        If the NPC has not been encountered before, its progress starts at
        ``0``. The returned value is always a non‑negative integer.

        Parameters
        ----------
        npc_id: str
            Identifier of the NPC.

        Returns
        -------
        int
            Index of the next line to present from ``NPC.dialogue``.
        """
        return self.npcs_dialogue_progress.get(npc_id, 0)

    def advance_npc_dialogue(self, npc_id: str) -> None:
        """Advance the dialogue index for *npc_id* by one.

        If the NPC has not been seen before, it is initialised with an index
        of ``1`` (i.e., the first line has just been shown).

        Parameters
        ----------
        npc_id: str
            Identifier of the NPC whose dialogue progress should advance.
        """
        self.npcs_dialogue_progress[npc_id] = self.get_npc_next_index(npc_id) + 1

    def get_monster_state(self, monster_id: str) -> MonsterState:
        """Return the mutable state for *monster_id*, creating it if absent.

        Parameters
        ----------
        monster_id: str
            Identifier of the monster instance.

        Returns
        -------
        MonsterState
            The state object associated with the given monster.
        """
        if monster_id not in self.monsters_state:
            self.monsters_state[monster_id] = MonsterState()
        return self.monsters_state[monster_id]

    def reset(self) -> None:
        """Reset the entire mutable game state to its initial condition.

        This clears all per‑room, NPC, and monster state while preserving the
        ``player`` object (its own internal reset logic, if any, is not
        invoked here). After calling ``reset()``, the ``GameState`` behaves as
        if it had just been instantiated.
        """
        self.rooms_state.clear()
        self.npcs_dialogue_progress.clear()
        self.monsters_state.clear()

    def __init__(self, world_data: dict | None = None) -> None:
        """
        Initialise a new GameState.

        Parameters
        ----------
        world_data : dict | None, optional
            Optional dictionary containing world configuration data.
            The current implementation does not use this data for state
            construction; it is stored on the instance for potential future
            reference.

        The constructor always creates a fresh ``Player`` instance and assigns
        it to ``self.player``.  Any provided ``world_data`` is saved to
        ``self.world_data`` (or ``None`` if omitted) without affecting the
        player initialisation.
        """
        # Store the optional world data for possible later use.
        self.world_data = world_data if world_data is not None else {}

        # Initialise the core mutable game objects.
        from .player import Player  # Local import to avoid circular dependencies.

        self.player = Player()

        # Containers for dynamic per‑entity state; they start empty and are
        # populated lazily via the accessor methods defined elsewhere in the
        # class.
        self._rooms: dict[str, RoomState] = {}
        self._npcs: dict[str, int] = {}
        self._monsters: dict[str, MonsterState] = {}
