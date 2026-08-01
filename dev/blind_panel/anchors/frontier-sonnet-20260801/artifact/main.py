#!/usr/bin/env python3
"""Entry point for The Sunstone Wyrm -- a small text adventure.

Run it with:

    python main.py

from this directory (or point at this file from anywhere; paths inside
the game are resolved relative to this file, not the current working
directory). See README.md for a full command reference.
"""

from game.engine import GameEngine


def main():
    engine = GameEngine()
    engine.start()


if __name__ == "__main__":
    main()
