"""Entry point that creates the game engine and starts the adventure."""

from __future__ import annotations

from engine import GameEngine
from world_loader import load_world


def main() -> None:
    """Load world data, instantiate the engine, and begin the game loop."""
    world = load_world()
    engine = GameEngine(world)
    engine.start()


if __name__ == "__main__":
    main()
