"""Command parsing for The Ashen Keep.

parse() turns a raw line of player input into a (verb, rest) pair
where verb is a canonical command name (e.g. "go", "take", "attack")
and rest is whatever free text followed it (already lowercased and
stripped, may be ""). All the game actually has to do is switch on the
canonical verb -- every synonym and shorthand ("n", "get", "wield",
"x") is resolved here in one place.
"""

DIR_FULL = {
    "n": "north",
    "north": "north",
    "s": "south",
    "south": "south",
    "e": "east",
    "east": "east",
    "w": "west",
    "west": "west",
}

_ALIASES = {
    "l": "look",
    "look": "look",
    "i": "inventory",
    "inv": "inventory",
    "inventory": "inventory",
    "stat": "status",
    "stats": "status",
    "status": "status",
    "h": "help",
    "help": "help",
    "?": "help",
    "q": "quit",
    "quit": "quit",
    "exit": "quit",
    "go": "go",
    "move": "go",
    "walk": "go",
    "take": "take",
    "get": "take",
    "pickup": "take",
    "grab": "take",
    "drop": "drop",
    "discard": "drop",
    "leave": "drop",
    "use": "use",
    "drink": "use",
    "apply": "use",
    "consume": "use",
    "equip": "equip",
    "wear": "equip",
    "wield": "equip",
    "hold": "equip",
    "examine": "examine",
    "x": "examine",
    "inspect": "examine",
    "read": "examine",
    "talk": "talk",
    "speak": "talk",
    "chat": "talk",
    "greet": "talk",
    "attack": "attack",
    "fight": "attack",
    "hit": "attack",
    "strike": "attack",
    "flee": "flee",
    "run": "flee",
    "escape": "flee",
    "retreat": "flee",
    "save": "save",
    "load": "load",
}


def parse(raw_input):
    """Parse one line of input into (verb, rest). Returns ("", "") for
    blank input.
    """
    raw = raw_input.strip()
    if not raw:
        return "", ""

    parts = raw.split(None, 1)
    verb = parts[0].lower()
    rest = parts[1].strip().lower() if len(parts) > 1 else ""

    # Bare direction words ("north", "n") act as if the player typed
    # "go north".
    if verb in DIR_FULL and rest == "":
        return "go", DIR_FULL[verb]

    canonical = _ALIASES.get(verb, verb)

    if canonical == "go":
        rest = DIR_FULL.get(rest, rest)

    if canonical == "talk" and rest.startswith("to "):
        rest = rest[3:].strip()

    return canonical, rest


HELP_TEXT = """
==============================================================
COMMANDS
==============================================================
Movement:
  go north / go south / go east / go west   (or just: n, s, e, w)

Looking around:
  look                    describe your current surroundings again
  examine <thing>         look closely at an item, yourself, or a foe
  status                  show your health, equipment, and location
  inventory               list what you're carrying

Items:
  take <item>             pick something up off the floor
  drop <item>             leave something on the floor
  equip <item>            wield a weapon or wear a piece of armor
  use <item>              drink a potion or use a consumable

People:
  talk to <name>          speak with someone in the room

Combat (once a monster blocks your way):
  attack                  strike with your equipped weapon
  flee                    try to break off and retreat
  use <item> / equip <item>   still work mid-fight, and cost a turn

Other:
  save                    write your progress to savegame.json
  load                    load progress from savegame.json
  help                    show this message again
  quit                    leave the game (with a confirmation)
==============================================================
Tip: people in this keep know things. Talk to them more than once.
==============================================================
"""
