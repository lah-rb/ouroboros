from models import Command, CommandType


def parse_command(raw: str) -> Command:
    raw = raw.strip().lower()
    if not raw:
        return Command(type=CommandType.HELP)
    parts = raw.split()
    verb = parts[0]
    # Movement shortcuts
    if verb in ("north", "south", "east", "west"):
        return Command(type=CommandType.MOVE, args={"direction": verb})
    if verb in ("n", "s", "e", "w"):
        mapping = {"n": "north", "s": "south", "e": "east", "w": "west"}
        return Command(type=CommandType.MOVE, args={"direction": mapping[verb]})
    # Compound commands
    if verb == "go":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        direction = parts[1]
        if direction in ("n", "north"):
            direction = "north"
        if direction in ("s", "south"):
            direction = "south"
        if direction in ("e", "east"):
            direction = "east"
        if direction in ("w", "west"):
            direction = "west"
        return Command(type=CommandType.MOVE, args={"direction": direction})
    if verb == "take":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        return Command(type=CommandType.TAKE, target=parts[1])
    if verb == "drop":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        return Command(type=CommandType.DROP, target=parts[1])
    if verb == "use":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        return Command(type=CommandType.USE, target=parts[1])
    if verb == "examine":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        return Command(type=CommandType.EXAMINE, target=parts[1])
    if verb == "talk":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        if parts[1] == "to" and len(parts) > 2:
            target = parts[2]
        else:
            target = " ".join(parts[1:])
        return Command(type=CommandType.TALK, target=target)
    if verb == "attack":
        if len(parts) < 2:
            return Command(type=CommandType.HELP)
        return Command(type=CommandType.ATTACK, target=parts[1])
    if verb == "flee":
        return Command(type=CommandType.FLEE)
    if verb == "look":
        return Command(type=CommandType.LOOK)
    if verb == "status":
        return Command(type=CommandType.STATUS)
    if verb == "inventory":
        return Command(type=CommandType.INVENTORY)
    if verb == "help":
        return Command(type=CommandType.HELP)
    if verb == "quit":
        return Command(type=CommandType.QUIT)
    if verb == "save":
        return Command(type=CommandType.SAVE)
    if verb == "load":
        return Command(type=CommandType.LOAD)
    return Command(type=CommandType.HELP)
