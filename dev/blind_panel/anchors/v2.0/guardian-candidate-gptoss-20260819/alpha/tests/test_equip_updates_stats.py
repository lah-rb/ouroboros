import builtins

import pytest

from game import GameEngine


def test_equip_weapon_updates_player_attack(monkeypatch):
    """
    Regression test for the equip command.

    The bug: equipping a weapon records the item ID but does not modify the
    player's attack (or defense) stats according to the item's stats.
    This test asserts that after equipping a weapon with a positive attack bonus,
    the player's attack value is increased accordingly.
    """

    # Mock the input() call that asks for the character name during GameEngine init.
    monkeypatch.setattr(builtins, "input", lambda _: "Tester")

    # Initialise the game engine (loads world data, creates player, etc.).
    engine = GameEngine()
    player = engine.player

    # Record the baseline stats before equipping anything.
    base_attack = player.attack
    base_defense = player.defense

    # Find a weapon in the loaded world that provides a non‑zero attack bonus.
    weapon_id = None
    weapon_attack_bonus = 0
    for item_id, item in engine.items.items():
        if getattr(item, "type", None) == "weapon":
            attack_bonus = item.stats.get("attack", 0)
            if attack_bonus > 0:
                weapon_id = item_id
                weapon_attack_bonus = attack_bonus
                break

    # Ensure the test data actually contains such a weapon.
    assert weapon_id is not None, "Test requires at least one weapon with a positive attack bonus."

    # Make sure the player has the weapon in their inventory before equipping.
    if weapon_id not in player.inventory:
        player.add_item(weapon_id)

    # Perform the equip action using the internal method (the same code path the CLI uses).
    engine._equip_item(weapon_id)

    # After equipping, the player's attack should have increased by the weapon's bonus.
    expected_attack = base_attack + weapon_attack_bonus
    assert player.attack == expected_attack, (
        f"Equipping weapon '{weapon_id}' did not update attack stat: "
        f"expected {expected_attack}, got {player.attack}"
    )

    # Defense should remain unchanged because we equipped a weapon, not armor.
    assert player.defense == base_defense, (
        f"Equipping a weapon altered defense unexpectedly: "
        f"expected {base_defense}, got {player.defense}"
    )