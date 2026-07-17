"""
Parses raw player input into a structured Command object.
"""

from dataclasses import dataclass
from typing import List


@dataclass
class Command:
    name: str  # e.g., "go", "take", "attack"
    args: List[str]  # remaining tokens


def parse_command(raw: str) -> Command:
    """
    Very permissive parser: splits on whitespace, lower‑cases the command name.
    Returns a Command with name and list of arguments (still raw strings).
    """
    tokens = raw.strip().split()
    if not tokens:
        return Command(name="empty", args=[])
    name = tokens[0].lower()
    args = tokens[1:]
    return Command(name=name, args=args)
