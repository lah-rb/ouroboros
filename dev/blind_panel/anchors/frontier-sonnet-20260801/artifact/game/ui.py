"""Presentation helpers: the title screen and the win/lose end screens.

Kept separate from engine.py so all of the game's "voice" -- the framing
text a player sees before and after a run -- lives in one place.
"""

WIDTH = 70


def divider(char="-", width=WIDTH):
    return char * width


def header(text):
    return f"\n-- {text} --"


def title_screen():
    """Show the title screen and return "new" or "load" based on the
    player's choice. Raises SystemExit if they choose to quit here."""
    print(divider("="))
    print()
    print("               T H E   S U N S T O N E   W Y R M")
    print("                     a text adventure")
    print()
    print(divider("="))
    print()
    print("A wyrm has claimed the mountain above your village, and no one who")
    print("has gone to fight it has returned. The old folk still swap rumors")
    print("over ale about a stone that remembers the sun, hidden somewhere in")
    print("the hills past the dark forest. Rumors are all you have to go on.")
    print()
    print("  [1] New Game")
    print("  [2] Load Game")
    print("  [3] Quit")
    print()

    while True:
        choice = input("Choose an option (1-3): ").strip().lower()
        if choice in ("1", "new", "n", "new game"):
            return "new"
        if choice in ("2", "load", "l", "load game"):
            return "load"
        if choice in ("3", "quit", "q", "exit"):
            print("\nFarewell.")
            raise SystemExit(0)
        print("Please choose 1, 2, or 3.")


def victory_screen():
    return f"""
{divider("=")}
                          V I C T O R Y
{divider("=")}

The Ashwyrm's molten heart dims and goes dark. Its final roar rattles
loose stone from the cavern ceiling, and then the mountain is silent for
the first time in a generation. Sunlight -- real sunlight -- finds its
way in through the widening cracks.

You have saved the village.
{divider("=")}
"""


def defeat_screen():
    return f"""
{divider("=")}
                  Y O U   H A V E   F A L L E N
{divider("=")}

Your strength gives out, and the dark closes in. Whatever becomes of the
village now, it will not be because of anything more you can do.

You did not survive this journey.
{divider("=")}
"""
