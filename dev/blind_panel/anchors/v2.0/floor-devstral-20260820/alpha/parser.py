from typing import Dict, List, Optional
from models import Direction

class Command:
    def __init__(self, action: str, target: Optional[str] = None, direction: Optional[Direction] = None):
        self.action = action
        self.target = target
        self.direction = direction

def parse_command(input_str: str) -> Command:
    input_str = input_str.strip().lower()
    if not input_str:
        return Command("unknown")

    parts = input_str.split(maxsplit=1)
    action = parts[0]

    # Handle movement commands (north, south, east, west, up, down)
    if action in [Direction.NORTH, Direction.SOUTH, Direction.EAST, Direction.WEST, Direction.UP, Direction.DOWN]:
        return Command("move", direction=action)

    # Handle other commands with optional targets
    target = parts[1] if len(parts) > 1 else None

    # Map common command aliases to standard actions
    command_mapping = {
        "go": "move",
        "walk": "move",
        "run": "move",
        "take": "get",
        "grab": "get",
        "pick": "get",
        "drop": "drop",
        "use": "use",
        "equip": "equip",
        "wear": "equip",
        "attack": "attack",
        "fight": "attack",
        "kill": "attack",
        "talk": "talk",
        "speak": "talk",
        "look": "look",
        "items": "inventory",
        "inventory": "inventory",
        "inv": "inventory",
        "stats": "stats",
        "health": "stats",
        "save": "save",
        "quit": "quit",
        "exit": "quit",
        "help": "help",
    }

    # Default to the original action if not mapped
    standard_action = command_mapping.get(action, action)

    return Command(standard_action, target=target)
