"""Serialize and deserialize :class:`GameState` to/from plain JSON files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from state import GameState, RoomState, NPCState, MonsterState
from entities import Player


def _state_to_dict(state: GameState) -> Dict[str, Any]:
    """Convert a :class:`GameState` into a JSON‑serializable dict.

    All custom objects are flattened to primitive Python structures
    (dicts, lists, ints, strings, bools).  This helper is part of the
    contract and must be used by ``save_game``.

    Returns:
        A dict ready for ``json.dump``.
    """

    # Helper that walks an arbitrary object graph and produces only JSON‑compatible
    # primitive types.  Known game‑specific classes are converted to dictionaries
    # containing their public attributes plus a ``_type`` marker so that the
    # complementary ``_dict_to_state`` routine can reconstruct the original objects.
    def _serialize(obj: Any) -> Any:
        # Primitive JSON types – return unchanged.
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj

        # Containers – recurse into their elements.
        if isinstance(obj, dict):
            return {k: _serialize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple, set)):
            return [_serialize(item) for item in obj]

        # Game‑specific objects – flatten their public attributes.
        if isinstance(obj, (GameState, RoomState, NPCState, MonsterState, Player)):
            result: Dict[str, Any] = {"_type": obj.__class__.__name__}
            for attr_name, attr_value in vars(obj).items():
                # Skip private/internal attributes that start with an underscore.
                if attr_name.startswith("_"):
                    continue
                result[attr_name] = _serialize(attr_value)
            return result

        # Fallback – represent unknown objects as their string form.
        return str(obj)

    return _serialize(state)


def _dict_to_state(data: Dict[str, Any]) -> GameState:
    """Reconstruct a :class:`GameState` from a plain dict produced by
    :func:`_state_to_dict`.

    All nested structures are re‑instantiated as the appropriate classes.

    Returns:
        A fully populated ``GameState`` instance.
    """
    # Basic validation -------------------------------------------------------
    if not isinstance(data, dict):
        raise ValueError("Input data must be a dictionary")

    # ------------------------------------------------------------------------
    # Helper to instantiate a class from a mapping when possible.
    # If the value is already an instance of the target class we keep it.
    # ------------------------------------------------------------------------
    def _make_instance(cls, payload):
        if isinstance(payload, cls):
            return payload
        if isinstance(payload, dict):
            try:
                return cls(**payload)
            except Exception as exc:
                raise ValueError(
                    f"Failed to instantiate {cls.__name__} from data {payload!r}: {exc}"
                ) from exc
        raise ValueError(f"Expected a mapping for {cls.__name__}, got {type(payload)}")

    # ------------------------------------------------------------------------
    # Required top‑level fields – the contract expects at least a player.
    # Additional collections (rooms, npcs, monsters) are optional but will be
    # reconstructed when present.
    # ------------------------------------------------------------------------
    if "player" not in data:
        raise ValueError("Missing required field 'player' in saved state")
    player = _make_instance(Player, data["player"])

    # Re‑create collections ---------------------------------------------------
    def _reconstruct_collection(raw, item_cls):
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"Expected a dict for collection of {item_cls.__name__}, got {type(raw)}"
            )
        result = {}
        for key, val in raw.items():
            result[key] = _make_instance(item_cls, val)
        return result

    rooms = _reconstruct_collection(data.get("rooms", {}), RoomState)
    npcs = _reconstruct_collection(data.get("npcs", {}), NPCState)
    monsters = _reconstruct_collection(data.get("monsters", {}), MonsterState)

    # Any other fields are passed straight through to GameState ----------
    extra_kwargs = {
        k: v
        for k, v in data.items()
        if k not in {"player", "rooms", "npcs", "monsters"}
    }

    # Construct the final GameState -----------------------------------------
    try:
        state = GameState(
            player=player, rooms=rooms, npcs=npcs, monsters=monsters, **extra_kwargs
        )
    except Exception as exc:
        raise ValueError(f"Failed to construct GameState from data: {exc}") from exc

    return state


def save_game(state: GameState, filename: str) -> None:
    """Write the current game state to ``filename`` in JSON format.

    The function serializes ``state`` using :func:`_state_to_dict` and
    writes UTF‑8 text.  Existing files are overwritten.

    Args:
        state: The mutable game snapshot to persist.
        filename: Destination path for the JSON file.

    Raises:
        OSError: If the file cannot be written.
    """
    # Convert the GameState into a JSON‑serializable dictionary.
    data = _state_to_dict(state)

    # Serialize the dictionary to a pretty‑printed JSON string.
    json_text = json.dumps(data, ensure_ascii=False, indent=4)

    # Write the JSON text to the target file, overwriting any existing content.
    Path(filename).write_text(json_text, encoding="utf-8")


def load_game(filename: str) -> GameState:
    """Read a JSON save file and return the reconstructed :class:`GameState`.

    Args:
        filename: Path to a previously saved JSON file.

    Returns:
        A new ``GameState`` instance reflecting the saved data.

    Raises:
        FileNotFoundError: If ``filename`` does not exist.
        json.JSONDecodeError: If the file contents are not valid JSON.
        ValueError: If required fields are missing after deserialization.
    """
    path = Path(filename)
    # Let FileNotFoundError propagate if the file does not exist
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)  # May raise json.JSONDecodeError

    if not isinstance(data, dict):
        raise ValueError("Top-level JSON object must be a dictionary")

    return _dict_to_state(data)
