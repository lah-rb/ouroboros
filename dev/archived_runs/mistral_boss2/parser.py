from enum import Enum, auto
from typing import Optional
from models import Direction


class CommandType(Enum):
    MOVE = auto()
    TAKE = auto()
    DROP = auto()
    USE = auto()
    EXAMINE = auto()
    TALK = auto()
    ATTACK = auto()
    FLEE = auto()
    LOOK = auto()
    INVENTORY = auto()
    STATUS = auto()
    HELP = auto()
    QUIT = auto()
    SAVE = auto()
    LOAD = auto()
    EQUIP = auto()


class Command:
    def __init__(
        self,
        command_type: CommandType,
        direction: Optional[Direction] = None,
        item_id: Optional[str] = None,
        npc_id: Optional[str] = None,
        monster_id: Optional[str] = None,
    ):
        self.type = command_type
        self.direction = direction
        self.item_id = item_id
        self.npc_id = npc_id
        self.monster_id = monster_id


def parse_input(raw: str) -> Command:
    raw = raw.strip().lower()
    if not raw:
        return Command(CommandType.HELP)

    tokens = raw.split()
    verb = tokens[0]

    direction_map = {
        "north": Direction.NORTH,
        "south": Direction.SOUTH,
        "east": Direction.EAST,
        "west": Direction.WEST,
        "n": Direction.NORTH,
        "s": Direction.SOUTH,
        "e": Direction.EAST,
        "w": Direction.WEST,
        "up": Direction.UP,
        "u": Direction.UP,
        "down": Direction.DOWN,
        "d": Direction.DOWN,
    }

    # Movement commands
    if verb in ("go", "move"):
        if len(tokens) < 2:
            return Command(CommandType.HELP)
        direction_str = tokens[1]
        if direction_str in direction_map:
            return Command(CommandType.MOVE, direction=direction_map[direction_str])
        return Command(CommandType.HELP)

    # Shortcut movement (just direction)
    if verb in direction_map:
        return Command(CommandType.MOVE, direction=direction_map[verb])

    # Inventory commands
    if verb == "take":
        if len(tokens) < 2:
            return Command(CommandType.HELP)
        item_name = " ".join(tokens[1:])
        return Command(CommandType.TAKE, item_id=item_name)

    if verb == "drop":
        if len(tokens) < 2:
            return Command(CommandType.HELP)
        item_name = " ".join(tokens[1:])
        return Command(CommandType.DROP, item_id=item_name)

    if verb == "use":
        if len(tokens) < 2:
            return Command(CommandType.HELP)
        item_name = " ".join(tokens[1:])
        return Command(CommandType.USE, item_id=item_name)

    if verb == "examine":
        if len(tokens) < 2:
            return Command(CommandType.HELP)
        target = " ".join(tokens[1:])
        # Could be item or NPC - we'll resolve in engine
        return Command(CommandType.EXAMINE, item_id=target)

    if verb in ("talk", "speak"):
        if len(tokens) < 2:
            return Command(CommandType.HELP)
        # Support both "talk to npc" and "talk npc"
        if tokens[1] == "to":
            if len(tokens) < 3:
                return Command(CommandType.HELP)
            npc_name = " ".join(tokens[2:])
        else:
            npc_name = " ".join(tokens[1:])
        return Command(CommandType.TALK, npc_id=npc_name)

    if verb == "equip":
        if len(tokens) < 3:
            return Command(CommandType.HELP)
        item_name = " ".join(tokens[2:])
        return Command(CommandType.EQUIP, item_id=item_name)

    if verb == "attack":
        if len(tokens) < 2:
            return Command(CommandType.ATTACK)  # Attack whatever is in the room
        target = " ".join(tokens[1:])
        return Command(CommandType.ATTACK, monster_id=target)

    if verb == "flee":
        return Command(CommandType.FLEE)

    if verb in ("look", "l"):
        return Command(CommandType.LOOK)

    if verb in ("inventory", "inv", "i"):
        return Command(CommandType.INVENTORY)

    if verb in ("status", "stats"):
        return Command(CommandType.STATUS)

    if verb in ("help", "h", "?"):
        return Command(CommandType.HELP)

    if verb in ("quit", "exit", "q"):
        return Command(CommandType.QUIT)

    if verb == "save":
        return Command(CommandType.SAVE)

    if verb == "load":
        return Command(CommandType.LOAD)

    return Command(CommandType.HELP)
