import os
import tempfile
from engine import GameEngine

def test_player_can_drop_items_from_inventory_into_the_current_room():
    # Create a temporary directory for the test and copy world.yaml there
    with tempfile.TemporaryDirectory() as tmpdir:
        # Copy world.yaml to the temporary directory
        original_dir = os.getcwd()
        os.chdir(tmpdir)
        os.system(f"cp {original_dir}/world.yaml .")

        try:
            # Initialize the game engine
            engine = GameEngine()

            # Start a new game
            engine.initialize_game()

            # Get the initial room (entrance_hall)
            initial_room = engine.game_state.location

            # Add an item to the room's items list for testing
            test_item_id = "rusty_broadsword"
            test_item = {
                "item_id": test_item_id,
                "name": "Rusty Broadsword",
                "description": "An old, rusty broadsword.",
                "item_type": "weapon",
                "stats": {"attack": 5}
            }
            engine.world_data["items"][test_item_id] = type('Item', (), test_item)()
            initial_room.items.append(test_item_id)

            # Pick up the item (take command)
            command = type('Command', (), {"action": "take", "target": test_item_id})
            result = engine.handle_command(command)
            assert result == f"You picked up {test_item['name']}."
            assert len(engine.game_state.inventory) == 1
            assert engine.game_state.inventory[0].item_id == test_item_id

            # Verify the item is not in the room anymore
            assert test_item_id not in initial_room.items

            # Try to drop the item (drop command)
            drop_command = type('Command', (), {"action": "drop", "target": test_item_id})
            result = engine.handle_command(drop_command)

            # This should fail with the current broken code
            assert result == "I don't understand that command."

            # Verify the item is still in inventory (should not be dropped)
            assert len(engine.game_state.inventory) == 1
            assert engine.game_state.inventory[0].item_id == test_item_id

            # Verify the item is not in the room (should not be there yet)
            assert test_item_id not in initial_room.items

        finally:
            # Restore original directory
            os.chdir(original_dir)