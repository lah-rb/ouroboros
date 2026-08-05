"""Serialize and deserialize the full GameState to/from JSON files."""

from __future__ import annotations
import json
from src.models import GameState, Player, Item, Room, NPC, Monster


def save_game(state: GameState, path: str) -> None:
    """Write the current GameState to ``path`` as a JSON document.

    The JSON structure must match the canonical GameState schema defined
    in the data contracts.

    Args:
        state: The GameState to persist.
        path: Destination file path.

    Returns:
        None.

    Raises:
        OSError: If the file cannot be written.
    """

    def _serialize(obj):
        """Recursively turn dataclass instances into JSON‑serialisable dicts."""
        # Primitive types
        if obj is None or isinstance(obj, (str, int, float, bool)):
            return obj

        # Containers
        if isinstance(obj, list):
            return [_serialize(item) for item in obj]
        if isinstance(obj, dict):
            return {key: _serialize(value) for key, value in obj.items()}

        # Model objects – handle each known class explicitly
        if isinstance(obj, Item):
            return {
                "id": obj.id,
                "name": obj.name,
                "description": obj.description,
                "type": obj.type,
            }

        if isinstance(obj, Monster):
            return {
                "id": obj.id,
                "name": obj.name,
                "health": obj.health,
                "max_health": obj.max_health,
                "attack": obj.attack,
                "description": obj.description,
            }

        if isinstance(obj, NPC):
            return {
                "id": obj.id,
                "name": obj.name,
                "location": obj.location,
                "dialogue": obj.dialogue,
                "dialogue_index": obj.dialogue_index,
            }

        if isinstance(obj, Room):
            return {
                "id": obj.id,
                "name": obj.name,
                "description": obj.description,
                "connections": obj.connections,
                "items": _serialize(obj.items),
                "npcs": _serialize(obj.npcs),
                "monster": _serialize(obj.monster) if obj.monster is not None else None,
            }

        if isinstance(obj, Player):
            return {
                "location": obj.location,
                "health": obj.health,
                "max_health": obj.max_health,
                "attack": obj.attack,
                "defense": obj.defense,
                "inventory": _serialize(obj.inventory),
                "equipped": {
                    "weapon": _serialize(obj.equipped.get("weapon")),
                    "armor": _serialize(obj.equipped.get("armor")),
                },
            }

        if isinstance(obj, GameState):
            return {
                "player": _serialize(obj.player),
                "rooms": {rid: _serialize(room) for rid, room in obj.rooms.items()},
                "npcs": {nid: _serialize(npc) for nid, npc in obj.npcs.items()},
                "monsters": {
                    mid: _serialize(monster) for mid, monster in obj.monsters.items()
                },
            }

        # Fallback – should not occur with the defined model
        return str(obj)

    # Build a serialisable representation of the whole game state
    data = _serialize(state)

    # Write JSON to the specified path; any OSError propagates as required
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=2)


def load_game(path: str) -> GameState:
    """Read a JSON save file and reconstruct the GameState.

    Args:
        path: Path to a JSON file produced by ``save_game``.

    Returns:
        A GameState instance reflecting the saved data.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        json.JSONDecodeError: If the file contents are invalid.
    """
    # Open the file – let FileNotFoundError propagate if the path is invalid.
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)  # May raise json.JSONDecodeError for malformed JSON.

    # ----- Reconstruct Player -------------------------------------------------
    player_data = data["player"]
    inventory = [Item(**itm) for itm in player_data.get("inventory", [])]

    equipped_raw = player_data.get("equipped", {})
    equipped = {
        "weapon": (
            Item(**equipped_raw["weapon"]) if equipped_raw.get("weapon") else None
        ),
        "armor": Item(**equipped_raw["armor"]) if equipped_raw.get("armor") else None,
    }

    player = Player(
        location=player_data["location"],
        health=player_data["health"],
        max_health=player_data["max_health"],
        attack=player_data["attack"],
        defense=player_data["defense"],
        inventory=inventory,
        equipped=equipped,
    )

    # ----- Reconstruct Rooms --------------------------------------------------
    rooms: dict[str, Room] = {}
    for room_id, room_data in data.get("rooms", {}).items():
        items = [Item(**itm) for itm in room_data.get("items", [])]
        npcs = [NPC(**npc) for npc in room_data.get("npcs", [])]

        monster_raw = room_data.get("monster")
        monster = Monster(**monster_raw) if monster_raw is not None else None

        room = Room(
            id=room_data["id"],
            name=room_data["name"],
            description=room_data["description"],
            connections=room_data.get("connections", {}),
            items=items,
            npcs=npcs,
            monster=monster,
        )
        rooms[room_id] = room

    # ----- Reconstruct Top‑Level NPCs -----------------------------------------
    npcs: dict[str, NPC] = {}
    for npc_id, npc_data in data.get("npcs", {}).items():
        npcs[npc_id] = NPC(**npc_data)

    # ----- Reconstruct Monsters ------------------------------------------------
    monsters: dict[str, Monster] = {}
    for mon_id, mon_data in data.get("monsters", {}).items():
        monsters[mon_id] = Monster(**mon_data)

    # ----- Assemble GameState -------------------------------------------------
    return GameState(player=player, rooms=rooms, npcs=npcs, monsters=monsters)
