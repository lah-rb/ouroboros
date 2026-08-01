"""Command parsing.

Turns a raw line of player input into a Command(verb, args_text, raw)
where `verb` is one of the engine's canonical command names (see
engine.GameEngine.dispatch) and `args_text` is whatever's left over --
already de-prefixed of small words like "to" so handlers don't have to
deal with them.

This module is deliberately forgiving: lots of synonyms map to the same
verb, and a bare direction word ("north", "n") is understood as movement
without needing "go" in front of it.
"""

DIRECTION_ALIASES = {
    "n": "north", "north": "north",
    "s": "south", "south": "south",
    "e": "east", "east": "east",
    "w": "west", "west": "west",
    "u": "up", "up": "up",
    "d": "down", "down": "down",
}
BARE_DIRECTIONS = set(DIRECTION_ALIASES.keys())

VERB_ALIASES = {
    "go": "go", "move": "go", "walk": "go",
    "take": "take", "get": "take", "grab": "take", "pick": "take",
    "drop": "drop", "discard": "drop",
    "use": "use", "drink": "use", "eat": "use",
    "examine": "examine", "inspect": "examine", "x": "examine",
    "look": "look", "l": "look",
    "equip": "equip", "wear": "equip", "wield": "equip",
    "unequip": "unequip", "remove": "unequip",
    "talk": "talk", "speak": "talk",
    "ask": "ask",
    "attack": "attack", "fight": "attack", "hit": "attack",
    "flee": "flee", "run": "flee", "escape": "flee",
    "inventory": "inventory", "inv": "inventory", "i": "inventory",
    "status": "status", "stats": "status", "st": "status",
    "help": "help", "?": "help",
    "quit": "quit", "exit": "quit", "q": "quit",
    "save": "save",
    "load": "load",
    "map": "map", "exits": "map",
}


class Command:
    def __init__(self, verb, args_text, raw):
        self.verb = verb
        self.args_text = args_text
        self.raw = raw


def parse(raw_input):
    text = (raw_input or "").strip().lower()
    if not text:
        return Command("", "", raw_input)

    parts = text.split()
    first, rest = parts[0], parts[1:]

    if first in BARE_DIRECTIONS and not rest:
        return Command("go", DIRECTION_ALIASES[first], raw_input)

    verb = VERB_ALIASES.get(first)
    if verb is None:
        # Unknown verb -- pass it through so the engine can report it cleanly.
        return Command(first, " ".join(rest), raw_input)

    args_text = " ".join(rest)

    if verb == "go":
        if args_text.startswith("to "):
            args_text = args_text[3:]
        args_text = DIRECTION_ALIASES.get(args_text, args_text)
    elif verb == "talk":
        if args_text.startswith("to "):
            args_text = args_text[3:]

    return Command(verb, args_text, raw_input)
