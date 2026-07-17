"""Command‑line entry point that runs the text adventure game loop."""

from __future__ import annotations


from world_loader import load_world
from state import GameState
from parser import parse_command, Command


def main() -> None:
    """Initialize the game, process player input, and coordinate modules.

    The function performs the following high‑level steps:
        1. Load world definitions from ``world.yaml``.
        2. Create an initial :class:`GameState`.
        3. Enter a REPL loop reading commands, parsing them, and invoking
           appropriate handlers (movement, inventory, combat, etc.).
        4. On ``quit`` or fatal error, exit cleanly.

    No arguments are read from ``sys.argv`` for this demo.

    Raises:
        RuntimeError: If world loading fails or essential data is missing.
    """
    # -----------------------------------------------------------------
    # 1. Load the world definition file.
    # -----------------------------------------------------------------
    try:
        # The world loader expects a path to the YAML file and returns the
        # raw world data structure required by the rest of the engine.
        world_data = load_world("world.yaml")
    except Exception as exc:  # pragma: no cover – defensive, exact type unknown
        raise RuntimeError("Failed to load world definitions") from exc

    # -----------------------------------------------------------------
    # 2. Initialise the game state.
    # -----------------------------------------------------------------
    try:
        # Most implementations accept the raw world data as the first
        # positional argument; if that signature does not match we fall back
        # to a no‑argument constructor.
        state = GameState(world_data)  # type: ignore[arg-type]
    except TypeError:
        try:
            state = GameState()
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Failed to initialise GameState") from exc

    # -----------------------------------------------------------------
    # 3. REPL loop – read, parse and dispatch commands.
    # -----------------------------------------------------------------
    while True:
        try:
            raw_input = input("> ")
        except EOFError:
            # End‑of‑file (e.g., Ctrl‑D) terminates the game gracefully.
            print("\nGoodbye.")
            break
        except KeyboardInterrupt:
            # Ctrl‑C also ends the session cleanly.
            print("\nGoodbye.")
            break

        # Skip empty lines.
        if not raw_input.strip():
            continue

        try:
            command: Command = parse_command(raw_input)
        except Exception as exc:  # pragma: no cover – parsing errors are fatal
            print(f"Error parsing command: {exc}")
            continue

        # A simple convention: the Command object provides a ``name`` attribute.
        # The demo only needs to recognise ``quit``/``exit``; all other commands
        # are ignored (real handlers would be invoked here).
        cmd_name = getattr(command, "name", "").lower()
        if cmd_name in ("quit", "exit"):
            print("Goodbye.")
            break

        # Placeholder for future command handling – keep the loop alive.
        # Real implementations would dispatch to movement, inventory,
        # combat, etc., using the ``state`` object and other modules.

    # -----------------------------------------------------------------
    # 4. Clean exit – any required teardown would happen here.
    # -----------------------------------------------------------------
    return None


if __name__ == "__main__":
    main()
