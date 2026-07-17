"""
Entry point for the adventure game.
"""

from .engine import GameEngine


def main() -> None:
    engine = GameEngine()
    engine.start()


if __name__ == "__main__":
    main()
