from engine import GameEngine
from saver import load_game

def main():
    engine = GameEngine()
    saved_game = load_game()

    if saved_game:
        print("Welcome back to the Gothic Castle Adventure!")
        print("Type 'continue' to resume your game or 'new' to start a new one.")
    else:
        print("Welcome to the Gothic Castle Adventure!")
        print("Type 'new' to start a new game.")

    while True:
        command = input("> ").strip().lower()

        if command == "quit":
            print("Goodbye!")
            return
        elif command in ["new", "start"]:
            engine.initialize_game()
            print("\nGame started!")
            print(engine.game_state.location.description)
            break
        elif command == "continue" and saved_game:
            # In a real implementation, we would restore the saved game state here
            print("Game continuation not yet implemented")
            break
        else:
            print("Please type 'new' to start a new game or 'continue' to load your saved game.")

    engine.run()

if __name__ == "__main__":
    main()
