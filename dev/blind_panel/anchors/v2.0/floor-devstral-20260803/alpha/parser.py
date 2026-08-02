from typing import NamedTuple, Optional


class Command(NamedTuple):
    action: str
    target: Optional[str] = None


def parse_command(raw: str) -> Command:
    parts = raw.strip().lower().split()
    if len(parts) >= 2 and parts[0] == "talk" and parts[1] == "to":
        action = "talk to"
        target = " ".join(parts[2:]) if len(parts) > 2 else None
    else:
        action = parts[0] if parts else None
        target = " ".join(parts[1:]) if len(parts) > 1 else None
    return Command(action, target)
