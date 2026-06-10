from typing import Optional, Set
from models import Command

# Valid verbs in the game
VALID_VERBS: Set[str] = {
    "look",
    "examine",
    "inspect",
    "check",
    "go",
    "move",
    "walk",
    "run",
    "travel",
    "take",
    "grab",
    "pick",
    "pickup",
    "collect",
    "drop",
    "discard",
    "leave",
    "use",
    "utilize",
    "inventory",
    "i",
    "inv",
    "help",
    "h",
    "quit",
    "q",
    "exit",
    "north",
    "south",
    "east",
    "west",
    "n",
    "s",
    "e",
    "w",
    "northwest",
    "northeast",
    "southwest",
    "southeast",
    "nw",
    "ne",
    "sw",
    "se",
    "up",
    "down",
    "u",
    "d",
    "open",
    "close",
    "talk",
    "speak",
    "say",
    "attack",
    "fight",
    "hit",
    "steal",
    "thieve",
}


def parse_command(raw: str) -> Command:
    """
    Parse a raw player command into a structured Command object.

    Args:
        raw: The raw input string from the player

    Returns:
        A Command object with verb, noun, and validation status
    """
    # Clean up the input
    raw = raw.strip()

    # Handle empty input
    if not raw:
        return Command(
            verb="",
            noun=None,
            raw=raw,
            valid=False,
            error_message="Empty command. Please enter a command.",
        )

    # Split into words
    words = raw.lower().split()

    # Extract verb (first word)
    verb = words[0] if words else ""

    # Extract noun (everything after the first word)
    noun = " ".join(words[1:]) if len(words) > 1 else None

    # Normalize direction aliases
    verb = _normalize_direction(verb)

    # Validate the command
    return validate_command(verb, noun, raw)


def _normalize_direction(verb: str) -> str:
    """Normalize direction aliases to standard directions."""
    direction_map = {
        "n": "north",
        "s": "south",
        "e": "east",
        "w": "west",
        "ne": "northeast",
        "nw": "northwest",
        "se": "southeast",
        "sw": "southwest",
        "u": "up",
        "d": "down",
        "i": "inventory",
        "inv": "inventory",
        "h": "help",
        "q": "quit",
        "quit": "quit",
        "exit": "quit",
    }
    return direction_map.get(verb, verb)


def validate_command(verb: str, noun: Optional[str], raw: str) -> Command:
    """
    Validate a parsed command and return a Command object.

    Args:
        verb: The action verb from the command
        noun: The object of the action (if any)
        raw: The original raw input string

    Returns:
        A Command object with validation status
    """
    # Check if verb is valid
    if verb not in VALID_VERBS:
        return Command(
            verb=verb,
            noun=noun,
            raw=raw,
            valid=False,
            error_message=f"Unknown command: '{verb}'. Type 'help' for available commands.",
        )

    # Special validation for commands that require a noun
    if _requires_noun(verb) and not noun:
        return Command(
            verb=verb,
            noun=noun,
            raw=raw,
            valid=False,
            error_message=f"'{verb}' requires a noun. Example: '{verb} [object]'.",
        )

    # Special validation for commands that don't accept a noun
    if _no_noun_allowed(verb) and noun:
        return Command(
            verb=verb,
            noun=noun,
            raw=raw,
            valid=False,
            error_message=f"'{verb}' does not take a noun.",
        )

    # Special validation for direction commands
    if _is_direction(verb) and noun:
        return Command(
            verb=verb,
            noun=noun,
            raw=raw,
            valid=False,
            error_message=f"Directions don't take nouns. Use '{verb}' alone.",
        )

    # All validations passed
    return Command(verb=verb, noun=noun, raw=raw, valid=True)


def _requires_noun(verb: str) -> bool:
    """Check if the verb requires a noun."""
    requires_noun_verbs = {
        "take",
        "grab",
        "pick",
        "pickup",
        "collect",
        "drop",
        "discard",
        "leave",
        "use",
        "utilize",
        "examine",
        "inspect",
        "check",
        "go",
        "move",
        "walk",
        "run",
        "travel",
        "open",
        "close",
        "attack",
        "fight",
        "hit",
        "steal",
        "thieve",
    }
    return verb in requires_noun_verbs


def _no_noun_allowed(verb: str) -> bool:
    """Check if the verb does not accept a noun."""
    no_noun_verbs = {"inventory", "i", "inv", "help", "h", "quit", "q", "exit"}
    return verb in no_noun_verbs


def _is_direction(verb: str) -> bool:
    """Check if the verb is a direction command."""
    directions = {
        "north",
        "south",
        "east",
        "west",
        "n",
        "s",
        "e",
        "w",
        "northwest",
        "northeast",
        "southwest",
        "southeast",
        "nw",
        "ne",
        "sw",
        "se",
        "up",
        "down",
        "u",
        "d",
    }
    return verb in directions
