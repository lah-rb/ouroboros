"""Turn‑based combat mechanics operating on mutable game state."""

from __future__ import annotations

from typing import Union

from entities import Player
from state import MonsterState, GameState


class CombatEngine:
    """Orchestrates a combat encounter between the player and monsters.

    The engine mutates ``state.monsters_state`` entries to reflect damage,
    phase changes, and defeat.  It also updates the player's health and
    equipment effects as needed.

    Args:
        state: The current :class:`GameState` containing player and monster data.

    >>> dummy_state = GameState()
    >>> engine = CombatEngine()
    >>> isinstance(engine, CombatEngine)
    True
    """

    def start_combat(self, state: GameState) -> None:
        """Begin combat loop for the current room's monsters.

        The method repeatedly prompts the player (outside this contract) and
        calls :func:`perform_attack` until either all monsters are defeated or
        the player's health drops to zero.

        Args:
            state: Shared mutable game state.

        Raises:
            RuntimeError: If there are no monster entries for the player's room.
        """
        # Ensure the game state contains a collection of monster states.
        try:
            monsters = state.monsters_state
        except AttributeError as exc:
            raise RuntimeError("GameState lacks 'monsters_state' attribute") from exc

        if not monsters:
            raise RuntimeError("No monster entries for the player's room")

        # Retrieve the player object from the game state.
        try:
            player = state.player
        except AttributeError as exc:
            raise RuntimeError("GameState lacks 'player' attribute") from exc

        # Combat continues while both sides have living combatants.
        while getattr(player, "health", 0) > 0 and any(
            getattr(monster, "health", 0) > 0 for monster in monsters
        ):
            # Player attacks the first alive monster.
            target = next((m for m in monsters if getattr(m, "health", 0) > 0), None)
            if target is not None:
                perform_attack(player, target)

            # Each surviving monster attacks the player.
            for monster in monsters:
                if getattr(monster, "health", 0) > 0:
                    perform_attack(monster, player)


def perform_attack(
    attacker: Union[Player, MonsterState], defender: Union[Player, MonsterState]
) -> None:
    """Apply a single attack from ``attacker`` to ``defender``.

    The function updates the defender's health in‑place using
    :func:`calculate_damage`.  No value is returned.

    Args:
        attacker: Either the player or a monster state object.
        defender: The opposite combatant.

    Raises:
        ValueError: If either participant lacks required combat attributes.
    """
    # Verify required attributes on attacker and defender.
    if not hasattr(attacker, "attack"):
        raise ValueError("Attacker missing required 'attack' attribute")
    if not hasattr(defender, "defense"):
        raise ValueError("Defender missing required 'defense' attribute")
    if not hasattr(defender, "health"):
        raise ValueError("Defender missing required 'health' attribute")

    # Calculate damage using the sibling symbol.
    dmg = calculate_damage(attacker, defender)

    # Apply damage to defender's health in‑place.
    # Clamp at zero to avoid negative health values.
    new_health = getattr(defender, "health") - dmg
    setattr(defender, "health", max(0, new_health))


def calculate_damage(
    attacker: Union[Player, MonsterState], defender: Union[Player, MonsterState]
) -> int:
    """Compute damage dealt by ``attacker`` to ``defender``.

    Damage = max(0, attacker.attack - defender.defense).

    Args:
        attacker: Combatant dealing damage.
        defender: Combatant receiving damage.

    Returns:
        Non‑negative integer amount of HP to subtract from defender.

    >>> class Dummy:
    ...     attack = 5
    ...     defense = 2
    >>> calculate_damage(Dummy(), Dummy())
    3
    """
    # Verify required combat attributes are present.
    if not hasattr(attacker, "attack"):
        raise ValueError("Attacker lacks required 'attack' attribute.")
    if not hasattr(defender, "defense"):
        raise ValueError("Defender lacks required 'defense' attribute.")

    # Compute raw damage and ensure it is non‑negative.
    raw_damage = attacker.attack - defender.defense  # type: ignore[attr-defined]
    return max(0, raw_damage)


if __name__ == "__main__":
    open("p")
