"""Monster definitions for The Ashen Keep.

Each monster subclasses Monster and overrides take_turn() with its own
behaviour -- there is deliberately no shared "AI script". The combat
system (see game.py) only ever calls the public interface defined on
the base class: is_alive(), take_hit(), player_attack_multiplier(),
and take_turn().
"""

import random


class Monster:
    """Base class for all monsters. Provides shared bookkeeping only;
    combat behaviour (take_turn) is meant to be overridden.
    """

    def __init__(self, key, name, max_hp, attack, description,
                 intro_text, defeat_text):
        self.key = key
        self.name = name
        self.max_hp = max_hp
        self.hp = max_hp
        self.attack = attack
        self.description = description
        self.intro_text = intro_text
        self.defeat_text = defeat_text

    def is_alive(self):
        return self.hp > 0

    def take_hit(self, amount):
        """Apply incoming damage from the player. Returns remaining HP."""
        self.hp = max(0, self.hp - amount)
        return self.hp

    def _deal_damage(self, player, base_attack):
        """Shared helper for computing and applying a hit against the
        player. Individual monsters call this with whatever base_attack
        value fits their behaviour for that action; the randomness and
        the player's defense are handled here so subclasses don't each
        reimplement the same arithmetic.
        """
        variance = random.randint(-1, 2)
        raw = max(1, base_attack + variance - player.total_defense())
        player.take_damage(raw)
        return raw

    def player_attack_multiplier(self, weapon_key):
        """Multiplier applied to the player's outgoing damage against
        this monster on a given attack. Most monsters don't care what
        you hit them with; a few (guarding monsters, the boss) do.
        """
        return 1.0

    def take_turn(self, player, turn_number):
        """Perform this monster's action for the round. Mutates the
        player's state directly (damage, status effects) and returns a
        list of narration strings to print. Default behaviour is a
        plain attack; real monsters override this.
        """
        dmg = self._deal_damage(player, self.attack)
        return [f"The {self.name} attacks for {dmg} damage!"]


class HollowSentinel(Monster):
    """Guards the Armory. A slow, defensive fighter: every third turn
    it braces behind its shield instead of attacking, which halves the
    damage of the player's very next hit against it.
    """

    def __init__(self):
        super().__init__(
            key="hollow_sentinel",
            name="Hollow Sentinel",
            max_hp=32,
            attack=7,
            description=(
                "A suit of ancient plate armor, animated by some "
                "lingering malice. It moves stiffly, joints grinding, "
                "but its blows land with a soldier's discipline."
            ),
            intro_text=(
                "The empty armor in the corner grinds upright, joints "
                "shrieking, and levels a notched blade at you!"
            ),
            defeat_text=(
                "The Hollow Sentinel folds in on itself with a hollow "
                "clatter of plate, the malice animating it finally spent."
            ),
        )
        self.guard_active = False
        self.turns_taken = 0

    def player_attack_multiplier(self, weapon_key):
        if self.guard_active:
            self.guard_active = False
            return 0.5
        return 1.0

    def take_turn(self, player, turn_number):
        self.turns_taken += 1
        if self.turns_taken % 3 == 0:
            self.guard_active = True
            return [
                "The Hollow Sentinel raises its dented shield, bracing "
                "for your next blow instead of attacking."
            ]
        dmg = self._deal_damage(player, self.attack)
        return [f"The Hollow Sentinel swings its notched blade for {dmg} damage!"]


class RavenousCur(Monster):
    """Guards the Storeroom. A fast, aggressive brawler that gets
    reckless once wounded: below half health it lashes out twice in a
    single turn instead of once.
    """

    def __init__(self):
        super().__init__(
            key="ravenous_cur",
            name="Ravenous Cur",
            max_hp=18,
            attack=6,
            description=(
                "A gaunt, mange-ridden hound with too many teeth and "
                "eyes like dying coals. It has been alone down here a "
                "long time."
            ),
            intro_text=(
                "A ravenous cur bursts from behind a fallen shelf, "
                "hackles raised, and snarls!"
            ),
            defeat_text="The cur collapses with a whimper that almost sounds relieved.",
        )

    def take_turn(self, player, turn_number):
        lines = []
        if self.hp <= self.max_hp // 2:
            dmg1 = self._deal_damage(player, max(1, self.attack - 2))
            lines.append(f"Bloodied and frenzied, the cur lunges for {dmg1} damage!")
            if player.is_alive():
                dmg2 = self._deal_damage(player, max(1, self.attack - 2))
                lines.append(f"It snaps again before you can recover -- {dmg2} more damage!")
        else:
            dmg = self._deal_damage(player, self.attack)
            lines.append(f"The cur bites savagely for {dmg} damage!")
        return lines


class CaveWidow(Monster):
    """Guards the Crypt. Hits softer than the other two, but its bite
    has a chance to poison the player, applying damage over time that
    ticks at the start of each following combat round.
    """

    def __init__(self):
        super().__init__(
            key="cave_widow",
            name="Cave Widow",
            max_hp=24,
            attack=6,
            description=(
                "A spider the size of a hound, its bloated body "
                "glistening with venom. Old webbing hangs between the "
                "sarcophagi like grey funeral shrouds."
            ),
            intro_text=(
                "Something huge and many-legged drops from the crypt's "
                "ceiling with a wet click of mandibles!"
            ),
            defeat_text="The Cave Widow curls in on itself, legs twitching once, then still.",
        )

    def take_turn(self, player, turn_number):
        lines = []
        dmg = self._deal_damage(player, max(1, self.attack - 2))
        lines.append(f"The Cave Widow sinks its fangs in for {dmg} damage!")
        if player.is_alive() and random.random() < 0.6:
            if player.poisoned_turns < 3:
                player.poisoned_turns = 3
            lines.append("A venomous chill spreads through your veins. You are poisoned!")
        return lines


class AshenKing(Monster):
    """The final boss. Two phases: once at or below half health, his
    crown cracks and he enters a more aggressive second phase with a
    chance to strike twice. He is naturally resistant to ordinary
    weapons, but the Sunfire Brand burns through that resistance and
    then some -- see player_attack_multiplier().
    """

    def __init__(self):
        super().__init__(
            key="ashen_king",
            name="The Ashen King",
            max_hp=70,
            attack=8,
            description=(
                "A towering figure in a crown of cinders, skin cracked "
                "like dried riverbed, embers glowing dull orange in the "
                "seams. He was a man once."
            ),
            intro_text=(
                "The Ashen King rises from his throne of fused bone. "
                "\"Another fool comes to warm my hall,\" he rasps, ash "
                "sifting from his shoulders like grey snow."
            ),
            defeat_text=(
                "The Ashen King's crown shatters. For one moment his "
                "ashen face looks almost human again -- almost grateful "
                "-- before he crumbles to cold cinder and silence."
            ),
        )
        self.phase = 1

    def player_attack_multiplier(self, weapon_key):
        if weapon_key == "sunfire_brand":
            return 2.0
        return 0.6

    def take_turn(self, player, turn_number):
        lines = []
        if self.phase == 1 and self.hp <= self.max_hp // 2:
            self.phase = 2
            self.attack += 4
            lines.append(
                "The Ashen King's crown cracks apart, spilling grey fire! "
                "\"THEN BURN WITH ME!\" His wounds knit shut with ember "
                "and smoke -- he grows more savage!"
            )
        dmg = self._deal_damage(player, self.attack)
        if self.phase == 1:
            lines.append(f"The Ashen King strikes with his ashen blade for {dmg} damage!")
        else:
            lines.append(f"The Ashen King, wreathed in cinders, cleaves at you for {dmg} damage!")
            if player.is_alive() and random.random() < 0.4:
                dmg2 = self._deal_damage(player, max(1, self.attack - 3))
                lines.append(f"Ash swirls into a second blow -- {dmg2} more damage!")
        return lines


_MONSTER_CLASSES = {
    "hollow_sentinel": HollowSentinel,
    "ravenous_cur": RavenousCur,
    "cave_widow": CaveWidow,
    "ashen_king": AshenKing,
}


def create_monster(key):
    """Instantiate a fresh monster by key. Always returns a brand new
    object with full HP -- callers that need a shared, mutable, living
    monster (i.e. every room in the world) should call this once at
    world-build time and hold onto the instance.
    """
    return _MONSTER_CLASSES[key]()
