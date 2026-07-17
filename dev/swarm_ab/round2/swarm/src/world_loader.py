"""Loads static world definitions from YAML into model objects."""

from __future__ import annotations

import os
from typing import Dict, Any

import yaml

# Architecture‑declared imports (required for downstream modules)
# The imports are kept even if not used directly here to satisfy the
# contract that these symbols are available from this module.
try:
    from src import Room, Item, NPC, Monster  # noqa: F401
except Exception:  # pragma: no cover
    # In isolated test environments the full package may not be importable.
    # The import is optional for the loader itself; downstream code will
    # import the concrete classes where needed.
    pass


def load_world(path: str = "data/world.yaml") -> Dict[str, Any]:
    """Parse a world definition YAML file and return its raw sections.

    The returned mapping contains the top‑level keys ``rooms``, ``items``,
    ``npcs``, and ``monsters`` exactly as read from YAML. Converting those
    entries into :class:`Room`, :class:`Item`, etc. is the responsibility of
    the caller.

    Args:
        path: Filesystem path to a YAML file. If omitted, defaults to
            ``data/world.yaml`` relative to the project root.

    Returns:
        Dictionary with keys ``rooms``, ``items``, ``npcs``, ``monsters``.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        yaml.YAMLError: If the file cannot be parsed.
        KeyError: If any required top‑level key is missing.

    >>> import tempfile, textwrap, os
    >>> tmp = tempfile.NamedTemporaryFile(delete=False, mode='w')
    >>> _ = tmp.write(textwrap.dedent('''\
    ... rooms:
    ...   - id: test
    ...     name: Test Room
    ...     description: "Just a test."
    ...     connections: {}
    ... items: []
    ... npcs: []
    ... monsters: []'''))
    >>> tmp.close()
    >>> data = load_world(tmp.name)
    >>> isinstance(data, dict) and 'rooms' in data
    True
    >>> os.unlink(tmp.name)
    """
    # Ensure the path points to an existing file before attempting to open.
    if not os.path.isfile(path):
        raise FileNotFoundError(f"No such file or directory: '{path}'")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # ``yaml.safe_load`` returns ``None`` for empty files; treat that as an
    # invalid world definition.
    if raw is None:
        raise yaml.YAMLError("YAML file is empty or could not be parsed")

    # Validate required top‑level keys exist
    for key in ("rooms", "items", "npcs", "monsters"):
        if key not in raw:
            raise KeyError(f"Missing top‑level key '{key}' in world file")
    return raw
