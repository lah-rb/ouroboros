"""Combat system for the adventure game.

Provides:
- ``BossPhase`` – an enum describing the two phases of a boss monster.
- ``CombatEngine`` – orchestrates a single turn of combat between a
  :class:`entities.Player` and a :class:`entities.Monster`.

The engine is deliberately simple but fully functional:
* Player damage = player.attack (weapon bonuses are ignored for now).
* Monster damage = monster.attack, reduced by the player's defense.
* Defensive monsters may skip their attack when healthy.
* Bosses change phase when their health drops below 50 % of max health,
  which doubles their attack power.
* The result of each turn is a dictionary matching the contract
  ``CombatEngine.turn_result``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Optional

from entities import Player, Monster


class BossPhase(Enum):
    """Two‑phase state for boss monsters."""

    PHASE_ONE = 1
    PHASE_TWO = 2


class CombatEngine:
    """Handles a single turn of combat.

    Parameters
    ----------
    player : Player
        The player character participating in combat.
    monster : Monster
        The opponent. Its ``behavior`` field determines AI logic.
    """

    def __init__(self, player: Player, monster: Monster) -> None:
        self.player: Player = player
        self.monster: Monster = monster

        # Inventory of Item objects available during combat (item_id → Item)
        self.items: Dict[str, Any] = {}

        # Track turn number (useful for future extensions)
        self.turn_number: int = 0

        # Boss‑specific state
        self.boss_phase: Optional[BossPhase] = None
        if self.monster.behavior.lower() == "boss":
            # Start in phase one; will transition automatically later.
            self.boss_phase = BossPhase.PHASE_ONE

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def take_turn(self, player_action: str) -> Dict[str, Any]:
        """Execute one combat round.

        Returns a dict adhering to the ``CombatEngine.turn_result`` contract.

        Parameters
        ----------
        player_action : str
            Normalised action string supplied by the game loop (e.g. ``"attack"``,
            ``"flee"``, ``"use <item_id>"``). ``"attack"`` has its usual effect;
            ``"use"`` will attempt to consume a healing item.

        Returns
        -------
        dict
            Turn result with keys:
            ``player_action``, ``monster_action``, ``damage_to_player``,
            ``damage_to_monster``, ``player_defeated``, ``monster_defeated``,
            ``phase_change``.
        """
        # Increment turn counter
        self.turn_number += 1

        # Normalise the player action string
        player_action = player_action.strip().lower()

        # Determine monster's AI decision before damage is applied
        monster_action = self._determine_monster_action()

        # -----------------------------------------------------------------
        # Handle player actions
        # -----------------------------------------------------------------
        damage_to_monster: int = 0
        damage_to_player: int = 0

        if player_action.startswith("use"):
            # Expected format: "use <item_id>"
            parts = player_action.split(maxsplit=1)
            if len(parts) == 2:
                _, item_id = parts
                if item_id in self.player.inventory:
                    # Look up the Item object; fall back to a generic heal if missing.
                    item = self.items.get(item_id)
                    heal_amount = 0
                    if (
                        item
                        and hasattr(item, "is_consumable")
                        and callable(item.is_consumable)
                        and item.is_consumable()
                    ):
                        heal_amount = int(item.stats.get("heal", 0))

                    # Apply healing only if it would actually restore health.
                    if heal_amount > 0:
                        new_health = min(
                            self.player.health + heal_amount,
                            self.player.max_health,
                        )
                        healed = new_health - self.player.health
                        if healed > 0:
                            self.player.health = new_health
                            # Remove the used item from inventory only when healing occurred.
                            try:
                                self.player.inventory.remove(item_id)
                            except ValueError:
                                pass

            # Using an item does not directly damage the monster
            damage_to_monster = 0
            # Monster still gets to act this turn
            damage_to_player = self._apply_monster_action(monster_action)
        else:
            # Regular combat actions (attack, flee, etc.)
            damage_to_monster = self._apply_player_action(player_action)
            damage_to_player = self._apply_monster_action(monster_action)

        # -----------------------------------------------------------------
        # Update defeat flags
        # -----------------------------------------------------------------
        player_defeated = self.player.health <= 0
        monster_defeated = self.monster.health <= 0

        # -----------------------------------------------------------------
        # Handle boss phase transition (if applicable)
        # -----------------------------------------------------------------
        phase_change = False
        if self.boss_phase is not None and not monster_defeated:
            threshold = self.monster.max_health // 2
            if (
                self.boss_phase == BossPhase.PHASE_ONE
                and self.monster.health <= threshold
            ):
                self.boss_phase = BossPhase.PHASE_TWO
                phase_change = True

        # Build the result dict exactly as specified by the contract
        turn_result: Dict[str, Any] = {
            "player_action": player_action,
            "monster_action": monster_action,
            "damage_to_player": damage_to_player,
            "damage_to_monster": damage_to_monster,
            "player_defeated": player_defeated,
            "monster_defeated": monster_defeated,
            "phase_change": phase_change,
        }

        return turn_result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _determine_monster_action(self) -> str:
        """Choose the monster's action based on its behavior and health."""
        behavior = self.monster.behavior.lower()

        if behavior == "aggressive":
            return "attack"

        if behavior == "defensive":
            # Defensive monsters attack only when low on health
            health_ratio = self.monster.health / max(1, self.monster.max_health)
            return "attack" if health_ratio < 0.3 else "defend"

        if behavior == "boss":
            # Bosses always act; the phase influences damage elsewhere.
            return "attack"

        # Default fallback
        return "attack"

    def _apply_player_action(self, action: str) -> int:
        """Resolve the player's action and return damage dealt to the monster."""
        if action != "attack" or self.monster.health <= 0:
            return 0

        # Base player attack; weapon bonuses are not modelled here.
        damage = max(0, self.player.attack)

        # Apply damage
        self.monster.health = max(0, self.monster.health - damage)
        return damage

    def _apply_monster_action(self, action: str) -> int:
        """Resolve the monster's action and return damage dealt to the player."""
        if action == "defend" or self.player.health <= 0:
            # No damage this turn
            return 0

        if action != "attack":
            # Unknown actions are treated as no‑op
            return 0

        # Base monster attack
        base_damage = max(0, self.monster.attack)

        # Boss phase may amplify damage
        if self.boss_phase == BossPhase.PHASE_TWO:
            base_damage *= 2  # double damage in second phase

        # Player defense reduces incoming damage
        damage = max(0, base_damage - self.player.defense)

        # Apply damage
        self.player.health = max(0, self.player.health - damage)
        return damage


__all__ = ["CombatEngine", "BossPhase"]
