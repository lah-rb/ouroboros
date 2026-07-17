"""IO utilities for loading world data and persisting game state."""

import json
from pathlib import Path
from typing import Dict, Any

import yaml


def load_world(path: str) -> Dict[str, Any]:
    """Parse a YAML world definition file.

    Args:
        path: Filesystem path to the YAML file.

    Returns:
        A dictionary matching the Data Contract described in the blueprint
        (keys: rooms, items, npcs, monsters, boss, start_room, boss_room).

    Raises:
        FileNotFoundError: If the file does not exist.
        yaml.YAMLError: If the file cannot be parsed.

    >>> import tempfile, textwrap, os, yaml, json; \
    ... tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.yaml'); \
    ... _ = tmp.write(textwrap.dedent('''\\\nrooms: []\\nitems: []\\nnpcs: []\\nmonsters: []\\nboss: {}\\nstart_room: ""\\nboss_room: ""\\n''').encode()); \
    ... tmp.close(); \
    ... isinstance(load_world(tmp.name), dict)
    True
    """
    file_path = Path(path)

    # Ensure the file exists; raise FileNotFoundError otherwise.
    if not file_path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    try:
        with file_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError:
        # Propagate YAML parsing errors as specified.
        raise

    # The world definition must be a mapping; otherwise treat it as a parse error.
    if not isinstance(data, dict):
        raise yaml.YAMLError("YAML content does not represent a dictionary")

    return data


def save_state(state: Dict[str, Any], path: str) -> None:
    """Serialize the full game state to a JSON file.

    Args:
        state: The persisted_save_state mapping.
        path: Destination file path.

    Raises:
        OSError: If the file cannot be written.

    >>> import tempfile, json; \
    ... tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.json'); \
    ... save_state({'player': {}}, tmp.name); \
    ... isinstance(json.load(open(tmp.name)), dict)
    True
    """
    # Write the state dictionary to the given path as JSON.
    # Any I/O error (e.g., permission issues, missing directories) will naturally
    # raise an OSError (or subclass), satisfying the contract.
    with open(path, "w") as f:
        json.dump(state, f)


def load_state(path: str) -> Dict[str, Any]:
    """Deserialize a previously saved game state from JSON.

    Example:
        >>> load_state('p')
        Traceback (most recent call last):
            ...
        FileNotFoundError: No such file: p

    Args:
        path: Path to the JSON file.

    Returns:
        The ``persisted_save_state`` mapping.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    file_path = Path(path)

    if not file_path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    with file_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    return data
