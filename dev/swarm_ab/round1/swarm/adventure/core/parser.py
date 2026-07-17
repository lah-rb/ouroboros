"""Command‑parsing utilities turning raw input into structured commands."""

from dataclasses import dataclass
from typing import List


@dataclass
class Command:
    """Structured representation of a player command.

    Args:
        name: Canonical verb (e.g., "go", "take", "attack").
        args: List of argument strings following the verb.

    >>> cmd = Command(name="go", args=["north"])
    >>> cmd.name
    'go'
    """

    name: str
    args: List[str]


def parse_command(raw_input: str) -> Command:
    """Parse user input into a Command object.

    Supported verbs and their expected arguments:
        go <direction>          – move player (north/south/east/west)
        take <item_id>          – pick up an item
        drop <item_id>           – leave an item in the current room
        use <item_id>           – consume or equip an item
        examine <target>        – look at an item, npc, or monster
        talk <npc_id>           – initiate dialogue with an NPC
        attack <monster_id>     – start combat with a monster
        flee                    – attempt to escape combat
        look                    – re‑describe the current room
        status                  – show player health/inventory
        help                    – list available commands
        quit                    – exit the game

    Args:
        raw_input: The exact line entered by the player.

    Returns:
        A Command instance with normalized verb and argument list.

    Raises:
        ValueError: If the input is empty or contains an unknown verb.

    >>> parse_command("go north")
    Command(name='go', args=['north'])
    >>> parse_command("help")
    Command(name='help', args=[])
    """
    # Remove surrounding whitespace
    stripped = raw_input.strip()
    if not stripped:
        raise ValueError("Input command is empty")

    # Split the input into verb and arguments
    parts = stripped.split()
    verb = parts[0].lower()

    # Set of all supported verbs
    supported_verbs = {
        "go",
        "take",
        "drop",
        "use",
        "examine",
        "talk",
        "attack",
        "flee",
        "look",
        "status",
        "help",
        "quit",
    }

    if verb not in supported_verbs:
        raise ValueError(f"Unknown command verb: {verb}")

    args = parts[1:]
    return Command(name=verb, args=args)


__doc__ = "Command-parsing utilities turning raw input into structured commands."
