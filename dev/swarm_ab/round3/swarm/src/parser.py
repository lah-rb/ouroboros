"""Parse raw player input into structured Command objects."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, List
from src.constants import Direction, CommandType

@dataclass
class Command:
    """A parsed player instruction.

    Attributes:
        type: The command category.
        target: Primary argument (e.g., direction name, item id, npc id).
        extra: Additional free‑form arguments if needed.
    """
    type: CommandType
    target: Optional[str] = None
    extra: Optional[List[str]] = None

def parse_command(raw: str) -> Command:
    """Convert a raw input line into a Command.

    The parser recognises the vocabulary defined in the engine contract.
    It is case‑insensitive and trims surrounding whitespace.

    Args:
        raw: User input string.

    Returns:
        A Command instance describing the requested action.

    Raises:
        ValueError: If the input cannot be mapped to a known command.

    >>> parse_command("go north")
    Command(type=<CommandType.MOVE: 1>, target='north', extra=None)
    >>> parse_command("take sword")
    Command(type=<CommandType.TAKE: 2>, target='sword', extra=None)
    """
    # Normalise input
    if raw is None:
        raise ValueError("Input cannot be None")
    stripped = raw.strip()
    if not stripped:
        raise ValueError("Empty command")

    tokens = stripped.split()
    cmd_word = tokens[0].lower()

    # Mapping of command words (including common synonyms) to CommandType members
    command_map = {
        # movement
        "go": CommandType.MOVE,
        "move": CommandType.MOVE,
        # item manipulation
        "take": CommandType.TAKE,
        "get": CommandType.TAKE,
        "pick": CommandType.TAKE,
        "drop": CommandType.DROP,
        "discard": CommandType.DROP,
        "use": CommandType.USE,
        # inspection / interaction
        "examine": CommandType.EXAMINE,
        "inspect": CommandType.EXAMINE,
        "lookat": CommandType.EXAMINE,
        "talk": CommandType.TALK,
        "speak": CommandType.TALK,
        # combat
        "attack": CommandType.ATTACK,
        "hit": CommandType.ATTACK,
        "strike": CommandType.ATTACK,
        "flee": CommandType.FLEE,
        "run": CommandType.FLEE,
        # utility / meta commands
        "look": CommandType.LOOK,
        "l": CommandType.LOOK,
        "status": CommandType.STATUS,
        "stats": CommandType.STATUS,
        "help": CommandType.HELP,
        "?": CommandType.HELP,
        "save": CommandType.SAVE,
        "load": CommandType.LOAD,
        "quit": CommandType.QUIT,
        "exit": CommandType.QUIT,
    }

    if cmd_word not in command_map:
        raise ValueError(f"Unknown command: {raw}")

    cmd_type = command_map[cmd_word]

    # Determine which commands require a primary target argument
    requires_target = {
        CommandType.MOVE,
        CommandType.TAKE,
        CommandType.DROP,
        CommandType.USE,
        CommandType.EXAMINE,
        CommandType.TALK,
        CommandType.ATTACK,
        CommandType.SAVE,
        CommandType.LOAD,
    }

    target: Optional[str] = None
    extra: Optional[List[str]] = None

    if cmd_type in requires_target:
        if len(tokens) < 2:
            raise ValueError(f"Command '{cmd_word}' requires a target argument")
        target = tokens[1]
        if len(tokens) > 2:
            extra = tokens[2:]
    else:
        # Commands that do not need a primary target (e.g., look, status, help, quit, flee)
        if len(tokens) > 1:
            extra = tokens[1:]

    return Command(type=cmd_type, target=target, extra=extra if extra else None)
