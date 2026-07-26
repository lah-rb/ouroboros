from data_loader import load_world
from game_state import load_game
from engine import GameEngine
import sys


def main():
    # Check if we should load a saved game
    if len(sys.argv) > 1 and sys.argv[1] == "--load":
        saved_state = load_game()
        if saved_state:
            print("Loaded saved game.")
            world_data = load_world("world.yaml")
            engine = GameEngine(world_data)
            engine.state_manager.state = saved_state
            engine.run()
            return

    # Start new game
    try:
        world_data = load_world("world.yaml")
    except FileNotFoundError:
        print("Error: world.yaml not found. Please ensure the file exists.")
        sys.exit(1)

    engine = GameEngine(world_data)
    engine.run()


if __name__ == "__main__":
    main()
