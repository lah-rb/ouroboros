"""
Preprocessing module initialization.

Exports static token management and CLI utilities.
"""

from .static_tokens import manager as static_tokens_manager, get_static_tokens
from .cli import main

__all__ = ["static_tokens_manager", "get_static_tokens", "main"]
