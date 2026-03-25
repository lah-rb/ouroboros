"""Schema extraction — lightweight structural summaries for LLM context.

Level 1: Extract key-access patterns from Python code via tree-sitter.
         Produces "what keys does this function expect?" summaries.

Level 2: Extract structural skeletons from YAML/JSON data files.
         Produces compact type-shape representations without values.

Both levels produce token-efficient representations suitable for
inclusion in every context bundle without significant cost.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Level 1: Key-access pattern extraction from Python code
# ══════════════════════════════════════════════════════════════════════


def extract_key_access_patterns(source: str, file_path: str = "") -> dict[str, list[str]]:
    """Extract dict key access patterns from Python source code.

    Finds patterns like:
        data["key"], data.get("key"), data['key']
        for x in data["items"]
        required_fields = {"id", "name", ...}

    Returns a dict mapping function/method names to the keys they access.
    This tells downstream consumers "what shape of data does this code expect?"

    Falls back to regex when tree-sitter is unavailable.
    """
    try:
        return _extract_keys_tree_sitter(source, file_path)
    except Exception:
        return _extract_keys_regex(source)


def _extract_keys_tree_sitter(source: str, file_path: str) -> dict[str, list[str]]:
    """Extract key access patterns using tree-sitter AST traversal."""
    try:
        import tree_sitter_python as tspython
        from tree_sitter import Language, Parser
    except ImportError:
        return _extract_keys_regex(source)

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))

    results: dict[str, list[str]] = {}
    current_func = "(module-level)"

    def _walk(node, func_name: str):
        nonlocal current_func

        # Track function/method scope
        if node.type in ("function_definition", "method_definition"):
            name_node = node.child_by_field_name("name")
            if name_node:
                func_name = name_node.text.decode("utf-8")

        # Subscript access: data["key"] or data['key']
        if node.type == "subscript":
            # The subscript value (the key)
            for child in node.children:
                if child.type == "string":
                    key = child.text.decode("utf-8").strip("'\"")
                    if func_name not in results:
                        results[func_name] = []
                    if key not in results[func_name]:
                        results[func_name].append(key)

        # Method call: data.get("key", ...) or data.pop("key")
        if node.type == "call":
            func_node = node.child_by_field_name("function")
            if func_node and func_node.type == "attribute":
                method_name = func_node.child_by_field_name("attribute")
                if method_name and method_name.text.decode("utf-8") in ("get", "pop", "setdefault"):
                    args = node.child_by_field_name("arguments")
                    if args:
                        for arg in args.children:
                            if arg.type == "string":
                                key = arg.text.decode("utf-8").strip("'\"")
                                if func_name not in results:
                                    results[func_name] = []
                                if key not in results[func_name]:
                                    results[func_name].append(key)

        # Set literal used as required_fields: {"id", "name", ...}
        if node.type == "set" and node.parent and node.parent.type == "assignment":
            target = node.parent.child_by_field_name("left")
            if target and "field" in target.text.decode("utf-8").lower():
                keys = []
                for child in node.children:
                    if child.type == "string":
                        keys.append(child.text.decode("utf-8").strip("'\""))
                if keys:
                    label = f"{func_name}:required_fields"
                    results[label] = keys

        for child in node.children:
            _walk(child, func_name)

    _walk(tree.root_node, current_func)
    return results


def _extract_keys_regex(source: str) -> dict[str, list[str]]:
    """Fallback regex-based key extraction."""
    results: dict[str, list[str]] = {}
    current_func = "(module-level)"

    for line in source.splitlines():
        # Track function scope
        func_match = re.match(r'\s*def\s+(\w+)', line)
        if func_match:
            current_func = func_match.group(1)

        # data["key"] or data['key']
        for match in re.finditer(r'''\[["'](\w+)["']\]''', line):
            key = match.group(1)
            if current_func not in results:
                results[current_func] = []
            if key not in results[current_func]:
                results[current_func].append(key)

        # data.get("key") or data.get('key')
        for match in re.finditer(r'''\.get\(["'](\w+)["']''', line):
            key = match.group(1)
            if current_func not in results:
                results[current_func] = []
            if key not in results[current_func]:
                results[current_func].append(key)

    return results


def format_key_patterns(patterns: dict[str, list[str]], file_path: str = "") -> str:
    """Format key access patterns as a compact schema summary.

    Output looks like:
        loader.py data keys:
          load_game_world: rooms, items, npcs, start_room
          validate_room: id, name, description, connections
    """
    if not patterns:
        return ""

    label = os.path.basename(file_path) if file_path else "file"
    lines = [f"{label} data keys:"]
    for func, keys in patterns.items():
        if len(keys) > 0:
            lines.append(f"  {func}: {', '.join(keys)}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════
# Level 2: Structural skeletons from YAML/JSON data files
# ══════════════════════════════════════════════════════════════════════


def extract_data_skeleton(content: str, file_path: str) -> str:
    """Extract a structural skeleton from a YAML or JSON data file.

    Replaces values with type indicators, preserves keys and nesting.
    A 240-line YAML becomes ~10-30 lines of structural summary.

    Example output:
        rooms: {<id>: {name: str, description: str, connections: {dir: str}, items: [str]}}
        items: {<id>: {name: str, description: str, effect: str}}
        npcs: {<id>: {name: str, dialogue: [{text: str, responses: [str]}]}}
    """
    ext = os.path.splitext(file_path)[1].lower()

    if ext in (".yaml", ".yml"):
        return _skeleton_from_yaml(content, file_path)
    elif ext in (".json",):
        return _skeleton_from_json(content, file_path)
    elif ext in (".toml",):
        return _skeleton_from_toml(content, file_path)
    return ""


def _skeleton_from_yaml(content: str, file_path: str) -> str:
    """Extract skeleton from YAML content."""
    try:
        import yaml
        data = yaml.safe_load(content)
        if data is None:
            return ""
        return _format_skeleton(data, os.path.basename(file_path))
    except Exception as e:
        logger.debug("Failed to parse YAML %s: %s", file_path, e)
        return ""


def _skeleton_from_json(content: str, file_path: str) -> str:
    """Extract skeleton from JSON content."""
    try:
        data = json.loads(content)
        return _format_skeleton(data, os.path.basename(file_path))
    except Exception as e:
        logger.debug("Failed to parse JSON %s: %s", file_path, e)
        return ""


def _skeleton_from_toml(content: str, file_path: str) -> str:
    """Extract skeleton from TOML content (best-effort)."""
    # TOML parsing requires tomllib (3.11+) or tomli
    try:
        import tomllib
        data = tomllib.loads(content)
        return _format_skeleton(data, os.path.basename(file_path))
    except ImportError:
        try:
            import tomli
            data = tomli.loads(content)
            return _format_skeleton(data, os.path.basename(file_path))
        except ImportError:
            return ""
    except Exception:
        return ""


def _format_skeleton(data: Any, label: str, max_depth: int = 4) -> str:
    """Format a data structure as a compact type skeleton.

    Produces output like:
        demo_adventure.yaml:
          initial_room: str
          rooms: {<id>: {name: str, description: str, connections: {dir: str}}}
          items: {<id>: {name: str, description: str}}
    """
    lines = [f"{label} schema:"]
    shape = _describe_shape(data, depth=0, max_depth=max_depth)
    if isinstance(data, dict):
        for key, value in data.items():
            child_shape = _describe_shape(value, depth=1, max_depth=max_depth)
            lines.append(f"  {key}: {child_shape}")
    else:
        lines.append(f"  {shape}")
    return "\n".join(lines)


def _describe_shape(value: Any, depth: int = 0, max_depth: int = 4) -> str:
    """Recursively describe the shape of a value as a compact type string."""
    if depth > max_depth:
        return "..."

    if value is None:
        return "null"
    elif isinstance(value, bool):
        return "bool"
    elif isinstance(value, int):
        return "int"
    elif isinstance(value, float):
        return "float"
    elif isinstance(value, str):
        return "str"
    elif isinstance(value, list):
        if not value:
            return "[]"
        # Describe the shape of the first element as representative
        elem_shape = _describe_shape(value[0], depth + 1, max_depth)
        # Check if all elements have the same shape
        if len(value) > 1:
            second_shape = _describe_shape(value[1], depth + 1, max_depth)
            if elem_shape != second_shape:
                return f"[{elem_shape}, ...]"
        return f"[{elem_shape}]"
    elif isinstance(value, dict):
        if not value:
            return "{}"
        keys = list(value.keys())
        first_val = value[keys[0]]

        # Check if this is a homogeneous dict (all values are dicts with similar keys)
        # e.g., rooms: {village_square: {name, desc, ...}, general_store: {name, desc, ...}}
        if len(keys) >= 2 and isinstance(first_val, dict):
            first_keys = set(first_val.keys()) if isinstance(first_val, dict) else set()
            is_homogeneous = all(
                isinstance(value[k], dict) and set(value[k].keys()) == first_keys
                for k in keys[:4]
            ) if first_keys else False

            if not is_homogeneous and first_keys:
                # Relaxed check: at least 70% key overlap
                is_homogeneous = all(
                    isinstance(value[k], dict)
                    and len(set(value[k].keys()) & first_keys) >= len(first_keys) * 0.7
                    for k in keys[:4]
                )

            if is_homogeneous:
                val_shape = _describe_shape(first_val, depth + 1, max_depth)
                return f"{{<id>: {val_shape}}}"

        # Non-dict values or heterogeneous — check for simple homogeneity
        if len(keys) >= 2:
            shapes = set()
            for k in keys[:3]:
                shapes.add(_describe_shape(value[k], depth + 1, max_depth))
            if len(shapes) == 1:
                val_shape = _describe_shape(first_val, depth + 1, max_depth)
                return f"{{<id>: {val_shape}}}"

        # Heterogeneous dict — show all keys
        parts = []
        for key in keys[:8]:
            child_shape = _describe_shape(value[key], depth + 1, max_depth)
            parts.append(f"{key}: {child_shape}")
        if len(keys) > 8:
            parts.append("...")
        return "{" + ", ".join(parts) + "}"

    return "unknown"


# ══════════════════════════════════════════════════════════════════════
# Integration: Format both levels for context inclusion
# ══════════════════════════════════════════════════════════════════════


def build_schema_context(
    files: dict[str, str],
    max_chars: int = 1500,
) -> str:
    """Build a combined schema context from all project files.

    Extracts Level 1 (key-access patterns) from code files and
    Level 2 (structural skeletons) from data files. Returns a
    compact string suitable for direct inclusion in prompts.

    Args:
        files: Dictionary mapping file paths to file contents.
        max_chars: Maximum characters for the output.

    Returns:
        Formatted schema context string, or empty string if nothing extracted.
    """
    sections: list[str] = []
    chars_used = 0

    data_extensions = {".yaml", ".yml", ".json", ".toml"}
    code_extensions = {".py", ".js", ".ts", ".rs"}

    # Level 2 first — data file skeletons (most valuable for format alignment)
    for file_path, content in sorted(files.items()):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in data_extensions:
            continue
        skeleton = extract_data_skeleton(content, file_path)
        if skeleton and chars_used + len(skeleton) < max_chars:
            sections.append(skeleton)
            chars_used += len(skeleton) + 1

    # Level 1 — key-access patterns from code files
    for file_path, content in sorted(files.items()):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in code_extensions:
            continue
        patterns = extract_key_access_patterns(content, file_path)
        if patterns:
            formatted = format_key_patterns(patterns, file_path)
            if formatted and chars_used + len(formatted) < max_chars:
                sections.append(formatted)
                chars_used += len(formatted) + 1

    if not sections:
        return ""

    return "\n\n".join(sections)
