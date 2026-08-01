"""Turn-based combat resolution.

A Combat instance wraps one ongoing fight between the player and a single
monster. It knows nothing about command parsing or the game loop -- the
engine drives it one half-turn at a time (player acts, then, if the
monster is still alive and hasn't fled, the monster acts) so that the
engine can interleave player input between turns.

All monster-specific behavior (shields, frenzies, boss phases, weakness
exploits) lives on the Monster subclasses in monsters.py; this module
just calls the right hooks in the right order and collects narration.
"""

FLEE_SUCCESS_CHANCE = 0.65


class Combat:
    def __init__(self, player, monster):
        self.player = player
        self.monster = monster
        self.turn_number = 0

    def player_attack(self):
        """Resolve the player's attack against the current monster.
        Returns a list of narration lines."""
        lines = []
        flavor = self.monster.attack_flavor(self.player)
        if flavor:
            lines.append(flavor)

        dmg = self.monster.incoming_player_damage(self.player)
        self.monster.health = max(0, self.monster.health - dmg)
        lines.append(
            f"You hit {self.monster.name} for {dmg} damage! "
            f"({self.monster.health}/{self.monster.max_health} HP)"
        )

        note = self.monster.on_hit(dmg)
        if note:
            lines.append(note)

        extra = self.monster.on_damaged()
        if extra:
            lines.extend(extra)

        return lines

    def monster_turn(self):
        """Resolve the monster's turn. Returns (lines, fled) where `fled`
        is True if the monster escaped the fight instead of acting."""
        self.turn_number += 1
        raw_lines = self.monster.choose_action(self.player, self.turn_number)
        fled = bool(raw_lines) and raw_lines[-1] == "__FLEE__"
        lines = raw_lines[:-1] if fled else raw_lines
        return lines, fled
