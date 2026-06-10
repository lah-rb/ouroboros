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


def extract_key_access_patterns(
    source: str, file_path: str = ""
) -> dict[str, list[str]]:
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
        return _extract_keys_tree_sitter(source)
    except Exception:
        return _extract_keys_regex(source)


def _extract_keys_tree_sitter(source: str) -> dict[str, list[str]]:
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
                if method_name and method_name.text.decode("utf-8") in (
                    "get",
                    "pop",
                    "setdefault",
                ):
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
        func_match = re.match(r"\s*def\s+(\w+)", line)
        if func_match:
            current_func = func_match.group(1)

        # data["key"] or data['key']
        for match in re.finditer(r"""\[["'](\w+)["']\]""", line):
            key = match.group(1)
            if current_func not in results:
                results[current_func] = []
            if key not in results[current_func]:
                results[current_func].append(key)

        # data.get("key") or data.get('key')
        for match in re.finditer(r"""\.get\(["'](\w+)["']""", line):
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


# ── Data-load detection (which data file does this code read?) ─────────

# Callables whose argument carries a data-file path. The path almost always
# appears as a string literal arg to open(...) (e.g. yaml.safe_load(open(p)))
# or directly to a loader; we detect the literal and also the bare-Name case
# (open(DATA_PATH)) for the caller to resolve via extract_module_constants.
_DATA_LOADERS = {
    "open",
    "yaml.safe_load",
    "yaml.load",
    "json.load",
    "json.loads",
    "tomllib.load",
    "tomllib.loads",
    "toml.load",
    "safe_load",
}
_DATA_PATH_SUFFIXES = (".yaml", ".yml", ".json", ".toml")


def find_data_loads(source: str, file_path: str = "") -> list[dict]:
    """Find data-file load sites in a Python source / symbol body.

    Detects string-literal data paths (``open("world_data.yaml")``) and the
    module-constant case (``open(DATA_PATH)`` where ``DATA_PATH = "..."``) —
    the latter returned unresolved for the caller to resolve via
    :func:`extract_module_constants`. The complement to the code-symbol trace:
    it locates the *data* a symbol reads, which the symbol-reference walk
    cannot see. Falls back to regex when tree-sitter is unavailable.

    Returns deduped ``[{"path": str|None, "raw_arg": str, "resolved": bool,
    "loader": str}]``.
    """
    try:
        return _find_data_loads_tree_sitter(source)
    except Exception:
        return _find_data_loads_regex(source)


def _find_data_loads_tree_sitter(source: str) -> list[dict]:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))

    out: list[dict] = []
    seen: set[str] = set()

    def _add(path: str | None, raw: str, resolved: bool, loader: str) -> None:
        key = (path or raw or "").strip()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(
            {"path": path, "raw_arg": raw, "resolved": resolved, "loader": loader}
        )

    def _walk(node: Any) -> None:
        # String literal ending in a data extension → a resolved data path.
        if node.type == "string":
            val = node.text.decode("utf-8", "replace").strip("'\"")
            if val.endswith(_DATA_PATH_SUFFIXES):
                _add(val, val, True, "literal")
        # open()/loader call with a bare Name arg → unresolved module constant.
        elif node.type == "call":
            fn = node.child_by_field_name("function")
            fname = fn.text.decode("utf-8", "replace") if fn is not None else ""
            bare = fname.rsplit(".", 1)[-1] if "." in fname else fname
            if fname in _DATA_LOADERS or bare in _DATA_LOADERS:
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for arg in args.children:
                        if arg.type == "identifier":
                            _add(
                                None,
                                arg.text.decode("utf-8", "replace"),
                                False,
                                fname or "open",
                            )
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return out


def _find_data_loads_regex(source: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"""["']([^"']+\.(?:ya?ml|json|toml))["']""", source):
        val = m.group(1)
        if val not in seen:
            seen.add(val)
            out.append(
                {"path": val, "raw_arg": val, "resolved": True, "loader": "literal"}
            )
    return out


def extract_module_constants(source: str) -> dict[str, str]:
    """Top-level ``NAME = "string-literal"`` assignments.

    Used to resolve ``open(DATA_PATH)`` where ``DATA_PATH = "world_data.yaml"``
    is defined at module scope. Only string-valued, module-level. Falls back to
    regex when tree-sitter is unavailable.
    """
    try:
        return _extract_module_constants_tree_sitter(source)
    except Exception:
        return _extract_module_constants_regex(source)


def _extract_module_constants_tree_sitter(source: str) -> dict[str, str]:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    consts: dict[str, str] = {}
    for child in tree.root_node.children:  # module scope only
        if child.type != "expression_statement" or not child.children:
            continue
        assign = child.children[0]
        if assign.type != "assignment":
            continue
        left = assign.child_by_field_name("left")
        right = assign.child_by_field_name("right")
        if (
            left is not None
            and left.type == "identifier"
            and right is not None
            and right.type == "string"
        ):
            name = left.text.decode("utf-8", "replace")
            consts[name] = right.text.decode("utf-8", "replace").strip("'\"")
    return consts


def _extract_module_constants_regex(source: str) -> dict[str, str]:
    consts: dict[str, str] = {}
    for line in source.splitlines():
        m = re.match(r"""([A-Za-z_]\w*)\s*=\s*["']([^"']+)["']\s*$""", line)
        if m:  # no leading indent ⇒ module level
            consts[m.group(1)] = m.group(2)
    return consts


# A call to a structured-data parser. Distinctive names (no false positives),
# so a regex is enough. Signals "this code parses data" even when the path is
# a PARAMETER (``def load(filepath): yaml.safe_load(open(filepath))``) — the
# single most common loader shape, where the path literal lives at the call
# site, not the body. The caller then surfaces the project's data files.
_DATA_PARSE_CALL_RE = re.compile(
    r"(?:yaml\.safe_load|yaml\.load|json\.loads?|tomllib\.loads?|toml\.load|"
    r"\bsafe_load)\s*\("
)


def parses_data(source: str) -> bool:
    """True if the source contains a structured-data parse call (yaml/json/toml)."""
    return bool(_DATA_PARSE_CALL_RE.search(source))


# ══════════════════════════════════════════════════════════════════════
# Level 1b: Model-attribute access + model definitions (the model-layer hop)
# ══════════════════════════════════════════════════════════════════════
#
# The dict-key extractor above sees ``data["dialogue"]``. When a loader parses a
# data file into typed model instances and gameplay code reads ATTRIBUTES
# (``npc.dialogue_nodes``), that access is invisible to it. These helpers expose
# the model side: which attributes a symbol reads, and how project model classes
# map field → type — enough to connect ``npc.dialogue_nodes`` back to the data.


def extract_attr_reads(source: str) -> dict[str, list[str]]:
    """Map function/method name → attribute names it READS (not method calls).

    ``node.text`` and ``npc.dialogue_nodes`` are attribute reads; ``x.get(...)``
    and ``items.append(...)`` are calls and are excluded. The complement to
    :func:`extract_key_access_patterns` for model-instance access. Falls back to
    regex when tree-sitter is unavailable.
    """
    try:
        return _extract_attr_reads_tree_sitter(source)
    except Exception:
        return _extract_attr_reads_regex(source)


def _extract_attr_reads_tree_sitter(source: str) -> dict[str, list[str]]:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))

    results: dict[str, list[str]] = {}

    def _walk(node: Any, func_name: str) -> None:
        if node.type in ("function_definition", "method_definition"):
            nm = node.child_by_field_name("name")
            if nm:
                func_name = nm.text.decode("utf-8")

        if node.type == "attribute":
            attr = node.child_by_field_name("attribute")
            # Skip when this attribute is the function being CALLED (x.method()).
            # Compare by byte span — child_by_field_name returns a fresh wrapper,
            # so an `is` identity check would never match.
            parent = node.parent
            is_called = False
            if parent is not None and parent.type == "call":
                fn = parent.child_by_field_name("function")
                if fn is not None and (fn.start_byte, fn.end_byte) == (
                    node.start_byte,
                    node.end_byte,
                ):
                    is_called = True
            if attr is not None and not is_called:
                name = attr.text.decode("utf-8")
                results.setdefault(func_name, [])
                if name not in results[func_name]:
                    results[func_name].append(name)

        for child in node.children:
            _walk(child, func_name)

    _walk(tree.root_node, "(module-level)")
    return results


def _extract_attr_reads_regex(source: str) -> dict[str, list[str]]:
    results: dict[str, list[str]] = {}
    current = "(module-level)"
    func_re = re.compile(r"^\s*def\s+([A-Za-z_]\w*)\s*\(")
    # `.attr` only when the WHOLE name is followed by a non-word, non-`(` char
    # (or end) — excludes method calls (`.strip(`) without truncating the name.
    attr_re = re.compile(r"\.([A-Za-z_]\w*)(?=[^\w(]|$)")
    for line in source.splitlines():
        m = func_re.match(line)
        if m:
            current = m.group(1)
            continue
        for am in attr_re.finditer(line):
            name = am.group(1)
            results.setdefault(current, [])
            if name not in results[current]:
                results[current].append(name)
    return results


def extract_model_defs(source: str) -> dict[str, dict]:
    """Parse class definitions → ``{ClassName: {"fields": {name: type_str},
    "bases": [str], "decorators": [str]}}``.

    Only class-level annotated fields (``name: type`` / ``name: type = ...``) are
    captured — method locals are ignored. Used to map a read attribute back to
    the model that owns it and to follow field types through containers. Falls
    back to a light regex when tree-sitter is unavailable.
    """
    try:
        return _extract_model_defs_tree_sitter(source)
    except Exception:
        return _extract_model_defs_regex(source)


def _extract_model_defs_tree_sitter(source: str) -> dict[str, dict]:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    out: dict[str, dict] = {}

    def _decorators(cls_node: Any) -> list[str]:
        decs: list[str] = []
        parent = cls_node.parent
        if parent is not None and parent.type == "decorated_definition":
            for ch in parent.children:
                if ch.type == "decorator":
                    decs.append(
                        ch.text.decode("utf-8").lstrip("@").split("(")[0].strip()
                    )
        return decs

    def _bases(cls_node: Any) -> list[str]:
        sup = cls_node.child_by_field_name("superclasses")
        if sup is None:
            return []
        return [
            c.text.decode("utf-8")
            for c in sup.children
            if c.type in ("identifier", "attribute")
        ]

    def _fields(cls_node: Any) -> dict[str, str]:
        body = cls_node.child_by_field_name("body")
        fields: dict[str, str] = {}
        if body is None:
            return fields
        for stmt in body.children:
            # A class-level annotation parses as expression_statement > assignment
            # carrying a `type` field (with or without a `right` default).
            node = stmt
            if stmt.type == "expression_statement" and stmt.children:
                node = stmt.children[0]
            if node.type == "assignment":
                left = node.child_by_field_name("left")
                typ = node.child_by_field_name("type")
                if left is not None and left.type == "identifier" and typ is not None:
                    fields[left.text.decode("utf-8")] = typ.text.decode("utf-8")
        return fields

    def _walk(node: Any) -> None:
        if node.type == "class_definition":
            nm = node.child_by_field_name("name")
            if nm is not None:
                out[nm.text.decode("utf-8")] = {
                    "fields": _fields(node),
                    "bases": _bases(node),
                    "decorators": _decorators(node),
                }
        for child in node.children:
            _walk(child)

    _walk(tree.root_node)
    return out


def _extract_model_defs_regex(source: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    current: str | None = None
    base_indent = 0
    cls_re = re.compile(r"^(\s*)class\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?\s*:")
    field_re = re.compile(r"^(\s+)([A-Za-z_]\w*)\s*:\s*([^=\n]+?)\s*(?:=.*)?$")
    for line in source.splitlines():
        cm = cls_re.match(line)
        if cm:
            current = cm.group(2)
            base_indent = len(cm.group(1))
            bases = [b.strip() for b in (cm.group(3) or "").split(",") if b.strip()]
            out[current] = {"fields": {}, "bases": bases, "decorators": []}
            continue
        if current is None:
            continue
        fm = field_re.match(line)
        if fm and len(fm.group(1)) > base_indent and "def " not in line:
            # one level of indent past the class — a class body field
            if len(fm.group(1)) <= base_indent + 8:
                out[current]["fields"][fm.group(2)] = fm.group(3).strip()
        elif line.strip() and not line[:1].isspace():
            current = None  # dedent to module level
    return out


def element_type(type_str: str) -> str:
    """Innermost element type of a possibly-generic annotation.

    ``Dict[str, DialogueNode]`` → ``DialogueNode``; ``List[NPC]`` → ``NPC``;
    ``Optional[Room]`` → ``Room``; ``NPC`` → ``NPC``.
    """
    t = (type_str or "").strip().strip("'\"")
    if "[" in t and t.endswith("]"):
        inner = t[t.index("[") + 1 : -1]
        parts = _split_top_level(inner)
        if parts:
            return element_type(parts[-1])
    return t.split(".")[-1].strip()


def find_model_instantiation_keys(source: str, model_names: set[str]) -> dict[str, str]:
    """Map model class → the data key its instances are built from.

    A loader like ``for r in data["rooms"]: Room(...)`` ties ``Room`` to the key
    ``rooms``; a nested ``for n in r["npcs"]: NPC(...)`` ties ``NPC`` to ``npcs``.
    Pins the data-file location a model originates from. Tree-sitter, regex
    fallback. Returns only models found instantiated inside a keyed loop.
    """
    try:
        return _find_model_instantiation_keys_tree_sitter(source, model_names)
    except Exception:
        return {}


def _subscript_or_get_key(node: Any) -> str | None:
    """First string key in a `x["key"]` subscript or `x.get("key")` within node."""
    if node is None:
        return None
    if node.type == "subscript":
        for ch in node.children:
            if ch.type == "string":
                return ch.text.decode("utf-8", "replace").strip("'\"")
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            mth = fn.child_by_field_name("attribute")
            if mth is not None and mth.text.decode("utf-8") in ("get", "pop"):
                args = node.child_by_field_name("arguments")
                if args is not None:
                    for a in args.children:
                        if a.type == "string":
                            return a.text.decode("utf-8", "replace").strip("'\"")
    for ch in node.children:
        k = _subscript_or_get_key(ch)
        if k:
            return k
    return None


def _find_model_instantiation_keys_tree_sitter(
    source: str, model_names: set[str]
) -> dict[str, str]:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    lang = Language(tspython.language())
    parser = Parser(lang)
    tree = parser.parse(source.encode("utf-8"))
    out: dict[str, str] = {}

    def _walk(node: Any, key_stack: list[str]) -> None:
        stack = key_stack
        if node.type == "for_statement":
            right = node.child_by_field_name("right")
            k = _subscript_or_get_key(right)
            stack = key_stack + ([k] if k else [])
        if node.type == "call":
            fn = node.child_by_field_name("function")
            if fn is not None and fn.type == "identifier":
                cls = fn.text.decode("utf-8")
                if cls in model_names and cls not in out and stack:
                    out[cls] = stack[-1]  # nearest enclosing keyed loop
        for child in node.children:
            _walk(child, stack)

    _walk(tree.root_node, [])
    return out


def _split_top_level(s: str) -> list[str]:
    """Split on top-level commas, respecting [] nesting."""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in s:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if p.strip()]


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
            is_homogeneous = (
                all(
                    isinstance(value[k], dict) and set(value[k].keys()) == first_keys
                    for k in keys
                )
                if first_keys
                else False
            )

            if not is_homogeneous and first_keys:
                # Relaxed check: at least 70% key overlap
                is_homogeneous = all(
                    isinstance(value[k], dict)
                    and len(set(value[k].keys()) & first_keys) >= len(first_keys) * 0.7
                    for k in keys
                )

            if is_homogeneous:
                val_shape = _describe_shape(first_val, depth + 1, max_depth)
                return f"{{<id>: {val_shape}}}"

        # Non-dict values or heterogeneous — check for simple homogeneity
        if len(keys) >= 2:
            shapes = set()
            for k in keys:
                shapes.add(_describe_shape(value[k], depth + 1, max_depth))
            if len(shapes) == 1:
                val_shape = _describe_shape(first_val, depth + 1, max_depth)
                return f"{{<id>: {val_shape}}}"

        # Heterogeneous dict — show all keys
        parts = []
        for key in keys:
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
) -> str:
    """Build a combined schema context from all project files.

    Extracts Level 1 (key-access patterns) from code files and
    Level 2 (structural skeletons) from data files. Returns a
    string suitable for direct inclusion in prompts.

    Args:
        files: Dictionary mapping file paths to file contents.

    Returns:
        Formatted schema context string, or empty string if nothing extracted.
    """
    sections: list[str] = []

    data_extensions = {".yaml", ".yml", ".json", ".toml"}
    code_extensions = {".py", ".js", ".ts", ".rs"}

    # Level 2 first — data file skeletons (most valuable for format alignment)
    for file_path, content in sorted(files.items()):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in data_extensions:
            continue
        skeleton = extract_data_skeleton(content, file_path)
        if skeleton:
            sections.append(skeleton)

    # Level 1 — key-access patterns from code files
    for file_path, content in sorted(files.items()):
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in code_extensions:
            continue
        patterns = extract_key_access_patterns(content, file_path)
        if patterns:
            formatted = format_key_patterns(patterns, file_path)
            if formatted:
                sections.append(formatted)

    if not sections:
        return ""

    return "\n\n".join(sections)
