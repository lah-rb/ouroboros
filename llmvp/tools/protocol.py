"""
Tool-Calling Protocol

Model-agnostic text-based protocol for tool use during inference.
The LLM emits a simple XML marker when it needs a tool result;
the server detects it, executes the tool, and re-injects the answer.

Markers::

    <tool_call>{"name": "keal_damage_bonus", "params": {"attacker": "Cenozoic", "defender": "Decrepit"}}</tool_call>

This module provides:
  - Marker constants
  - Parser for extracting tool calls from model output
  - Formatter for tool results (injected back into the conversation)
  - Instruction generator for system-prompt inclusion
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from tools.registry import get_registry

log = logging.getLogger("llm-mvp")

# ------------------------------------------------------------------
# Marker constants
# ------------------------------------------------------------------
TOOL_CALL_START = "<tool_call>"
TOOL_CALL_END = "</tool_call>"

_TOOL_CALL_RE = re.compile(
    re.escape(TOOL_CALL_START) + r"(.*?)" + re.escape(TOOL_CALL_END),
    re.DOTALL,
)


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------
@dataclass
class ToolCallRequest:
    """Parsed tool call from model output."""

    name: str
    params: Dict[str, Any]
    raw_match: str  # The full <tool_call>...</tool_call> text


# ------------------------------------------------------------------
# Parsing
# ------------------------------------------------------------------
def parse_tool_call(text: str) -> Optional[ToolCallRequest]:
    """
    Extract the first ``<tool_call>...</tool_call>`` from *text*.

    Returns ``None`` if no tool call is found or if parsing fails.
    """
    match = _TOOL_CALL_RE.search(text)
    if match is None:
        return None

    raw_json = match.group(1).strip()
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        log.warning("⚠️ Tool call JSON parse error: %s", exc)
        return None

    name = payload.get("name")
    params = payload.get("params", {})

    if not isinstance(name, str) or not name:
        log.warning("⚠️ Tool call missing 'name' field")
        return None

    return ToolCallRequest(
        name=name,
        params=params if isinstance(params, dict) else {},
        raw_match=match.group(0),
    )


def has_tool_call(text: str) -> bool:
    """Quick check whether *text* contains a tool-call marker."""
    return TOOL_CALL_START in text and TOOL_CALL_END in text


# ------------------------------------------------------------------
# Formatting
# ------------------------------------------------------------------
def format_tool_result(name: str, result_json: str) -> str:
    """
    Format a tool result for injection into the conversation.

    Returns a string suitable for a follow-up user message.
    """
    return f"Tool result for {name}: {result_json}"


def format_tool_instructions() -> str:
    """
    Generate a system-prompt fragment that teaches the model
    how to use tools and lists all registered tools.

    Returns empty string if no tools are registered.
    """
    registry = get_registry()
    tools = registry.list_tools()
    if not tools:
        return ""

    lines = [
        "## Tool Use",
        "",
        "When you need to look up or calculate data that a tool provides,",
        "emit a tool call in your response using this exact format:",
        "",
        "```",
        f'{TOOL_CALL_START}{{"name": "tool_name", "params": {{"key": "value"}}}}{TOOL_CALL_END}',
        "```",
        "",
        "The system will execute the tool and provide the result.",
        "Then use the result to complete your answer.",
        "Only call one tool at a time.",
        "",
    ]

    # Append the registry's tool descriptions
    lines.append(registry.tools_system_prompt())

    return "\n".join(lines)
