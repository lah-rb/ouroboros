"""
Entry point for the text adventure.
Shows a title screen, creates the Game instance, and starts the loop.
"""

import sys
from game import Game


def print_title() -> None:
    title = r"""
   _____          _                     _       
  / ____|        | |                   | |      
 | (___     ___  | |_   ___   __      _| |_ ___ 
  \___ \   / _ \ | __| / _ \  \ \ /\ / / __/ _ \
  ____) | |  __/ | |_ | (_) |  \ V  V /| ||  __/
 |_____/   \___|  \__| \___/    \_/\_/  \__\___|
                                                
"""
    print(title)
    print("Welcome to the Adventure!\n")
    input("Press Enter to begin...")


def main() -> None:
    print_title()
    game = Game()
    game.run()


if __name__ == "__main__":
    # Ensure the script can be invoked directly.
    sys.exit(main())
