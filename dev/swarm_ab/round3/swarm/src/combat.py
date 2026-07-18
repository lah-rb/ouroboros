"""Turn‑based combat mechanics between the player and a monster."""

from __future__ import annotations

from enum import Enum, auto

# ---------------------------------------------------------------------------
# Import handling
# ---------------------------------------------------------------------------
# The project may be executed either as a package (``python -m src.main``)
# or directly (e.g. ``python src/combat.py``).  In the latter case the
# absolute import ``src.models`` fails because ``src`` is not on the import
# path.  We therefore attempt the absolute import first and fall back to a
# relative import when necessary.
# ---------------------------------------------------------------------------
try:
    from src.models import Player, Monster  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    # Relative import works when the file is run as a script inside the
    # ``src`` package directory.
    from .models import Player, Monster  # type: ignore

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class CombatResult(Enum):
    """Outcome of a combat encounter."""

    PLAYER_VICTORY = auto()
    PLAYER_DEFEAT = auto()
    ESCAPED = auto()


def initiate_combat(player: Player, monster: Monster) -> CombatResult:
    """Run a combat loop until one side wins or the player flees.

    The function updates ``player`` and ``monster`` health in‑place.
    It may modify ``player.inventory`` when healing items are consumed.

    Args:
        player: The active :class:`~src.models.Player` instance.
        monster: The :class:`~src.models.Monster` the player is fighting.

    Returns:
        A :class:`CombatResult` indicating how the encounter ended.

    Raises:
        RuntimeError: If either participant has non‑positive health at start.

    >>> dummy_player = Player(
    ...     location='room',
    ...     health=30,
    ...     max_health=30,
    ...     attack=5,
    ...     defense=2,
    ... )
    >>> dummy_monster = Monster(
    ...     id='gob',
    ...     name='Goblin',
    ...     health=10,
    ...     max_health=10,
    ...     attack=3,
    ...     description='A goblin'
    ... )
    >>> result = initiate_combat(dummy_player, dummy_monster)
    >>> result is CombatResult.PLAYER_VICTORY
    True
    """
    # Validate starting health
    if player.health <= 0 or monster.health <= 0:
        raise RuntimeError("Combat participants must have positive health to start.")

    # Helper to compute damage ensuring at least 1 point is dealt
    def _damage(dealer_attack: int, defender_defense: int = 0) -> int:
        """Calculate damage dealt by ``dealer`` to ``defender``."""
        return max(1, dealer_attack - defender_defense)

    while True:
        # Player attacks monster
        dmg_to_monster = _damage(player.attack)
        monster.health = max(0, monster.health - dmg_to_monster)

        if monster.health == 0:
            return CombatResult.PLAYER_VICTORY

        # Monster attacks player
        dmg_to_player = _damage(monster.attack, player.defense)
        player.health = max(0, player.health - dmg_to_player)

        if player.health == 0:
            return CombatResult.PLAYER_DEFEAT

        # No explicit flee logic is defined in the contract; combat continues
