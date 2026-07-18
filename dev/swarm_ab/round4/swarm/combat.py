"""Turn‑based combat engine handling player vs monster or boss encounters."""

from __future__ import annotations
from typing import Any

from models import Player, Monster, Boss


class CombatEngine:
    """Orchestrates a single combat session.

    Args:
        player: The current Player instance.
        opponent: Either a Monster or a Boss object the player is fighting.

    Methods:
        start(): Run the combat loop until one side is defeated or the player flees.
        apply_player_action(action_dict): Process a player command during combat.

    >>> # Minimal doctest – real combat requires full game state.
    >>> p = Player(location="room", health=10, max_health=10,
    ...            attack=2, defense=1, inventory=[], equipped_weapon=None)
    >>> m = Monster(id="goblin", name="Goblin", description="",
    ...             health=5, attack=1, behavior="aggressive", location="room")
    >>> engine = CombatEngine(p, m)
    >>> isinstance(engine, CombatEngine)
    True
    """

    def __init__(self, player: Player, opponent: Any) -> None:
        # Validate opponent type
        if not isinstance(opponent, (Monster, Boss)):
            raise RuntimeError("CombatEngine requires a Monster or Boss as opponent")
        self.player = player
        self.opponent = opponent
        self.fled = False

        # Initialise boss health tracking if needed
        if isinstance(self.opponent, Boss):
            # Phase 1 starts with health_phase1
            self.opponent._current_phase = 1
            self.opponent._current_health = self.opponent.health_phase1

    def start(self) -> None:
        """Run the combat loop until resolution.

        The method updates ``player`` and ``opponent`` in‑place.
        It may raise RuntimeError if called with an invalid opponent type.
        """

        # Helper closures for readability; they are not top‑level symbols.
        def player_alive() -> bool:
            return self.player.health > 0

        def opponent_alive() -> bool:
            if isinstance(self.opponent, Monster):
                return self.opponent.health > 0
            # Boss handling
            return getattr(self.opponent, "_current_health", 0) > 0

        def opponent_attack_value() -> int:
            if isinstance(self.opponent, Monster):
                return self.opponent.attack
            # Boss attack depends on current phase
            phase = getattr(self.opponent, "_current_phase", 1)
            return (
                self.opponent.attack_phase1
                if phase == 1
                else self.opponent.attack_phase2
            )

        def apply_damage_to_opponent(damage: int) -> None:
            if isinstance(self.opponent, Monster):
                self.opponent.health = max(0, self.opponent.health - damage)
            else:  # Boss
                cur = getattr(self.opponent, "_current_health", 0) - damage
                if cur > 0:
                    self.opponent._current_health = cur
                else:
                    # Phase transition or defeat
                    if getattr(self.opponent, "_current_phase", 1) == 1:
                        # Switch to phase 2
                        self.opponent._current_phase = 2
                        self.opponent._current_health = self.opponent.health_phase2
                    else:
                        # Defeated
                        self.opponent._current_health = 0

        # Main combat loop: player attacks first each round.
        while not self.fled and player_alive() and opponent_alive():
            # Player attack (automatic)
            damage_to_opponent = max(
                0, self.player.attack - 0
            )  # monsters/bosses have no defense attribute
            apply_damage_to_opponent(damage_to_opponent)

            if not opponent_alive():
                break  # opponent defeated

            # Opponent retaliates
            opp_attack = opponent_attack_value()
            damage_to_player = max(0, opp_attack - self.player.defense)
            self.player.health = max(0, self.player.health - damage_to_player)

        # Combat ends here; state is reflected in player and opponent objects.

    def apply_player_action(self, action: dict) -> None:
        """Handle a player command during combat (e.g., 'attack', 'use').

        Args:
            action: Command dictionary as produced by ``parser.parse_command``.
        """
        cmd = action.get("action")
        if not cmd:
            raise ValueError("Action dictionary must contain an 'action' key")

        if cmd == "attack":
            # Direct player attack
            damage = max(0, self.player.attack - 0)
            if isinstance(self.opponent, Monster):
                self.opponent.health = max(0, self.opponent.health - damage)
            else:  # Boss
                cur = getattr(self.opponent, "_current_health", 0) - damage
                if cur > 0:
                    self.opponent._current_health = cur
                else:
                    if getattr(self.opponent, "_current_phase", 1) == 1:
                        self.opponent._current_phase = 2
                        self.opponent._current_health = self.opponent.health_phase2
                    else:
                        self.opponent._current_health = 0

        elif cmd == "use":
            item_id = action.get("item_id")
            if not item_id:
                raise ValueError("Use action requires 'item_id'")
            if item_id not in self.player.inventory:
                raise ValueError(f"Item '{item_id}' not in inventory")
            # Simplified healing: restore 5 HP up to max_health
            heal_amount = 5
            new_health = min(self.player.max_health, self.player.health + heal_amount)
            self.player.health = new_health
            # Remove used item from inventory
            self.player.inventory.remove(item_id)

        elif cmd == "flee":
            self.fled = True

        else:
            raise ValueError(f"Unsupported combat action: {cmd}")

    """Orchestrates a single combat session.

    >>> player = Player(...)
    >>> m = Monster(...)
    >>> engine = CombatEngine(player, m)
    ...
    """
