"""The player character: stats, inventory, equipment, and progress flags.

A Player object owns every piece of state that must survive a save/load
round trip *except* the world content itself. It keeps a live reference
to the World (set by the engine right after construction) purely so it
can look up the attack/defense bonuses of whatever is currently equipped
-- that reference is never serialized.
"""

EQUIP_SLOTS = ("weapon", "armor", "trinket")


class Player:
    def __init__(self, name="Wanderer"):
        self.name = name
        self.max_health = 100
        self.health = 100
        self.base_attack = 6
        self.base_defense = 0

        self.inventory = {}                          # item_id -> count
        self.equipment = {slot: None for slot in EQUIP_SLOTS}
        self.current_room = "village_square"

        self.defeated_monsters = set()                # monster ids
        self.met_npcs = set()                          # npc ids

        self.world = None                              # set by the engine, not saved

    # -- derived stats ----------------------------------------------------

    def total_attack(self):
        if self.world is None:
            return self.base_attack
        bonus = 0
        for item_id in self.equipment.values():
            if item_id:
                item = self.world.get_item(item_id)
                if item:
                    bonus += item.attack_bonus
        return self.base_attack + bonus

    def total_defense(self):
        if self.world is None:
            return self.base_defense
        bonus = 0
        for item_id in self.equipment.values():
            if item_id:
                item = self.world.get_item(item_id)
                if item:
                    bonus += item.defense_bonus
        return self.base_defense + bonus

    @property
    def is_alive(self):
        return self.health > 0

    # -- health -------------------------------------------------------------

    def take_damage(self, amount):
        self.health = max(0, self.health - max(0, amount))

    def heal(self, amount):
        before = self.health
        self.health = min(self.max_health, self.health + max(0, amount))
        return self.health - before

    # -- inventory ----------------------------------------------------------

    def add_item(self, item_id, count=1):
        self.inventory[item_id] = self.inventory.get(item_id, 0) + count

    def remove_item(self, item_id, count=1):
        have = self.inventory.get(item_id, 0)
        if have <= 0:
            return False
        remaining = have - count
        if remaining <= 0:
            del self.inventory[item_id]
        else:
            self.inventory[item_id] = remaining
        return True

    def has_item(self, item_id):
        return self.inventory.get(item_id, 0) > 0

    # -- persistence ----------------------------------------------------------

    def to_dict(self):
        return {
            "name": self.name,
            "max_health": self.max_health,
            "health": self.health,
            "base_attack": self.base_attack,
            "base_defense": self.base_defense,
            "inventory": dict(self.inventory),
            "equipment": dict(self.equipment),
            "current_room": self.current_room,
            "defeated_monsters": sorted(self.defeated_monsters),
            "met_npcs": sorted(self.met_npcs),
        }

    @classmethod
    def from_dict(cls, data):
        p = cls(name=data.get("name", "Wanderer"))
        p.max_health = data.get("max_health", 100)
        p.health = data.get("health", p.max_health)
        p.base_attack = data.get("base_attack", 6)
        p.base_defense = data.get("base_defense", 0)
        p.inventory = dict(data.get("inventory", {}))
        p.equipment = {slot: None for slot in EQUIP_SLOTS}
        p.equipment.update(data.get("equipment", {}))
        p.current_room = data.get("current_room", "village_square")
        p.defeated_monsters = set(data.get("defeated_monsters", []))
        p.met_npcs = set(data.get("met_npcs", []))
        return p
