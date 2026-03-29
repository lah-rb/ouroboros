#!/usr/bin/env python3
"""Main entry point for the text adventure game engine."""

import sys

from engine import GameEngine
from loader import load_world_from_yaml


def main():
    """Load world data and start the game loop."""
    try:
        world_data = load_world_from_yaml("world_data.yaml")
    except FileNotFoundError:
        print("Error: world_data.yaml not found. Please provide a valid YAML file.")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading world data: {e}")
        sys.exit(1)

    engine = GameEngine(world_data)
    
    # Initial description
    print(engine.process_command("look"))
    
    # Game loop
    while True:
        try:
            command = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        
        if not command:
            continue
            
        response = engine.process_command(command)
        print(response)
        
        if "quit" in response.lower() or "goodbye" in response.lower():
            break


if __name__ == "__main__":
    main()
