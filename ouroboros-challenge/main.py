"""Application entry point.

This module creates a :class:`~engine.GameEngine` instance and provides a
simple REPL that accepts textual commands, parses them with
:func:`parser.parse_command`, and invokes the corresponding engine methods.

The command set is intentionally minimal – it demonstrates how the core
components of the project can be wired together without requiring any
external dependencies.
"""

from __future__ import annotations

import sys
from typing import List

from engine import GameEngine
from parser import Command, parse_command
from models import Item, Settings, User


def _create_user(args: List[str], engine: GameEngine) -> None:
    """Create a new :class:`User` and add it to the engine.

    Expected arguments:
        id (int), username (str), email (str) [, full_name (str)]

    Example:
        create_user 1 alice alice@example.com "Alice Smith"
    """
    if len(args) < 3:
        print("Usage: create_user <id> <username> <email> [full_name]")
        return

    try:
        user_id = int(args[0])
    except ValueError:
        print("User id must be an integer.")
        return

    username = args[1]
    email = args[2]
    full_name = args[3] if len(args) > 3 else None

    user = User(id=user_id, username=username, email=email, full_name=full_name)
    engine.add_user(user)
    print(f"User {user_id} added.")


def _add_item(args: List[str], engine: GameEngine) -> None:
    """Create a new :class:`Item` and add it to the engine.

    Expected arguments:
        id (int), owner_id (int), name (str) [, description (str)]

    Example:
        add_item 10 1 "Sword of Truth" "A legendary blade."
    """
    if len(args) < 3:
        print("Usage: add_item  [description]")
        return

    try:
        item_id = int(args[0])
        owner_id = int(args[1])
    except ValueError:
        print("Item id and owner_id must be integers.")
        return

    name = args[2]
    description = args[3] if len(args) > 3 else None

    item = Item(id=item_id, owner_id=owner_id, name=name, description=description)
    engine.add_item(item)
    print(f"Item {item_id} added for user {owner_id}.")


def _list_users(engine: GameEngine) -> None:
    """Print a summary of all users currently stored in the engine."""
    if not engine._users:  # type: ignore[attr-defined]
        print("No users.")
        return

    for uid, user in engine._users.items():  # type: ignore[attr-defined]
        print(f"{uid}: {user.username} ({user.email})")


def _list_items(engine: GameEngine) -> None:
    """Print a summary of all items currently stored in the engine."""
    if not engine._items:  # type: ignore[attr-defined]
        print("No items.")
        return

    for iid, item in engine._items.items():  # type: ignore[attr-defined]
        print(f"{iid}: {item.name} (owner {item.owner_id})")


def _dispatch(command: Command, engine: GameEngine) -> bool:
    """Execute a parsed command.

    Returns ``True`` if the REPL should continue, ``False`` to exit.
    """
    name = command.name.lower()
    args = command.args

    if name in {"exit", "quit"}:
        return False
    elif name == "create_user":
        _create_user(args, engine)
    elif name == "add_item":
        _add_item(args, engine)
    elif name == "list_users":
        _list_users(engine)
    elif name == "list_items":
        _list_items(engine)
    elif name == "start":
        engine.start()
        print("Engine started.")
    elif name == "stop":
        engine.stop()
        print("Engine stopped.")
    else:
        print(f"Unknown command: {name}")
        print("Available commands: create_user, add_item, list_users, list_items, start, stop, exit")
    return True


def main() -> None:
    """Run the interactive command‑line interface."""
    engine = GameEngine(settings=Settings())  # type: ignore[arg-type]

    print("Welcome to the Game Engine REPL. Type 'exit' or 'quit' to leave.")
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not line:
            continue

        try:
            cmd = parse_command(line)
        except Exception as exc:  # pragma: no cover
            print(f"Failed to parse command: {exc}")
            continue

        if not _dispatch(cmd, engine):
            break


if __name__ == "__main__":
    sys.exit(main())
