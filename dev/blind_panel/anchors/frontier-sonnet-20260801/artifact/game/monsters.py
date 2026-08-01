"""Monsters: base class, three distinctly-behaved regular monsters, and the
two-phase final boss.

Every monster gets its stat numbers from data/monsters.yaml (loaded by
world.py into `World.monster_templates`), but *behavior* is Python, on
purpose -- the prompt this game was built against is explicit that each
regular monster should fight with its own logic rather than a shared
script. create_monster() is the one place that maps a monster id to the
class that gives it its personality.

Combat math lives partly here (how much damage a monster deals, and how
much damage it *takes*) because that's where a monster's behavior --
shields, frenzies, weaknesses -- actually changes the numbers. The turn
orchestration itself lives in combat.py.
"""

import random


class Monster:
    """Base monster: a plain attacker with no special tricks."""

    def __init__(self, id, name, description, health, attack, defense=0):
        self.id = id
        self.name = name
        self.description = description
        self.max_health = health
        self.health = health
        self.attack_power = attack
        self.defense = defense

    @property
    def is_alive(self):
        return self.health > 0

    # -- damage exchange, overridable per-monster --------------------------

    def incoming_player_damage(self, player):
        """How much damage the player's next attack deals to this monster."""
        return max(1, player.total_attack() - self.defense)

    def attack_flavor(self, player):
        """Optional narration printed just before the player's hit lands."""
        return None

    def on_hit(self, damage_dealt):
        """Optional narration/side-effect after the player successfully hits."""
        return None

    def on_damaged(self):
        """Called after every hit the monster takes. Return a list of extra
        narration lines (e.g. a phase transition). Default: nothing."""
        return []

    def choose_action(self, player, turn_number):
        """The monster's turn. Applies its effect to `player` directly and
        returns a list of narration lines. A final sentinel line of
        "__FLEE__" tells the combat loop the monster has escaped."""
        dmg = max(1, self.attack_power - player.total_defense())
        player.take_damage(dmg)
        return [f"{self.name} attacks you for {dmg} damage!"]

    # -- persistence ---------------------------------------------------------

    def to_dict(self):
        return {"health": self.health, "max_health": self.max_health}

    def load_state(self, data):
        self.health = data.get("health", self.health)
        self.max_health = data.get("max_health", self.max_health)


class DireWolf(Monster):
    """Wolf Den guardian. Behavior: Frenzy -- once wounded below half
    health, it fights harder rather than weaker."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.frenzied_announced = False

    def choose_action(self, player, turn_number):
        lines = []
        frenzied = self.health <= self.max_health // 2
        if frenzied and not self.frenzied_announced:
            lines.append(f"{self.name}'s eyes go wild with pain -- it snarls into a frenzy!")
            self.frenzied_announced = True
        power = self.attack_power + (4 if frenzied else 0)
        dmg = max(1, power - player.total_defense())
        player.take_damage(dmg)
        tag = " (frenzied!)" if frenzied else ""
        lines.append(f"{self.name} lunges and bites for {dmg} damage!{tag}")
        return lines

    def to_dict(self):
        d = super().to_dict()
        d["frenzied_announced"] = self.frenzied_announced
        return d

    def load_state(self, data):
        super().load_state(data)
        self.frenzied_announced = data.get("frenzied_announced", False)


class SkeletalGuardian(Monster):
    """Crypt guardian. Behavior: Bone Wall -- every third turn it braces
    behind a shield of bone instead of attacking, halving the damage it
    takes on the player's next hit."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shielded = False

    def incoming_player_damage(self, player):
        base = max(1, player.total_attack() - self.defense)
        if self.shielded:
            return max(1, base // 2)
        return base

    def on_hit(self, damage_dealt):
        if self.shielded:
            self.shielded = False
            return "The bone shield cracks and crumbles from the impact."
        return None

    def choose_action(self, player, turn_number):
        if turn_number % 3 == 0:
            self.shielded = True
            return [f"{self.name} raises a wall of interlocked bone, bracing for your next blow!"]
        dmg = max(1, self.attack_power - player.total_defense())
        player.take_damage(dmg)
        return [f"{self.name} swings a rusted blade for {dmg} damage!"]

    def to_dict(self):
        d = super().to_dict()
        d["shielded"] = self.shielded
        return d

    def load_state(self, data):
        super().load_state(data)
        self.shielded = data.get("shielded", False)


class BanditScout(Monster):
    """Chapel Ruins ambusher. Behavior: Coward's Gambit -- once badly
    wounded it may panic and flee the fight outright rather than fight to
    the death. It keeps whatever damage it has already taken, so a fight
    you interrupt stays interrupted."""

    FLEE_HEALTH_FRACTION = 0.4
    FLEE_CHANCE = 0.45

    def choose_action(self, player, turn_number):
        if (self.health <= self.max_health * self.FLEE_HEALTH_FRACTION
                and random.random() < self.FLEE_CHANCE):
            return [f"{self.name} panics and bolts, vanishing into the rubble!", "__FLEE__"]
        dmg = max(1, self.attack_power - player.total_defense())
        player.take_damage(dmg)
        return [f"{self.name} slashes at you for {dmg} damage!"]


class Ashwyrm(Monster):
    """The final boss. Two phases, and a hard counter to anyone who
    listened to the village's rumors: with the sunstone amulet equipped,
    its armored hide stops being a wall and starts being a liability.
    Without it, the fight is a war of attrition the player is meant to
    lose."""

    WEAKNESS_ITEM = "sunstone_amulet"

    def __init__(self, id, name, description, phase1_health, phase1_attack,
                 phase1_defense, phase2_health, phase2_attack, phase2_defense):
        super().__init__(id, name, description, phase1_health, phase1_attack, phase1_defense)
        self.phase = 1
        self.phase2_health = phase2_health
        self.phase2_attack = phase2_attack
        self.phase2_defense = phase2_defense
        self.phase2_triggered = False

    def has_weakness_exposed(self, player):
        return player.equipment.get("trinket") == self.WEAKNESS_ITEM

    def attack_flavor(self, player):
        if self.has_weakness_exposed(player):
            return ("The sunstone amulet flares with golden light -- the Ashwyrm recoils, "
                    "smoke hissing from where the glow touches its scales!")
        return "Your attack skitters off its scales as if striking a mirror."

    def incoming_player_damage(self, player):
        if self.has_weakness_exposed(player):
            effective_defense = self.defense // 4
            dmg = max(1, player.total_attack() - effective_defense)
            return int(dmg * 1.5)
        dmg = max(1, player.total_attack() - self.defense)
        return max(1, dmg // 2)

    def on_damaged(self):
        if self.phase == 1 and self.health <= 0 and not self.phase2_triggered:
            self.phase2_triggered = True
            self.phase = 2
            self.max_health = self.phase2_health
            self.health = self.phase2_health
            self.attack_power = self.phase2_attack
            self.defense = self.phase2_defense
            return [
                f"{self.name}'s outer scales shatter and slough away in smoking chunks!",
                f"With a furious roar it rears up, molten cracks glowing along its hide -- "
                f"{self.name} enters its second phase!",
            ]
        return []

    def choose_action(self, player, turn_number):
        exposed = self.has_weakness_exposed(player)
        power = self.attack_power
        if self.phase == 2 and not exposed:
            power += 5  # unopposed by the light, its fury only grows
        dmg = max(1, power - player.total_defense())
        player.take_damage(dmg)
        verb = "breathes searing flame at you" if self.phase == 2 else "claws and snaps at you"
        return [f"{self.name} {verb} for {dmg} damage!"]

    def to_dict(self):
        d = super().to_dict()
        d.update({
            "phase": self.phase,
            "phase2_triggered": self.phase2_triggered,
            "attack_power": self.attack_power,
            "defense": self.defense,
        })
        return d

    def load_state(self, data):
        super().load_state(data)
        self.phase = data.get("phase", 1)
        self.phase2_triggered = data.get("phase2_triggered", False)
        self.attack_power = data.get("attack_power", self.attack_power)
        self.defense = data.get("defense", self.defense)


MONSTER_CLASSES = {
    "dire_wolf": DireWolf,
    "skeletal_guardian": SkeletalGuardian,
    "bandit_scout": BanditScout,
    "ashwyrm": Ashwyrm,
}


def create_monster(monster_id, template):
    """Instantiate the right Monster subclass for `monster_id` from its
    YAML template dict (see data/monsters.yaml)."""
    cls = MONSTER_CLASSES.get(monster_id, Monster)
    if monster_id == "ashwyrm":
        return cls(
            id=monster_id,
            name=template["name"],
            description=template["description"],
            phase1_health=template["phase1_health"],
            phase1_attack=template["phase1_attack"],
            phase1_defense=template["phase1_defense"],
            phase2_health=template["phase2_health"],
            phase2_attack=template["phase2_attack"],
            phase2_defense=template["phase2_defense"],
        )
    return cls(
        id=monster_id,
        name=template["name"],
        description=template["description"],
        health=template["health"],
        attack=template["attack"],
        defense=template.get("defense", 0),
    )
