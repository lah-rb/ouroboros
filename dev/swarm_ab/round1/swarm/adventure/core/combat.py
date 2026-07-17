"""Turn‑based combat engine handling player‑monster encounters."""

from dataclasses import dataclass
from typing import Optional

from adventure.models.entities import Player, Monster, Item


@dataclass
class CombatResult:
    """Outcome of a single combat action.

    Attributes:
        outcome: One of "ongoing", "player_victory", "monster_victory".
        player_health: Player's health after the action.
        monster_health: Monster's health after the action.
        description: Human‑readable narration of what happened.

    >>> r = CombatResult(outcome="ongoing", player_health=90, monster_health=5, description="You strike.")
    >>> r.outcome
    'ongoing'
    """

    outcome: str
    player_health: int
    monster_health: int
    description: str


class CombatEngine:
    """Encapsulates a combat session between a Player and a Monster.

    Args:
        player: The current Player instance.
        monster: The Monster the player is fighting.

    Methods:
        resolve_action(action: str) -> CombatResult:
            Process a player action ("attack", "use <item>", "flee") and
            return the resulting combat state.

    Raises:
        ValueError: If an unsupported action string is supplied.

    >>> dummy_player = Player(name="Hero")
    >>> dummy_monster = Monster(id="gob", name="Goblin", description="", health=5, attack=1, defense=0, behavior="aggressive", loot=[])
    >>> engine = CombatEngine(dummy_player, dummy_monster)
    >>> isinstance(engine.resolve_action("attack"), CombatResult)
    True
    """

    def __init__(self, player: Player, monster: Monster) -> None:
        self.player = player
        self.monster = monster

    def resolve_action(self, action: str) -> CombatResult:
        """Resolve a combat action and return the resulting CombatResult."""
        act = action.strip().lower()

        if act == "attack":
            # Player attacks monster
            damage_to_monster = max(
                0,
                getattr(self.player, "attack", 0) - getattr(self.monster, "defense", 0),
            )
            self.monster.health = max(0, self.monster.health - damage_to_monster)

            # Monster defeated?
            if self.monster.health == 0:
                description = (
                    f"{self.player.name} attacks and defeats the {self.monster.name}."
                )
                return CombatResult(
                    outcome="player_victory",
                    player_health=self.player.health,
                    monster_health=0,
                    description=description,
                )

            # Monster retaliates
            damage_to_player = max(
                0,
                getattr(self.monster, "attack", 0) - getattr(self.player, "defense", 0),
            )
            self.player.health = max(0, self.player.health - damage_to_player)

            # Player defeated?
            if self.player.health == 0:
                description = f"The {self.monster.name} strikes back and defeats {self.player.name}."
                return CombatResult(
                    outcome="monster_victory",
                    player_health=0,
                    monster_health=self.monster.health,
                    description=description,
                )

            description = (
                f"{self.player.name} attacks dealing {damage_to_monster} damage. "
                f"The {self.monster.name} retaliates dealing {damage_to_player} damage."
            )
            return CombatResult(
                outcome="ongoing",
                player_health=self.player.health,
                monster_health=self.monster.health,
                description=description,
            )

        if act.startswith("use "):
            # Use an item from inventory
            item_name = act[4:].strip()
            inventory = getattr(self.player, "inventory", [])
            item: Optional[Item] = next(
                (
                    it
                    for it in inventory
                    if getattr(it, "name", "").lower() == item_name
                ),
                None,
            )
            if not item:
                description = f"{self.player.name} does not have a {item_name}."
                return CombatResult(
                    outcome="ongoing",
                    player_health=self.player.health,
                    monster_health=self.monster.health,
                    description=description,
                )

            healing = getattr(item, "healing", 0)
            if healing > 0:
                max_hp = getattr(self.player, "max_health", self.player.health)
                self.player.health = min(max_hp, self.player.health + healing)

                # Consume the item
                if hasattr(inventory, "remove"):
                    inventory.remove(item)

                description = f"{self.player.name} uses {item.name} and recovers {healing} health."
                return CombatResult(
                    outcome="ongoing",
                    player_health=self.player.health,
                    monster_health=self.monster.health,
                    description=description,
                )

            description = f"{self.player.name} uses {item.name}, but nothing happens."
            return CombatResult(
                outcome="ongoing",
                player_health=self.player.health,
                monster_health=self.monster.health,
                description=description,
            )

        if act == "flee":
            # Attempt to flee
            behavior = getattr(self.monster, "behavior", "").lower()
            if behavior != "aggressive":
                description = f"{self.player.name} successfully flees from the {self.monster.name}."
                return CombatResult(
                    outcome="player_victory",
                    player_health=self.player.health,
                    monster_health=self.monster.health,
                    description=description,
                )

            # Monster gets a free attack
            damage_to_player = max(
                0,
                getattr(self.monster, "attack", 0) - getattr(self.player, "defense", 0),
            )
            self.player.health = max(0, self.player.health - damage_to_player)

            if self.player.health == 0:
                description = f"{self.player.name} fails to flee and is slain by the {self.monster.name}."
                return CombatResult(
                    outcome="monster_victory",
                    player_health=0,
                    monster_health=self.monster.health,
                    description=description,
                )

            description = (
                f"{self.player.name} fails to flee. The {self.monster.name} attacks "
                f"dealing {damage_to_player} damage."
            )
            return CombatResult(
                outcome="ongoing",
                player_health=self.player.health,
                monster_health=self.monster.health,
                description=description,
            )

        raise ValueError(f"Unsupported action: {action}")
