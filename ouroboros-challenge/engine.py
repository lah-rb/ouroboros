"""Core game engine module.

Provides a lightweight in‑memory engine that manages users, items and
settings defined in :pymod:`models`.  The implementation is deliberately
simple – it stores objects in dictionaries and offers basic CRUD helpers.
It can be extended with persistence, event handling or a full game loop as
required by the application.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Import model classes from the project.  Guard against missing imports so
# that the engine can still be imported in isolation (e.g. during early
# development or testing).
try:
    from models import User, Item, Settings
except Exception:  # pragma: no cover
    User = Any  # type: ignore
    Item = Any  # type: ignore
    Settings = Any  # type: ignore


class GameEngine:
    """Manage users, items and global settings for the game.

    The engine stores objects in memory; it does not perform any I/O.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings: Settings | None = settings
        self._users: Dict[int, User] = {}
        self._items: Dict[int, Item] = {}

    # --------------------------------------------------------------------- #
    # User management
    # --------------------------------------------------------------------- #
    def add_user(self, user: User) -> None:
        """Add a new user to the engine.

        If a user with the same ``id`` already exists it will be overwritten.
        """
        self._users[user.id] = user

    def get_user(self, user_id: int) -> Optional[User]:
        """Retrieve a user by its identifier."""
        return self._users.get(user_id)

    def remove_user(self, user_id: int) -> None:
        """Remove a user from the engine.

        Also removes any items owned by this user.
        """
        if user_id in self._users:
            del self._users[user_id]
        # Clean up owned items
        items_to_remove = [item_id for item_id, item in self._items.items() if item.owner_id == user_id]
        for item_id in items_to_remove:
            del self._items[item_id]

    # --------------------------------------------------------------------- #
    # Item management
    # --------------------------------------------------------------------- #
    def add_item(self, item: Item) -> None:
        """Add a new item to the engine.

        Overwrites any existing item with the same ``id``.
        """
        if item.owner_id not in self._users:
            raise ValueError(f"Owner with id {item.owner_id} does not exist.")
        self._items[item.id] = item

    def get_item(self, item_id: int) -> Optional[Item]:
        """Retrieve an item by its identifier."""
        return self._items.get(item_id)

    def get_items_by_user(self, user_id: int) -> List[Item]:
        """Return a list of all items owned by the given user."""
        return [item for item in self._items.values() if item.owner_id == user_id]

    def remove_item(self, item_id: int) -> None:
        """Remove an item from the engine."""
        if item_id in self._items:
            del self._items[item_id]

    # --------------------------------------------------------------------- #
    # Engine lifecycle
    # --------------------------------------------------------------------- #
    def start(self) -> None:
        """Placeholder for starting any background processes.

        Currently a no‑op; present for API completeness.
        """
        # No background work required for the in‑memory engine.
        pass

    def stop(self) -> None:
        """Placeholder for stopping background processes.

        Currently a no‑op; present for API completeness.
        """
        # No background work to clean up.
        pass


__all__: List[str] = ["GameEngine"]
