"""
Turn‑based combat engine.
"""

from dataclasses import dataclass

from .models import Player, Monster


@dataclass
class CombatResult:
    victory: bool  # player defeated the monster
    player_dead: bool  # player died
    message: str  # narrative description of the outcome


class CombatEngine:
    """
    Handles a single combat encounter between the player and a monster.
    """

    def __init__(self, items: dict):
        """
        ``items`` is the master item dictionary (id -> Item) needed for weapon/armor look‑ups.
        """
        self.items = items

    def start_combat(self, player: Player, monster: Monster) -> CombatResult:
        log = []
        log.append(f"You engage the {monster.name}!")

        # Simple two‑phase boss logic
        is_final_boss = monster.id == "final_boss"
        phase_two_triggered = False

        while player.health > 0 and monster.health > 0:
            # Player attack
            player_attack = player.effective_attack(self.items)
            # Boss weakness: if final boss and player has the silver_key equipped as weapon,
            # double damage.
            if is_final_boss and player.equipped_weapon == "silver_key":
                player_attack *= 2

            damage_to_monster = max(1, player_attack - monster.defense)
            monster.health -= damage_to_monster
            log.append(
                f"You strike for {damage_to_monster} damage. ({monster.health}/{monster.max_health} HP left)"
            )

            if monster.health <= 0:
                break

            # Boss phase change check
            if (
                is_final_boss
                and not phase_two_triggered
                and monster.health <= monster.max_health // 2
            ):
                phase_two_triggered = True
                monster.attack += 3
                monster.defense += 2
                log.append(
                    "The Dark Lord roars and grows more ferocious! Its attacks become stronger."
                )

            # Monster attack
            monster_attack = monster.attack
            player_defense = player.effective_defense(self.items)
            damage_to_player = max(1, monster_attack - player_defense)
            player.health -= damage_to_player
            log.append(
                f"The {monster.name} hits you for {damage_to_player} damage. ({player.health}/{player.max_health} HP left)"
            )

        if player.health <= 0:
            return CombatResult(
                victory=False,
                player_dead=True,
                message="\n".join(log) + "\nYou have been slain.",
            )
        else:
            return CombatResult(
                victory=True,
                player_dead=False,
                message="\n".join(log) + f"\nYou have defeated the {monster.name}!",
            )
