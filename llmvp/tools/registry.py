"""
Tool Registry

Generic, domain-agnostic tool registration and dispatch system.
Tools register themselves by calling ``get_registry().register(tool)``.
The registry provides:
  - lookup / listing for tool metadata
  - synchronous dispatch by name
  - a helper that renders all registered tools as a system-prompt fragment
    so an LLM can decide when to call them
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("llm-mvp")


# ------------------------------------------------------------------
# Tool dataclass
# ------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    """
    A single callable tool.

    Attributes:
        name:        Machine-readable identifier (e.g. ``keal_damage_bonus``).
        description: Human-readable explanation shown to the LLM.
        parameters:  JSON-Schema-style dict describing accepted params.
        execute:     ``(params: dict) -> str`` — runs the tool and returns
                     a string result suitable for injection into an LLM
                     context window.
    """

    name: str
    description: str
    parameters: Dict[str, Any]
    execute: Callable[[Dict[str, Any]], str]


# ------------------------------------------------------------------
# Registry singleton
# ------------------------------------------------------------------
class ToolRegistry:
    """Thread-safe (GIL-safe) registry of available tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    # ---- mutation ----

    def register(self, tool: Tool) -> None:
        """Register a tool.  Overwrites silently if name already exists."""
        self._tools[tool.name] = tool
        log.info("🔧 Tool registered: %s", tool.name)

    def unregister(self, name: str) -> bool:
        """Remove a tool by name.  Returns True if it existed."""
        return self._tools.pop(name, None) is not None

    # ---- query ----

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[Tool]:
        return list(self._tools.values())

    def tool_names(self) -> List[str]:
        return list(self._tools.keys())

    # ---- dispatch ----

    def execute(self, name: str, params: Dict[str, Any]) -> str:
        """
        Look up *name* and call its execute function with *params*.

        Returns the string result on success.
        Raises ``KeyError`` if the tool is not registered,
        and propagates any exception the tool itself raises.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool not found: {name!r}")
        return tool.execute(params)

    # ---- LLM helpers ----

    def tools_system_prompt(self) -> str:
        """
        Render a system-prompt fragment that describes every registered
        tool so the LLM knows what is available.

        The format is intentionally simple Markdown so small models
        can parse it without fine-tuning on a function-calling schema.
        """
        if not self._tools:
            return ""

        lines = ["## Available Tools", ""]
        for tool in self._tools.values():
            lines.append(f"### {tool.name}")
            lines.append(tool.description)
            lines.append("")
            lines.append("**Parameters (JSON):**")
            lines.append(f"```json\n{json.dumps(tool.parameters, indent=2)}\n```")
            lines.append("")
        return "\n".join(lines)


# ------------------------------------------------------------------
# Module-level singleton accessor
# ------------------------------------------------------------------
_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """Return the global ToolRegistry, creating it on first call."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry
