"""Program entry point that loads world data and starts the game."""

from __future__ import annotations

import sys

# Architecture‑declared imports
from src.engine import run_engine
from src.world_loader import load_world
from src.save_load import load_game, GameState
from src.models import Player  # Added import for Player class


def main() -> None:
    """Initialize the game and launch the engine.

    The function attempts to load a saved game if a path is supplied as the
    first command‑line argument; otherwise it loads the static world from
    ``data/world.yaml`` and constructs an initial :class:`GameState`.

    After preparation, it calls :func:`run_engine` with the state.

    Returns:
        Nothing; exits when the engine terminates.
    """
    if len(sys.argv) > 1:
        save_path = sys.argv[1]
        state = load_game(save_path)
    else:
        world_data = load_world("data/world.yaml")
        # Minimal placeholder: construct a basic GameState from world_data.
        # Full construction is the responsibility of the implementer.
        state = GameState(
            player_location=world_data["rooms"][0]["id"],
            player=Player(health=20, max_health=20, attack=2, defense=1),
            rooms={},
            npcs={},
            monsters={},
            turn_counter=0,
            game_over=False,
            victory=False,
        )
    run_engine(state)


if __name__ == "__main__":
    main()
