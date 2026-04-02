"""
Tools Package

Generic tool-use framework for LLMvp.
Tools are registered via the ToolRegistry and can be dispatched by name.
Each tool is a self-contained module that registers itself on import.
"""

from tools.registry import Tool, ToolRegistry, get_registry  # noqa: F401
