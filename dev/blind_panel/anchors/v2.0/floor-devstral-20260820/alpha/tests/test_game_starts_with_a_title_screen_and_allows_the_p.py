import os
import sys
from io import StringIO
from unittest.mock import patch

# Add the project root to the path so imports work
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from engine import GameEngine

def test_game_starts_with_title_screen_and_allows_new_game():
    """Test that the game starts with a title screen and allows starting a new game."""
    # Create a fresh engine instance (simulates cold start)
    engine = GameEngine()

    # Simulate user input for 'new' command
    with patch('builtins.input', return_value='new'):
        try:
            # This should raise NameError because Player is not imported yet
            engine.initialize_game()
            assert False, "Expected NameError but game initialized successfully"
        except NameError as e:
            assert 'Player' in str(e), f"Expected NameError about Player, got: {e}"

    # After the fix (importing Player), this should work
    # We'll simulate the fix by adding the import dynamically
    from models import Player
    sys.modules['engine'].Player = Player

    engine = GameEngine()
    with patch('builtins.input', return_value='new'):
        try:
            engine.initialize_game()
            assert engine.game_state is not None, "Game state should be initialized"
            assert engine.game_state.player is not None, "Player should be created"
            assert engine.game_state.player.health == 30, "Player health should be 30"
            assert engine.game_state.player.attack == 5, "Player attack should be 5"
            assert engine.game_state.player.defense == 2, "Player defense should be 2"
        except Exception as e:
            assert False, f"Game initialization failed after fix: {e}"

if __name__ == '__main__':
    test_game_starts_with_title_screen_and_allows_new_game()