from engine import GameEngine
from loader import load_world, load_player


def main():
    world_data = load_world()
    player_data = load_player()
    engine = GameEngine(world_data, player_data)
    engine.run()


if __name__ == "__main__":
    main()
