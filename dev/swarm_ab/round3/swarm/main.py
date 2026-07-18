"""Entry point that creates the engine and starts the game."""

from src.engine import GameEngine

def main() -> None:
    """Instantiate GameEngine with default world file and start it."""
    engine = GameEngine()
    engine.start()

if __name__ == "__main__":
    main()
