"""World loading and static content classes.

Everything that makes up the game *world* -- rooms, items, NPCs, and the
stat templates for monsters -- is authored in the YAML files under
../data/. This module's only job is to read those files and turn them
into small, easy-to-use Python objects. It knows nothing about combat,
command parsing, or the player -- it is pure content plumbing.

Runtime-mutable state (a room's remaining items, a monster's current
health, which monsters are defeated, etc.) is NOT stored here as global
truth -- it lives on the Room/monster instances the engine hands out, and
is what gets written to / restored from a save file. See engine.py.
"""

from pathlib import Path

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Room:
    """A single location in the world."""

    def __init__(self, room_id, name, description, exits=None, items=None,
                 npc=None, monster=None):
        self.id = room_id
        self.name = name
        self.description = description
        self.exits = dict(exits or {})   # direction -> room_id
        self.items = list(items or [])   # item ids currently lying here
        self.npc = npc                   # npc id, or None
        self.monster = monster           # monster id, or None


class Item:
    """A takeable thing: a weapon, armor, a trinket, or a consumable."""

    def __init__(self, item_id, name, description, type, slot=None,
                 attack_bonus=0, defense_bonus=0, heal_amount=0, special=None):
        self.id = item_id
        self.name = name
        self.description = description
        self.type = type                 # weapon | armor | trinket | consumable
        self.slot = slot                 # weapon | armor | trinket | None
        self.attack_bonus = attack_bonus
        self.defense_bonus = defense_bonus
        self.heal_amount = heal_amount
        self.special = special           # e.g. "ashwyrm_weakness"


class NPC:
    """A conversational character with a greeting and topic-based dialogue."""

    def __init__(self, npc_id, name, room, description, greeting,
                 repeat_greeting, topics, default_response):
        self.id = npc_id
        self.name = name
        self.room = room
        self.description = (description or "").strip()
        self.greeting = greeting.strip()
        self.repeat_greeting = (repeat_greeting or greeting).strip()
        self.topics = {k: v.strip() for k, v in (topics or {}).items()}
        self.default_response = default_response.strip()


class World:
    """Holds every static content collection, keyed by id."""

    def __init__(self):
        self.rooms = {}
        self.items = {}
        self.npcs = {}
        self.monster_templates = {}

    @classmethod
    def load(cls, data_dir=None):
        data_dir = Path(data_dir) if data_dir else DATA_DIR
        world = cls()

        with open(data_dir / "rooms.yaml", encoding="utf-8") as f:
            rooms_data = yaml.safe_load(f) or {}
        for room_id, rd in rooms_data.items():
            world.rooms[room_id] = Room(
                room_id=room_id,
                name=rd["name"],
                description=rd["description"].strip(),
                exits=rd.get("exits", {}),
                items=rd.get("items", []),
                npc=rd.get("npc"),
                monster=rd.get("monster"),
            )

        with open(data_dir / "items.yaml", encoding="utf-8") as f:
            items_data = yaml.safe_load(f) or {}
        for item_id, idata in items_data.items():
            world.items[item_id] = Item(
                item_id=item_id,
                name=idata["name"],
                description=idata["description"].strip(),
                type=idata["type"],
                slot=idata.get("slot"),
                attack_bonus=idata.get("attack_bonus", 0),
                defense_bonus=idata.get("defense_bonus", 0),
                heal_amount=idata.get("heal_amount", 0),
                special=idata.get("special"),
            )

        with open(data_dir / "npcs.yaml", encoding="utf-8") as f:
            npcs_data = yaml.safe_load(f) or {}
        for npc_id, ndata in npcs_data.items():
            world.npcs[npc_id] = NPC(
                npc_id=npc_id,
                name=ndata["name"],
                room=ndata["room"],
                description=ndata.get("description", ""),
                greeting=ndata["greeting"],
                repeat_greeting=ndata.get("repeat_greeting", ndata["greeting"]),
                topics=ndata.get("topics", {}),
                default_response=ndata.get(
                    "default_response",
                    "They shake their head. \"I don't know anything about that.\"",
                ),
            )

        with open(data_dir / "monsters.yaml", encoding="utf-8") as f:
            world.monster_templates = yaml.safe_load(f) or {}

        return world

    def get_room(self, room_id):
        return self.rooms[room_id]

    def get_item(self, item_id):
        return self.items.get(item_id)

    def get_npc(self, npc_id):
        return self.npcs.get(npc_id)
