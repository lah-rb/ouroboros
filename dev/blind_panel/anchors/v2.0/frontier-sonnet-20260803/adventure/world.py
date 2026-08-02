"""World map for The Ashen Keep.

Nine rooms connected purely by north/south/east/west exits (no up/down,
to keep the parser's direction set exactly what the game promises the
player). Movement into a room with a living monster immediately starts
combat -- see Game.enter_room() in game.py -- so the graph below is
also, implicitly, the game's difficulty curve: the three regular
monsters and the two NPCs sit on the two branches off the central
Stairwell Landing, and the boss sits behind the Crypt at the very top.
"""

from .monsters import create_monster
from .npc import create_npc


class Room:
    """A single location in the world graph."""

    def __init__(self, key, name, description, exits=None, items=None):
        self.key = key
        self.name = name
        self.description = description
        self.exits = exits or {}          # direction -> room key
        self.items = items or []          # list of item keys currently here
        self.monster = None               # Monster instance or None
        self.npc = None                   # NPC instance or None
        self.visited = False

    def has_living_monster(self):
        return self.monster is not None and self.monster.is_alive()

    def exit_list_text(self):
        if not self.exits:
            return "none"
        return ", ".join(sorted(self.exits.keys()))


START_ROOM = "courtyard"


def build_world():
    """Construct and return a fresh dict of {room_key: Room}, with
    fresh monster and NPC instances wired in. Called once per new game
    (and once per loaded game, before save data is reapplied on top),
    so every playthrough starts from a clean, fully-populated map.
    """
    rooms = {}

    def add(key, name, description, exits, items=None):
        rooms[key] = Room(key, name, description, exits, items)

    add(
        "courtyard", "Ruined Courtyard",
        "Weeds split the flagstones of what was once a proud courtyard. "
        "The keep's broken gate hangs from a single hinge behind you; "
        "ahead, wide stairs climb north into a dark hall.",
        {"north": "hall"},
    )
    add(
        "hall", "Great Hall",
        "A vast hall, its banners burned to lace. Cold hearths line the "
        "walls, ash still heaped in their mouths. Passages lead east "
        "and west, and a grand stair climbs north; the courtyard lies "
        "south.",
        {"south": "courtyard", "east": "armory", "west": "shrine", "north": "landing"},
        items=["old_journal"],
    )
    add(
        "armory", "Armory",
        "Racks of rusted weapons line the walls, most too corroded to "
        "lift. Something is standing very still in the far corner.",
        {"west": "hall"},
        items=["iron_longsword"],
    )
    add(
        "shrine", "Shattered Shrine",
        "A small shrine, its altar cracked clean in two. Candle-wax has "
        "pooled and hardened on the floor in long-cold rivulets. A "
        "figure kneels before the altar.",
        {"east": "hall"},
        items=["healing_draught"],
    )
    add(
        "landing", "Stairwell Landing",
        "A wide landing at the heart of the keep's spiral stair. "
        "Passages open east and west, and the stair continues north "
        "into darkness; the hall lies south.",
        {"south": "hall", "east": "library", "west": "storeroom", "north": "crypt"},
    )
    add(
        "library", "High Library",
        "Shelves of scorched books rise toward a cracked skylight. "
        "Someone has clearly been living here among the ruin -- a "
        "bedroll, a cold lamp, careful stacks instead of chaos.",
        {"west": "landing"},
        items=["scaled_breastplate"],
    )
    add(
        "storeroom", "Storeroom",
        "Barrels and crates, most long since looted, line this cramped "
        "room. Something skitters in the shadows between them.",
        {"east": "landing"},
        items=["boiled_leather_vest", "healing_draught"],
    )
    add(
        "crypt", "Silent Crypt",
        "Rows of stone sarcophagi stretch into the dark, their lids "
        "carved with the likenesses of the keep's dead. The air is "
        "cold and smells of old ash. A door of black iron stands to "
        "the north.",
        {"south": "landing", "north": "throne"},
        items=["sunfire_brand"],
    )
    add(
        "throne", "Throne of Ash",
        "The throne room is scorched black, embers still glowing "
        "faintly in the cracks of the floor. A figure sits slumped on "
        "a throne of fused bone and cinder -- until it stirs.",
        {"south": "crypt"},
    )

    rooms["armory"].monster = create_monster("hollow_sentinel")
    rooms["storeroom"].monster = create_monster("ravenous_cur")
    rooms["crypt"].monster = create_monster("cave_widow")
    rooms["throne"].monster = create_monster("ashen_king")

    rooms["shrine"].npc = create_npc("sister_maren")
    rooms["library"].npc = create_npc("rell")

    return rooms
