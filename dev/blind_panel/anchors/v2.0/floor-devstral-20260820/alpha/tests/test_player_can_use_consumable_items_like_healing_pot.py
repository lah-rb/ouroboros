import os
import sys
from engine import GameEngine
from parser import parse_command

def test_player_can_use_healing_potion():
    # Create a fresh game engine
    engine = GameEngine()
    engine.initialize_game()

    # Navigate to the throne room where the healing potion is located
    engine.handle_command(parse_command("move north"))
    engine.handle_command(parse_command("move west"))

    # Pick up the healing potion
    result = engine.handle_command(parse_command("take healing_potion"))
    assert "picked up Healing Potion" in result

    # Use the healing potion - this should fail with AttributeError before the fix
    initial_health = engine.game_state.player.health
    try:
        result = engine.handle_command(parse_command("use healing_potion"))
        # After the fix, this should succeed and restore health
        assert "restored" in result.lower()
        assert engine.game_state.player.health > initial_health
    except AttributeError as e:
        # This is expected before the fix
        assert str(e) == "Item object has no attribute 'item_type'"
        raise AssertionError("Expected AttributeError before fix") from e

# Clean up any created files
if os.path.exists("game_state.json"):
    os.remove("game_state.json")