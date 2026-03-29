"""Command parser for the text adventure engine."""

import re
from typing import Optional, Tuple


class CommandParser:
    """Parses player input into structured commands."""
    
    def __init__(self):
        self.commands = {
            "go": ["go", "walk", "move", "travel"],
            "take": ["take", "grab", "pick", "pickup"],
            "drop": ["drop", "leave", "discard"],
            "use": ["use", "apply"],
            "examine": ["examine", "look", "inspect", "check"],
            "talk": ["talk", "speak", "chat", "converse"],
            "inventory": ["inventory", "inv", "i"],
            "look": ["look", "l"],
            "help": ["help", "?"],
            "quit": ["quit", "q", "exit"],
        }
        
        self.directions = ["north", "south", "east", "west", "n", "s", "e", "w"]
    
    def parse(self, input_text: str) -> Tuple[str, Optional[str]]:
        """
        Parse player input into command and argument.
        
        Returns:
            Tuple of (command_type, argument)
        """
        text = input_text.strip().lower()
        
        # Handle single-word commands
        if text in ["inventory", "inv", "i"]:
            return ("inventory", None)
        if text in ["look", "l"]:
            return ("look", None)
        if text in ["help", "?"]:
            return ("help", None)
        if text in ["quit", "q", "exit"]:
            return ("quit", None)
        
        # Handle direction commands
        if text in self.directions:
            return ("go", self._normalize_direction(text))
        
        # Parse multi-word commands
        for command_type, keywords in self.commands.items():
            for keyword in keywords:
                if text.startswith(keyword):
                    remainder = text[len(keyword):].strip()
                    
                    if command_type == "go":
                        direction = self._normalize_direction(remainder)
                        if direction:
                            return ("go", direction)
                    
                    return (command_type, remainder)
        
        return ("unknown", None)
    
    def _normalize_direction(self, direction: str) -> Optional[str]:
        """Normalize direction input to standard directions."""
        direction_map = {
            "n": "north",
            "s": "south", 
            "e": "east",
            "w": "west",
            "north": "north",
            "south": "south",
            "east": "east",
            "west": "west"
        }
        return direction_map.get(direction.lower())
