"""Command parser for the text adventure game engine."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class CommandType(Enum):
    """Types of commands supported by the game."""
    MOVE = auto()
    INVENTORY_MANAGE = auto()
    NPC_INTERACT = auto()
    LOOK = auto()
    HELP = auto()
    QUIT = auto()
    UNKNOWN = auto()


@dataclass
class Command:
    """Represents a parsed command."""
    type: CommandType
    action: Optional[str] = None  # e.g., "north", "take", "talk"
    target: Optional[str] = None  # e.g., item name, NPC name


def parse_command(raw: str) -> Command:
    """
    Parse a raw command string into a structured Command.
    
    Args:
        raw: The raw command input from the player.
        
    Returns:
        A Command object with type and optional action/target.
    """
    # Normalize input
    normalized = raw.strip().lower()
    if not normalized:
        return Command(type=CommandType.UNKNOWN)
    
    parts = normalized.split()
    verb = parts[0]
    
    # Movement commands
    if verb in ("go", "move", "walk"):
        if len(parts) >= 2:
            direction = parts[1]
            if direction in ("north", "south", "east", "west"):
                return Command(type=CommandType.MOVE, action="go", target=direction)
        return Command(type=CommandType.UNKNOWN)
    
    # Direct movement (just direction)
    if verb in ("north", "south", "east", "west"):
        return Command(type=CommandType.MOVE, action="go", target=verb)
    
    # Inventory management
    if verb in ("take", "grab", "pick", "pickup"):
        item = " ".join(parts[1:]) if len(parts) > 1 else None
        return Command(type=CommandType.INVENTORY_MANAGE, action="take", target=item)
    
    if verb in ("drop",):
        item = " ".join(parts[1:]) if len(parts) > 1 else None
        return Command(type=CommandType.INVENTORY_MANAGE, action="drop", target=item)
    
    if verb in ("use",):
        item = " ".join(parts[1:]) if len(parts) > 1 else None
        return Command(type=CommandType.INVENTORY_MANAGE, action="use", target=item)
    
    if verb in ("examine", "inspect", "look"):
        target = " ".join(parts[1:]) if len(parts) > 1 else None
        return Command(type=CommandType.INVENTORY_MANAGE, action="examine", target=target)
    
    # NPC interaction
    if verb in ("talk", "speak", "chat"):
        npc = " ".join(parts[1:]) if len(parts) > 1 else None
        return Command(type=CommandType.NPC_INTERACT, action="talk", target=npc)
    
    # Static commands
    if verb in ("look",):
        return Command(type=CommandType.LOOK, action="look")
    
    if verb in ("help", "?"):
        return Command(type=CommandType.HELP, action="help")
    
    if verb in ("quit", "q", "exit"):
        return Command(type=CommandType.QUIT, action="quit")
    
    # Default to unknown
    return Command(type=CommandType.UNKNOWN)
