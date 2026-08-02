#!/usr/bin/env python3
"""The Ashen Keep -- a turn-based text adventure.

Run with:
    python main.py
or:
    python3 main.py

See README.md in this directory for the full command reference and a
description of the game world.
"""

from adventure.game import Game


def main():
    game = Game()
    game.run()


if __name__ == "__main__":
    main()
