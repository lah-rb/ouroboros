"""The Player class for The Ashen Keep.

Holds every piece of state a save file needs to reconstruct exactly
where the player was: health, base stats, equipped weapon/armor,
inventory, current and previous location, poison status, which
monsters have been defeated, and how far each NPC conversation has
progressed.
"""

from .items import ITEMS


class Player:
    def __init__(self):
        self.max_hp = 30
        self.hp = 30
        self.base_attack = 4
        self.base_defense = 0
        self.weapon = "rusty_dagger"
        self.armor = None
        self.inventory = ["rusty_dagger", "healing_draught"]
        self.location = "courtyard"
        self.previous_location = "courtyard"
        self.poisoned_turns = 0
        self.defeated_monsters = []       # list of monster keys
        self.npc_stage = {}               # npc key -> int stage reached
        self.turns_taken = 0              # combat rounds elapsed, total

    # ---------- basic state ----------

    def is_alive(self):
        return self.hp > 0

    def take_damage(self, amount):
        self.hp = max(0, self.hp - amount)

    def heal(self, amount):
        before = self.hp
        self.hp = min(self.max_hp, self.hp + amount)
        return self.hp - before

    def total_attack(self):
        bonus = ITEMS[self.weapon].value if self.weapon else 0
        return self.base_attack + bonus

    def total_defense(self):
        bonus = ITEMS[self.armor].value if self.armor else 0
        return self.base_defense + bonus

    # ---------- inventory ----------

    def has_item(self, key):
        return key in self.inventory

    def item_counts(self):
        """Return {item_key: count} for display purposes, preserving
        first-seen order.
        """
        counts = {}
        for key in self.inventory:
            counts[key] = counts.get(key, 0) + 1
        return counts

    # ---------- persistence ----------

    def to_dict(self):
        return {
            "max_hp": self.max_hp,
            "hp": self.hp,
            "base_attack": self.base_attack,
            "base_defense": self.base_defense,
            "weapon": self.weapon,
            "armor": self.armor,
            "inventory": list(self.inventory),
            "location": self.location,
            "previous_location": self.previous_location,
            "poisoned_turns": self.poisoned_turns,
            "defeated_monsters": list(self.defeated_monsters),
            "npc_stage": dict(self.npc_stage),
            "turns_taken": self.turns_taken,
        }

    @classmethod
    def from_dict(cls, data):
        p = cls()
        p.max_hp = data.get("max_hp", p.max_hp)
        p.hp = data.get("hp", p.max_hp)
        p.base_attack = data.get("base_attack", p.base_attack)
        p.base_defense = data.get("base_defense", p.base_defense)
        p.weapon = data.get("weapon", p.weapon)
        p.armor = data.get("armor", p.armor)
        p.inventory = list(data.get("inventory", p.inventory))
        p.location = data.get("location", p.location)
        p.previous_location = data.get("previous_location", p.location)
        p.poisoned_turns = data.get("poisoned_turns", 0)
        p.defeated_monsters = list(data.get("defeated_monsters", []))
        p.npc_stage = dict(data.get("npc_stage", {}))
        p.turns_taken = data.get("turns_taken", 0)
        return p
