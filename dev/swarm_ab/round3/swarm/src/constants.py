"""Enumerations and constant values shared across the engine."""

from __future__ import annotations
from enum import Enum, auto


class Direction(Enum):
    """Cardinal directions used for room navigation."""

    NORTH = auto()
    SOUTH = auto()
    EAST = auto()
    WEST = auto()


class CommandType(Enum):
    """Supported player command categories."""

    MOVE = auto()
    TAKE = auto()
    DROP = auto()
    USE = auto()
    EXAMINE = auto()
    TALK = auto()
    ATTACK = auto()
    FLEE = auto()
    LOOK = auto()
    STATUS = auto()
    HELP = auto()
    QUIT = auto()
    SAVE = auto()
    LOAD = auto()
