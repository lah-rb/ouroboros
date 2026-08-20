import builtins

import pytest

def test_use_healing_item_restores_health_during_combat(monkeypatch):
    """
    Regression test for the bug where using a consumable item during combat
    does nothing. The test sets up a combat scenario, forces the player to use
    a healing item, and checks that:
      * the player's health is restored to max, and
      * the item is removed from the inventory.
    With the current buggy implementation this test fails (health stays low
    and the item remains), and it passes once the fix is applied.
    """

    # Input sequence: player name (for GameEngine init), then combat commands.
    inputs = iter(["TestPlayer", "use herb_tea", "flee"])
    monkeypatch.setattr(builtins, "input", lambda *args: next(inputs))

    # Import the engine after monkey‑patching input so the constructor uses our name.
    from game import GameEngine

    engine = GameEngine()

    # ------------------------------------------------------------------
    # Locate a room that contains a monster.
    # ------------------------------------------------------------------
    monster_room_id = None
    for rid, room in engine.rooms.items():
        if getattr(room, "monster", None):
            monster_room_id = rid
            break
    assert monster_room_id is not None, "World does not contain any monster rooms."

    # Place the player in that room.
    engine.player.location = monster_room_id

    # Ensure the monster will not deal damage (so we can isolate healing).
    monster_id = engine.rooms[monster_room_id].monster
    monster = engine.monsters[monster_id]
    monster.attack = 0

    # Reset mutable state for the room – make sure the monster is alive.
    engine._room_state(monster_room_id)["monster_defeated"] = False

    # ------------------------------------------------------------------
    # Add a healing consumable to the player's inventory.
    # ------------------------------------------------------------------
    item_id = "herb_tea"
    if item_id not in engine.items:
        # Fallback: pick any consumable that restores health.
        from entities import ItemType
        for iid, itm in engine.items.items():
            if (
                itm.type == ItemType.CONSUMABLE
                and int(itm.stats.get("heal", 0)) > 0
            ):
                item_id = iid
                break
    # The player now carries the item.
    engine.player.add_item(item_id)

    # Reduce health so that healing has an observable effect.
    engine.player.health = 50

    # ------------------------------------------------------------------
    # Run the combat loop. The mocked input will first issue "use herb_tea"
    # and then "flee" to exit the combat.
    # ------------------------------------------------------------------
    engine._handle_combat()

    # ------------------------------------------------------------------
    # Assertions – they fail with the buggy code and pass after the fix.
    # ------------------------------------------------------------------
    assert engine.player.health == engine.player.max_health, (
        "Health was not restored to maximum after using a healing item in combat."
    )
    assert item_id not in engine.player.inventory, (
        "Healing item was not consumed (still present in inventory) after use in combat."
    )