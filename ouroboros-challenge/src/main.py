"""Main entry point for the text adventure game."""

import sys
from loader import WorldLoader
from engine import GameEngine
from parser import CommandParser


def main():
    """Run the text adventure game."""
    # Load world data
    loader = WorldLoader()
    try:
        loader.load_from_yaml("world_data.yaml")
    except FileNotFoundError:
        print("Error: world_data.yaml not found. Please create a world data file.")
        sys.exit(1)
    
    # Initialize game engine
    engine = GameEngine(loader)
    parser = CommandParser()
    engine.parser = parser
    
    # Show welcome message and initial room
    print("\n" + "=" * 50)
    print("Welcome to the Text Adventure Engine!")
    print("=" * 50)
    print(engine.describe_current_room())
    
    # Game loop
    while True:
        try:
            user_input = input("\n> ").strip()
            
            if not user_input:
                continue
            
            command, argument = parser.parse(user_input)
            
            if command == "go":
                if argument:
                    print(engine.move(argument))
                else:
                    print("Go where? (north, south, east, west)")
            
            elif command == "take":
                if argument:
                    print(engine.take_item(argument))
                else:
                    print("Take what?")
            
            elif command == "drop":
                if argument:
                    print(engine.drop_item(argument))
                else:
                    print("Drop what?")
            
            elif command == "use":
                if argument:
                    print(engine.use_item(argument))
                else:
                    print("Use what?")
            
            elif command == "examine":
                print(engine.examine(argument or ""))
            
            elif command == "talk":
                if argument:
                    print(engine.talk_to_npc(argument))
                else:
                    print("Talk to whom?")
            
            elif command == "inventory":
                print(engine.show_inventory())
            
            elif command == "look":
                print(engine.describe_current_room())
            
            elif command == "help":
                print(engine.show_help())
            
            elif command == "quit":
                print("Thanks for playing!")
                break
            
            elif command == "unknown":
                print("I don't understand that command. Type 'help' for available commands.")
            
        except KeyboardInterrupt:
            print("\nThanks for playing!")
            break
        except EOFError:
            print("\nThanks for playing!")
            break


if __name__ == "__main__":
    main()
