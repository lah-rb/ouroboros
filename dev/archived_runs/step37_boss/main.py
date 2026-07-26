import json
import os
import time
from engine import GameEngine
from parser import parse_command, CommandType
from loader import load_world


def main():
    world_file = "world.yaml"
    save_file = "savegame.json"

    # Initialize game world
    world_data = load_world(world_file)
    engine = GameEngine(world_data)
    session_start = time.time()

    print("Welcome to the game!")
    print(engine.describe_room())

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not raw:
            continue

        cmd = parse_command(raw)

        if cmd.type == CommandType.QUIT:
            print("Goodbye!")
            break
        elif cmd.type == CommandType.SAVE:
            total_time = engine.playtime_seconds + (time.time() - session_start)
            save_data = {
                "player": {
                    "location": engine.player.location,
                    "health": engine.player.health,
                    "max_health": engine.player.max_health,
                    "base_attack": engine.player.base_attack,
                    "inventory": list(engine.player.inventory),
                    "equipment": dict(engine.player.equipment),
                    "flags": dict(engine.player.flags),
                },
                "current_room_id": engine.player.location,
                "room_states": {
                    rid: dict(state) for rid, state in engine.room_states.items()
                },
                "npc_states": {
                    nid: dict(state) for nid, state in engine.npc_states.items()
                },
                "defeated_monsters": list(engine.defeated_monsters),
                "playtime_seconds": total_time,
            }
            with open(save_file, "w") as f:
                json.dump(save_data, f, indent=2)
            print("Game saved.")
        elif cmd.type == CommandType.LOAD:
            if not os.path.exists(save_file):
                print("No save file found.")
                continue
            with open(save_file, "r") as f:
                save_data = json.load(f)
            world_data = load_world(world_file)
            engine = GameEngine(world_data)
            engine.player.location = save_data["player"]["location"]
            engine.player.health = save_data["player"]["health"]
            engine.player.max_health = save_data["player"]["max_health"]
            engine.player.base_attack = save_data["player"]["base_attack"]
            engine.player.inventory = list(save_data["player"]["inventory"])
            engine.player.equipment = dict(save_data["player"]["equipment"])
            engine.player.flags = dict(save_data["player"]["flags"])
            engine.room_states = {
                rid: dict(state) for rid, state in save_data["room_states"].items()
            }
            engine.npc_states = {
                nid: dict(state) for nid, state in save_data["npc_states"].items()
            }
            engine.defeated_monsters = list(save_data["defeated_monsters"])
            engine.playtime_seconds = save_data["playtime_seconds"]
            session_start = time.time()
            print("Game loaded.")
            print(engine.describe_room())
        else:
            output = engine.execute_command(cmd) or []
            for line in output:
                print(line)

            # Check for player death
            if engine.player.health <= 0:
                total_time = engine.playtime_seconds + (time.time() - session_start)
                print(f"\nPlaytime: {total_time:.1f} seconds")
                while True:
                    restart = input("Restart? (y/n): ").strip().lower()
                    if restart == "y":
                        world_data = load_world(world_file)
                        engine = GameEngine(world_data)
                        session_start = time.time()
                        print("Game restarted.")
                        print(engine.describe_room())
                        break
                    elif restart == "n":
                        print("Goodbye!")
                        return
                    else:
                        print("Please enter 'y' or 'n'.")


if __name__ == "__main__":
    main()
