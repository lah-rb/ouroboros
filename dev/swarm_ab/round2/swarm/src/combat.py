"""Turn‑based combat mechanics and boss phase handling."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Architecture‑declared imports
from src.models import Player, Monster, GameState


@dataclass
class CombatResult:
    """Outcome of a single combat turn.

    Args:
        outcome: One of ``"player_hit"``, ``"monster_hit"``, ``"monster_defeated"``,
                 ``"player_defeated"``, or ``"continue"``.
        player_health: Player HP after the turn.
        monster_health: Monster HP after the turn (0 if dead).

    >>> cr = CombatResult(outcome="continue", player_health=10, monster_health=5)
    >>> cr.outcome
    'continue'
    """

    outcome: str
    player_health: int
    monster_health: int


def resolve_combat_turn(state: GameState, monster_id: str) -> CombatResult:
    """Execute one combat round between the player and a monster.

    The player attacks first; if the monster survives it may retaliate.
    Handles boss phase transitions via :func:`check_boss_phase`.

    Args:
        state: Current mutable game state.
        monster_id: Identifier of the monster being fought.

    Returns:
        A :class:`CombatResult` describing what happened.

    Raises:
        KeyError: If ``monster_id`` is not present in ``state.monsters``.

    >>> # doctest placeholder – real test requires full GameState setup
    ...
    """
    # Retrieve the player and monster objects from the game state.
    # ``state.player`` is expected to be an instance of :class:`Player`.
    # ``state.monsters`` is a mapping (e.g., dict) from monster identifiers
    # to :class:`Monster` instances.
    if monster_id not in state.monsters:
        raise KeyError(f"Monster ID '{monster_id}' not found in game state.")

    player: Player = state.player
    monster: Monster = state.monsters[monster_id]

    # --- Player attacks monster -------------------------------------------------
    damage_to_monster = apply_damage(player.attack, monster.defense)
    monster.health = max(0, monster.health - damage_to_monster)

    # Check if the monster has been defeated by the player's attack.
    if monster.health == 0:
        # Remove the monster from the state's collection (it is dead).
        del state.monsters[monster_id]

        # If the monster is a boss, we still need to run phase logic in case
        # the defeat triggers a final phase transition before removal.
        if getattr(monster, "is_boss", False):
            try:
                check_boss_phase(monster, state)
            except Exception:
                # The contract for ``check_boss_phase`` specifies it may raise
                # ``ValueError`` for malformed data. Propagate that error so
                # callers see the correct failure mode.
                raise

        return CombatResult(
            outcome="monster_defeated",
            player_health=player.health,
            monster_health=0,
        )

    # --- Monster (still alive) may retaliate ------------------------------------
    # Only monsters that have a non‑zero attack value can hit back.
    if monster.attack > 0:
        damage_to_player = apply_damage(monster.attack, player.defense)
        player.health = max(0, player.health - damage_to_player)

        # Check if the player has been defeated by the monster's retaliation.
        if player.health == 0:
            return CombatResult(
                outcome="player_defeated",
                player_health=0,
                monster_health=monster.health,
            )

    # --- Boss phase handling ----------------------------------------------------
    # After the turn (whether or not the monster retaliated), update boss
    # phases if applicable.
    if getattr(monster, "is_boss", False):
        check_boss_phase(monster, state)

    # If neither side was defeated, the combat continues.
    return CombatResult(
        outcome="continue",
        player_health=player.health,
        monster_health=monster.health,
    )


def apply_damage(attacker_attack: int, defender_defense: int) -> int:
    """Calculate damage dealt after defense mitigation.

    Damage = max(1, attacker_attack - defender_defense).

    Args:
        attacker_attack: Attack value of the aggressor.
        defender_defense: Defense value of the target.

    Returns:
        Positive integer damage amount.

    >>> apply_damage(5, 2)
    3
    >>> apply_damage(2, 5)
    1
    """
    return max(1, attacker_attack - defender_defense)


def check_boss_phase(monster: Monster, state: GameState) -> None:
    """Update boss ``phase`` based on its current health.

    Mutates ``monster`` in‑place; no return value.

    Args:
        monster: Boss monster instance (must have ``is_boss`` true and ``phases`` defined).
        state: Full game state (may be consulted for global effects).

    Raises:
        ValueError: If the monster is not a boss or has malformed phase data.

    >>> # doctest placeholder – requires proper Monster with phases
    ...
    """
    # Validate that the monster is marked as a boss.
    if not getattr(monster, "is_boss", False):
        raise ValueError("check_boss_phase called on non‑boss monster")

    # Ensure the monster defines phase thresholds.
    # Expected format: a list of tuples (threshold_percent, phase_name)
    # sorted in descending order of threshold_percent.
    phases = getattr(monster, "phases", None)
    if not isinstance(phases, list) or not all(
        isinstance(p, tuple) and len(p) == 2 for p in phases
    ):
        raise ValueError("monster.phases must be a list of (threshold, phase) tuples")

    # Current health percentage of the boss.
    max_hp = getattr(monster, "max_health", None)
    current_hp = getattr(monster, "health", None)
    if not isinstance(max_hp, int) or not isinstance(current_hp, int) or max_hp <= 0:
        raise ValueError("monster must have valid max_health and health attributes")

    health_percent = (current_hp / max_hp) * 100

    # Determine the appropriate phase based on health percentage.
    # The first matching threshold (from highest to lowest) becomes the new phase.
    new_phase: Optional[str] = None
    for threshold, phase_name in phases:
        if not isinstance(threshold, (int, float)):
            raise ValueError("phase threshold must be numeric")
        if health_percent <= threshold:
            new_phase = phase_name
            break

    # If no phase matched, retain the current phase (or set to None if undefined).
    if new_phase is not None and getattr(monster, "phase", None) != new_phase:
        monster.phase = new_phase

    # Optional: trigger any side‑effects tied to phase changes.
    # Some games may define a callable on the monster called `on_phase_change`.
    on_change = getattr(monster, "on_phase_change", None)
    if callable(on_change):
        on_change(state, monster)
