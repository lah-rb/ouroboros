import os
import sys
from engine import GameEngine
from parser import parse_command

def test_examine_command_fails_before_fix():
    """Test that the examine command fails before the fix is applied."""
    # Create a fresh game engine
    engine = GameEngine()
    engine.initialize_game()

    # Test examining an item in the room (should fail with current code)
    result = engine.handle_command(parse_command("examine rusted_broadsword"))
    assert result == "I don't understand that command."

    # Test examining an NPC in the room (should fail with current code)
    result = engine.handle_command(parse_command("examine knight"))
    assert result == "I don't understand that command."

def test_examine_command_works_after_fix():
    """Test that the examine command works after the fix is applied."""
    # Create a fresh game engine
    engine = GameEngine()
    engine.initialize_game()

    # Test examining an item in the room (should work after fix)
    result = engine.handle_command(parse_command("examine rusted_broadsword"))
    assert "rusted_broadsword" in result.lower() or "broadsword" in result.lower()

    # Test examining an NPC in the room (should work after fix)
    result = engine.handle_command(parse_command("examine knight"))
    assert "knight" in result.lower()

def test_examine_nonexistent_item():
    """Test examining a non-existent item returns appropriate message."""
    engine = GameEngine()
    engine.initialize_game()

    result = engine.handle_command(parse_command("examine nonexistent_item"))
    assert "no" in result.lower() or "not here" in result.lower() or "not present" in result.lower()

if __name__ == "__main__":
    # Run tests
    test_examine_command_fails_before_fix()
    print("All tests passed (this should fail before the fix is applied)")