"""Entry point for the adventure game."""

from __future__ import annotations

from ui import display_title
from game import GameEngine


def main() -> None:
    """Display the title screen and start the interactive game loop."""
    display_title()
    engine = GameEngine()
    engine.run()


if __name__ == "__main__":
    main()
