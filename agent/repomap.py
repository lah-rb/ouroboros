"""AST-based repository map — tree-sitter parsing with PageRank ranking.

Inspired by Aider's repo map approach. Extracts symbol definitions and
references using tree-sitter, builds a file dependency graph, ranks files
with PageRank, and formats a token-budgeted map for LLM consumption.

Falls back to regex-based extraction for unsupported languages.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

import networkx as nx

# ── Data Models ───────────────────────────────────────────────────────


@dataclass
class SymbolDef:
    """A symbol definition extracted from source code."""

    name: str
    kind: Literal["class", "function", "method", "variable", "import", "module"]
    file_path: str
    line: int
    end_line: int = 0  # last line of the symbol (1-indexed)
    start_byte: int = 0  # byte offset of symbol start in file content
    end_byte: int = 0  # byte offset of symbol end in file content
    signature: str = ""  # e.g. "def process(items: list[str]) -> Result:"
    parent: str | None = None  # for methods: the class name


@dataclass
class SymbolRef:
    """A reference to a symbol in source code."""

    name: str
    file_path: str
    line: int


@dataclass
class FileInfo:
    """Aggregated info about a single file."""

    path: str
    definitions: list[SymbolDef] = field(default_factory=list)
    references: list[SymbolRef] = field(default_factory=list)
    language: str = "unknown"


@dataclass
class RepoMap:
    """Complete repository map with definitions, references, and rankings."""

    files: dict[str, FileInfo]  # file_path → FileInfo
    file_rankings: dict[str, float]  # file_path → PageRank score

    def format_for_prompt(
        self,
        max_chars: int = 4000,
        focus_files: list[str] | None = None,
    ) -> str:
        """Format the repo map for LLM consumption within a character budget.

        Args:
            max_chars: Maximum characters for the formatted output.
            focus_files: Files to boost in ranking (e.g., files being modified).

        Returns:
            Formatted repo map string.
        """
        # Compute effective rankings with focus boost
        effective_ranks = dict(self.file_rankings)
        if focus_files:
            for fp in focus_files:
                if fp in effective_ranks:
                    effective_ranks[fp] *= 3.0  # 3x boost for focus files
                # Also boost files that reference focus files
                if fp in self.files:
                    for ref in self.files[fp].references:
                        for other_fp, other_info in self.files.items():
                            for d in other_info.definitions:
                                if d.name == ref.name and other_fp != fp:
                                    effective_ranks[other_fp] = (
                                        effective_ranks.get(other_fp, 0) * 1.5
                                    )

        # Sort files by effective rank
        ranked_files = sorted(effective_ranks.items(), key=lambda x: x[1], reverse=True)

        lines: list[str] = []
        chars_used = 0

        for file_path, rank in ranked_files:
            file_info = self.files.get(file_path)
            if not file_info or not file_info.definitions:
                continue

            # Format file section
            file_lines = [f"{file_path}:"]
            for defn in file_info.definitions:
                if defn.kind in ("class", "function", "method"):
                    prefix = "│" if defn.parent is None else "│  "
                    file_lines.append(f"{prefix} {defn.signature}")
                elif defn.kind == "variable" and defn.parent is None:
                    file_lines.append(f"│ {defn.signature}")
            file_lines.append("⋮...")

            section = "\n".join(file_lines) + "\n"
            if chars_used + len(section) > max_chars:
                # Try to fit at least the filename
                stub = f"{file_path}: ({len(file_info.definitions)} definitions)\n"
                if chars_used + len(stub) <= max_chars:
                    lines.append(stub)
                    chars_used += len(stub)
                break

            lines.append(section)
            chars_used += len(section)

        return "".join(lines)

    def get_related_files(self, file_path: str, max_files: int = 10) -> list[str]:
        """Get files most related to the given file by reference graph.

        Returns files that define symbols referenced by the given file,
        or that reference symbols defined in the given file.
        """
        if file_path not in self.files:
            return []

        file_info = self.files[file_path]

        # Names defined in this file
        defined_names = {d.name for d in file_info.definitions}
        # Names referenced by this file
        referenced_names = {r.name for r in file_info.references}

        related_scores: dict[str, float] = defaultdict(float)

        for other_path, other_info in self.files.items():
            if other_path == file_path:
                continue

            # Files that define symbols we reference (imports/dependencies)
            other_defined = {d.name for d in other_info.definitions}
            shared_refs = referenced_names & other_defined
            if shared_refs:
                related_scores[other_path] += len(shared_refs) * 2.0

            # Files that reference symbols we define (dependents)
            other_refs = {r.name for r in other_info.references}
            shared_defs = defined_names & other_refs
            if shared_defs:
                related_scores[other_path] += len(shared_defs) * 1.0

        # Sort by score, top N
        ranked = sorted(related_scores.items(), key=lambda x: x[1], reverse=True)
        return [fp for fp, _ in ranked[:max_files]]


# ── tree-sitter Python Extractor ──────────────────────────────────────


_TREE_SITTER_AVAILABLE = False
_PYTHON_LANGUAGE = None

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    _PYTHON_LANGUAGE = Language(tspython.language())
    _TREE_SITTER_AVAILABLE = True
except ImportError:
    pass


def _extract_python_tree_sitter(
    file_path: str, content: str
) -> tuple[list[SymbolDef], list[SymbolRef]]:
    """Extract definitions and references from Python using tree-sitter.

    Returns (definitions, references).
    """
    if not _TREE_SITTER_AVAILABLE or _PYTHON_LANGUAGE is None:
        return [], []

    parser = Parser(_PYTHON_LANGUAGE)
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node

    definitions: list[SymbolDef] = []
    references: list[SymbolRef] = []
    defined_names: set[str] = set()

    def _walk(node: Any, parent_class: str | None = None) -> None:
        """Recursively walk the AST to extract definitions."""
        if node.type == "class_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                # Build signature from class line
                sig = _node_first_line(node, content)
                # Include decorators: if the parent is a decorated_definition,
                # extend start_byte to cover the decorator lines.  This ensures
                # the symbol body shown to the LLM includes @dataclass etc.,
                # preventing the splice from stacking duplicate decorators.
                effective_start = node.start_byte
                effective_line = node.start_point[0] + 1
                if (
                    node.parent is not None
                    and node.parent.type == "decorated_definition"
                ):
                    effective_start = node.parent.start_byte
                    effective_line = node.parent.start_point[0] + 1
                definitions.append(
                    SymbolDef(
                        name=name,
                        kind="class",
                        file_path=file_path,
                        line=effective_line,
                        end_line=node.end_point[0] + 1,
                        start_byte=effective_start,
                        end_byte=node.end_byte,
                        signature=sig,
                        parent=parent_class,
                    )
                )
                defined_names.add(name)
                # Recurse into class body for methods
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        _walk(child, parent_class=name)
                return  # Don't recurse further — body handled above

        elif node.type == "function_definition":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = name_node.text.decode("utf-8")
                sig = _node_first_line(node, content)
                kind = "method" if parent_class else "function"
                # Include decorators — same logic as class_definition above
                effective_start = node.start_byte
                effective_line = node.start_point[0] + 1
                if (
                    node.parent is not None
                    and node.parent.type == "decorated_definition"
                ):
                    effective_start = node.parent.start_byte
                    effective_line = node.parent.start_point[0] + 1
                definitions.append(
                    SymbolDef(
                        name=name,
                        kind=kind,
                        file_path=file_path,
                        line=effective_line,
                        end_line=node.end_point[0] + 1,
                        start_byte=effective_start,
                        end_byte=node.end_byte,
                        signature=sig,
                        parent=parent_class,
                    )
                )
                defined_names.add(name)
                return  # Don't recurse into function bodies

        elif node.type == "import_statement":
            text = node.text.decode("utf-8")
            definitions.append(
                SymbolDef(
                    name=text,
                    kind="import",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    signature=text,
                )
            )

        elif node.type == "import_from_statement":
            text = node.text.decode("utf-8")
            definitions.append(
                SymbolDef(
                    name=text,
                    kind="import",
                    file_path=file_path,
                    line=node.start_point[0] + 1,
                    signature=text,
                )
            )
            # Also extract individual imported names as references
            for child in node.children:
                if child.type == "dotted_name" or child.type == "aliased_import":
                    name = child.text.decode("utf-8")
                    if " as " in name:
                        name = name.split(" as ")[0].strip()
                    references.append(
                        SymbolRef(
                            name=name,
                            file_path=file_path,
                            line=child.start_point[0] + 1,
                        )
                    )

        elif (
            node.type == "expression_statement"
            and parent_class is None
            and node.parent
            and node.parent.type == "module"
        ):
            # Top-level variable assignment
            first_child = node.children[0] if node.children else None
            if first_child and first_child.type == "assignment":
                left = first_child.child_by_field_name("left")
                if left and left.type == "identifier":
                    name = left.text.decode("utf-8")
                    sig = _node_first_line(node, content)
                    # 88c regression fix — previously we populated only
                    # line + signature. The downstream splice path in
                    # action_rewrite_symbol_turn reads end_line /
                    # start_byte / end_byte to do byte-precise
                    # replacement; with those left at SymbolDef's
                    # zero defaults, patch on a variable silently
                    # routed to add_symbol (via the "symbol absent
                    # from symbol_table" branch — variables were
                    # filtered out), which APPENDED a duplicate
                    # definition instead of replacing. 88c's
                    # _COMMAND_PATTERNS ended up defined three times
                    # in parser.py, shadowing each other and
                    # breaking iteration. Capturing the full extent
                    # here lets patch target module-level
                    # assignments the same way it targets functions.
                    definitions.append(
                        SymbolDef(
                            name=name,
                            kind="variable",
                            file_path=file_path,
                            line=node.start_point[0] + 1,
                            end_line=node.end_point[0] + 1,
                            start_byte=node.start_byte,
                            end_byte=node.end_byte,
                            signature=sig,
                        )
                    )
                    defined_names.add(name)

        # Recurse into children
        for child in node.children:
            _walk(child, parent_class)

    _walk(root)

    # Extract references (identifiers not in definitions)
    _extract_references(root, file_path, references)

    return definitions, references


def _extract_references(
    root: Any,
    file_path: str,
    references: list[SymbolRef],
) -> None:
    """Extract identifier references from the AST.

    Walks all identifier nodes and collects names that could reference
    symbols defined in other files. Skips common builtins and keywords.
    """
    builtins = {
        "self",
        "cls",
        "None",
        "True",
        "False",
        "print",
        "len",
        "range",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "type",
        "isinstance",
        "issubclass",
        "super",
        "property",
        "staticmethod",
        "classmethod",
        "abstractmethod",
        "dataclass",
        "field",
        "Any",
        "Optional",
        "Union",
        "Literal",
        "Protocol",
        "TypeVar",
    }
    seen_refs: set[str] = set()

    def _walk_refs(node: Any) -> None:
        if node.type == "identifier":
            name = node.text.decode("utf-8")
            if (
                name not in builtins
                and name not in seen_refs
                and len(name) > 1
                and not name.startswith("_")
            ):
                seen_refs.add(name)
                references.append(
                    SymbolRef(
                        name=name,
                        file_path=file_path,
                        line=node.start_point[0] + 1,
                    )
                )
        for child in node.children:
            _walk_refs(child)

    _walk_refs(root)


def _node_first_line(node: Any, content: str) -> str:
    """Get the first line of source for a node."""
    lines = content.splitlines()
    line_idx = node.start_point[0]
    if line_idx < len(lines):
        return lines[line_idx].strip()
    return ""


# ── Regex Fallback Extractor ──────────────────────────────────────────


def _extract_python_regex(
    file_path: str, content: str
) -> tuple[list[SymbolDef], list[SymbolRef]]:
    """Regex-based Python extraction fallback when tree-sitter unavailable."""
    definitions: list[SymbolDef] = []
    references: list[SymbolRef] = []
    lines = content.splitlines()

    current_class: str | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # Track class context
        if stripped.startswith("class "):
            match = re.match(r"class\s+(\w+)", stripped)
            if match:
                name = match.group(1)
                current_class = name
                definitions.append(
                    SymbolDef(
                        name=name,
                        kind="class",
                        file_path=file_path,
                        line=i + 1,
                        signature=stripped.rstrip(":").strip() + ":",
                    )
                )

        elif stripped.startswith("def "):
            match = re.match(r"def\s+(\w+)", stripped)
            if match:
                name = match.group(1)
                kind = "method" if indent > 0 and current_class else "function"
                parent = current_class if kind == "method" else None
                definitions.append(
                    SymbolDef(
                        name=name,
                        kind=kind,
                        file_path=file_path,
                        line=i + 1,
                        signature=stripped.rstrip(":").strip() + ":",
                        parent=parent,
                    )
                )

        elif stripped.startswith(("import ", "from ")):
            definitions.append(
                SymbolDef(
                    name=stripped,
                    kind="import",
                    file_path=file_path,
                    line=i + 1,
                    signature=stripped,
                )
            )

        # Reset class context at top level
        if indent == 0 and not stripped.startswith(
            ("class ", "def ", " ", "\t", "#", "@")
        ):
            if stripped and not stripped.startswith(("import ", "from ", '"""', "'''")):
                current_class = None

    return definitions, references


# ── Multi-Language Dispatch ───────────────────────────────────────────


def extract_file_symbols(
    file_path: str, content: str
) -> tuple[list[SymbolDef], list[SymbolRef]]:
    """Extract definitions and references from a source file.

    Dispatches to tree-sitter when available, falls back to regex.

    Args:
        file_path: Path to the file.
        content: File content as string.

    Returns:
        Tuple of (definitions, references).
    """
    if file_path.endswith(".py"):
        if _TREE_SITTER_AVAILABLE:
            return _extract_python_tree_sitter(file_path, content)
        return _extract_python_regex(file_path, content)

    # Future: add tree-sitter-javascript, tree-sitter-typescript, etc.
    # For now, unsupported languages return empty
    return [], []


def is_tree_sitter_available() -> bool:
    """Check if tree-sitter is available for Python parsing."""
    return _TREE_SITTER_AVAILABLE


# ── Attribute Access Extraction (Data Flow Tracing) ───────────────────


@dataclass
class AttributeAccess:
    """A single attribute access found within a function body.

    Represents patterns like ``obj.attr``, ``self.items[x].name``,
    ``param.field``.  The chain captures the full dotted path from
    the root object to the accessed attribute.
    """

    chain: str  # e.g. "self.items.name", "cmd.args"
    root: str  # leftmost name, e.g. "self", "cmd", "item"
    attribute: str  # rightmost name, e.g. "name", "args"
    line: int  # 1-indexed source line


@dataclass
class FunctionAccesses:
    """Attribute accesses grouped by the function they occur in."""

    function_name: str  # e.g. "_handle_take" or "GameEngine._handle_take"
    kind: str  # "function" or "method"
    parent_class: str | None  # class name for methods
    parameters: list[str]  # parameter names (excluding self/cls)
    accesses: list[AttributeAccess] = field(default_factory=list)


def extract_attribute_accesses(
    file_path: str,
    content: str,
) -> list[FunctionAccesses]:
    """Extract attribute access patterns from functions in a source file.

    Uses tree-sitter to walk into function bodies (which the symbol
    extractor deliberately skips) and collect every ``attribute`` node.
    Each access is recorded as a dotted chain from the root identifier
    to the final attribute, e.g. ``self.current_room.items``.

    This is pure structural extraction — no type inference.  The caller
    (projection materializer) cross-references the results against known
    class field signatures and interface contracts to give the LLM the
    evidence it needs for semantic reasoning.

    Language support follows the same dispatch pattern as
    ``extract_file_symbols``: Python via tree-sitter today, extensible
    to other grammars by adding a ``_extract_accesses_<lang>`` function.

    Falls back to an empty list when tree-sitter is unavailable.

    Args:
        file_path: Path to the source file (used for language dispatch).
        content: Full file content as string.

    Returns:
        List of FunctionAccesses, one per function/method in the file.
    """
    if file_path.endswith(".py"):
        if _TREE_SITTER_AVAILABLE:
            return _extract_accesses_python(content)
        return _extract_accesses_python_ast(content)

    # Future: add extractors for other tree-sitter grammars
    return []


def _extract_accesses_python(content: str) -> list[FunctionAccesses]:
    """Extract attribute accesses from Python using tree-sitter.

    Walks the full AST including function bodies.  For each function
    or method definition, collects all ``attribute`` nodes and builds
    dotted access chains by walking up from the attribute node through
    nested attribute parents.
    """
    if not _TREE_SITTER_AVAILABLE or _PYTHON_LANGUAGE is None:
        return []

    parser = Parser(_PYTHON_LANGUAGE)
    tree = parser.parse(content.encode("utf-8"))
    root = tree.root_node
    results: list[FunctionAccesses] = []

    def _collect_from_function(
        func_node: Any,
        parent_class: str | None,
    ) -> FunctionAccesses:
        """Collect attribute accesses from a single function node."""
        name_node = func_node.child_by_field_name("name")
        func_name = name_node.text.decode("utf-8") if name_node else "<anonymous>"

        # Extract parameter names
        params_node = func_node.child_by_field_name("parameters")
        param_names: list[str] = []
        if params_node:
            for child in params_node.children:
                if child.type == "identifier":
                    pname = child.text.decode("utf-8")
                    if pname not in ("self", "cls"):
                        param_names.append(pname)
                elif child.type in (
                    "typed_parameter",
                    "default_parameter",
                    "typed_default_parameter",
                ):
                    # First identifier child is the parameter name
                    for sub in child.children:
                        if sub.type == "identifier":
                            pname = sub.text.decode("utf-8")
                            if pname not in ("self", "cls"):
                                param_names.append(pname)
                            break

        kind = "method" if parent_class else "function"
        fa = FunctionAccesses(
            function_name=func_name,
            kind=kind,
            parent_class=parent_class,
            parameters=param_names,
        )

        # Walk the function body to find all attribute access nodes
        body = func_node.child_by_field_name("body")
        if body:
            _collect_attributes(body, fa)

        return fa

    def _collect_attributes(node: Any, fa: FunctionAccesses) -> None:
        """Recursively collect attribute nodes from an AST subtree."""
        if node.type == "attribute":
            chain, root_name = _build_access_chain(node)
            if chain and root_name:
                attr_node = node.child_by_field_name("attribute")
                attr_name = (
                    attr_node.text.decode("utf-8")
                    if attr_node
                    else chain.rsplit(".", 1)[-1]
                )
                fa.accesses.append(
                    AttributeAccess(
                        chain=chain,
                        root=root_name,
                        attribute=attr_name,
                        line=node.start_point[0] + 1,
                    )
                )
            # Don't recurse into children of an attribute node —
            # the chain builder already walked down to the root.
            return

        # Skip nested function/class definitions — they have their own scope
        if node.type in ("function_definition", "class_definition"):
            return

        for child in node.children:
            _collect_attributes(child, fa)

    def _build_access_chain(node: Any) -> tuple[str, str]:
        """Build a dotted chain from an attribute node.

        Walks down through nested attribute nodes to find the root
        identifier, then builds the chain bottom-up.

        Returns (chain_str, root_name).  Returns ("", "") if the
        chain root is not a simple identifier (e.g. a function call
        result).
        """
        parts: list[str] = []
        current = node

        while current.type == "attribute":
            attr = current.child_by_field_name("attribute")
            if attr:
                parts.append(attr.text.decode("utf-8"))
            obj = current.child_by_field_name("object")
            if obj is None:
                break
            # Skip through subscript nodes: self.items[x].name
            # The object of the outer attribute is a subscript whose
            # value is the inner attribute/identifier we want.
            if obj.type == "subscript":
                obj = obj.child_by_field_name("value")
                if obj is None:
                    break
            current = obj

        if current.type == "identifier":
            root_name = current.text.decode("utf-8")
            parts.append(root_name)
            parts.reverse()
            return ".".join(parts), root_name

        # Root is not a simple identifier (e.g. function call result)
        return "", ""

    # Walk top-level and class-level to find all function definitions
    def _walk_for_functions(node: Any, parent_class: str | None = None) -> None:
        if node.type == "function_definition":
            fa = _collect_from_function(node, parent_class)
            if fa.accesses:  # Only include functions that have accesses
                results.append(fa)
            return  # Don't recurse into nested functions

        if node.type == "class_definition":
            class_name_node = node.child_by_field_name("name")
            class_name = (
                class_name_node.text.decode("utf-8") if class_name_node else None
            )
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    _walk_for_functions(child, parent_class=class_name)
            return

        # Handle decorated definitions
        if node.type == "decorated_definition":
            for child in node.children:
                if child.type in ("function_definition", "class_definition"):
                    _walk_for_functions(child, parent_class)
            return

        for child in node.children:
            _walk_for_functions(child, parent_class)

    _walk_for_functions(root)
    return results


def _extract_accesses_python_ast(content: str) -> list[FunctionAccesses]:
    """Fallback: extract attribute accesses using stdlib ast.

    Less precise than tree-sitter (no subscript transparency, simpler
    chain building) but works without external dependencies.
    """
    import ast as ast_mod

    try:
        tree = ast_mod.parse(content)
    except SyntaxError:
        return []

    results: list[FunctionAccesses] = []

    for node in ast_mod.walk(tree):
        if not isinstance(node, (ast_mod.FunctionDef, ast_mod.AsyncFunctionDef)):
            continue

        # Determine parent class
        parent_class = None
        for cls_node in ast_mod.walk(tree):
            if isinstance(cls_node, ast_mod.ClassDef):
                for item in cls_node.body:
                    if item is node:
                        parent_class = cls_node.name
                        break
                # Also check decorated items
                for item in cls_node.body:
                    if hasattr(item, "body") and isinstance(item, ast_mod.FunctionDef):
                        continue

        # Extract parameters
        param_names = [a.arg for a in node.args.args if a.arg not in ("self", "cls")]

        fa = FunctionAccesses(
            function_name=node.name,
            kind="method" if parent_class else "function",
            parent_class=parent_class,
            parameters=param_names,
        )

        # Walk function body for Attribute nodes
        for child in ast_mod.walk(node):
            if isinstance(child, ast_mod.Attribute):
                chain, root = _build_chain_ast(child)
                if chain and root:
                    fa.accesses.append(
                        AttributeAccess(
                            chain=chain,
                            root=root,
                            attribute=child.attr,
                            line=getattr(child, "lineno", 0),
                        )
                    )

        if fa.accesses:
            results.append(fa)

    return results


def _build_chain_ast(node: Any) -> tuple[str, str]:
    """Build a dotted access chain from an ast.Attribute node."""
    import ast as ast_mod

    parts: list[str] = []
    current = node

    while isinstance(current, ast_mod.Attribute):
        parts.append(current.attr)
        current = current.value
        # Skip through subscripts
        if isinstance(current, ast_mod.Subscript):
            current = current.value

    if isinstance(current, ast_mod.Name):
        parts.append(current.id)
        parts.reverse()
        return ".".join(parts), current.id

    return "", ""


# ── Sibling-Site Search (systematic-defect discovery) ─────────────────


@dataclass
class AccessSiteMatch:
    """A project-wide site where ``<root>.<attribute>`` is accessed."""

    file_path: str
    function: str  # qualified enclosing def, e.g. "GameEngine.process_command"
    line: int  # 1-indexed source line
    chain: str  # full dotted access, e.g. "command.name"
    root: str  # leftmost identifier, e.g. "command"
    attribute: str  # accessed member, e.g. "name"


def find_attribute_access_sites(
    files: dict[str, str],
    attribute: str,
    root_name: str | None = None,
) -> list[AccessSiteMatch]:
    """Find every ``<root>.<attribute>`` access across a set of files.

    The structural complement to the name/call-graph reference walk. Contract
    and field-mismatch defects — the most common *systematic* bug — take the
    shape of an attribute access repeated across sites (e.g. ``command.name`` in
    N places where the ``Command`` type defines no ``name``). Those are
    invisible to a symbol-name regex or an import-reference graph: ``.name`` is
    not a reference to any *symbol*, so neither ``_find_same_file_refs`` nor the
    import-only ``references`` list can see it.

    This reuses :func:`extract_attribute_accesses` (tree-sitter, with a stdlib
    ``ast`` fallback) per file, so it is structural — no type inference.
    ``attribute`` is the accessed member (e.g. ``"name"``). ``root_name``, when
    given, keeps only chains whose leftmost identifier matches exactly (e.g.
    ``root_name="command"`` → ``command.name`` but not ``item.name``).
    Object-name narrowing approximates type-narrowing without a resolver: high
    recall for the copy-paste defect class, with the imprecision absorbed
    downstream by existence-checking + the patch's no-op-on-irrelevant behavior.

    Returns one match per access site, in (file, source) order.
    """
    if not attribute:
        return []
    matches: list[AccessSiteMatch] = []
    for path, content in files.items():
        if not path.endswith(".py") or not content:
            continue
        try:
            fns = extract_attribute_accesses(path, content)
        except Exception:  # noqa: BLE001 - skip unparseable files, never raise
            continue
        for fa in fns:
            qualified = (
                f"{fa.parent_class}.{fa.function_name}"
                if fa.parent_class
                else fa.function_name
            )
            for acc in fa.accesses:
                if acc.attribute != attribute:
                    continue
                if root_name is not None and acc.root != root_name:
                    continue
                matches.append(
                    AccessSiteMatch(
                        file_path=path,
                        function=qualified,
                        line=acc.line,
                        chain=acc.chain,
                        root=acc.root,
                        attribute=acc.attribute,
                    )
                )
    return matches


# ── Graph Building & PageRank ─────────────────────────────────────────


def build_repo_map(files: dict[str, str]) -> RepoMap:
    """Build a complete repository map from file contents.

    1. Extracts definitions and references from each file.
    2. Builds a directed graph: referencer → definer, weighted by ref count.
    3. Runs PageRank to rank files by importance.

    Args:
        files: Dictionary mapping file paths to file contents.

    Returns:
        RepoMap with all extracted data and rankings.
    """
    file_infos: dict[str, FileInfo] = {}

    # Step 1: Extract symbols from each file
    for file_path, content in files.items():
        defs, refs = extract_file_symbols(file_path, content)

        lang = "unknown"
        if file_path.endswith(".py"):
            lang = "python"
        elif file_path.endswith((".js", ".jsx")):
            lang = "javascript"
        elif file_path.endswith((".ts", ".tsx")):
            lang = "typescript"
        elif file_path.endswith(".rs"):
            lang = "rust"

        file_infos[file_path] = FileInfo(
            path=file_path,
            definitions=defs,
            references=refs,
            language=lang,
        )

    # Step 2: Build definition index
    # name → list of files that define it
    defines: dict[str, list[str]] = defaultdict(list)
    for fp, info in file_infos.items():
        for defn in info.definitions:
            if defn.kind not in ("import",):  # Skip imports as definitions
                defines[defn.name].append(fp)

    # Step 3: Build directed graph
    G = nx.DiGraph()
    for fp in file_infos:
        G.add_node(fp)

    for fp, info in file_infos.items():
        ref_counts: dict[str, int] = defaultdict(int)
        for ref in info.references:
            ref_counts[ref.name] += 1

        for name, count in ref_counts.items():
            if name in defines:
                definers = defines[name]
                num_defs = len(definers)
                for definer_fp in definers:
                    if definer_fp != fp:
                        # Edge weight: refs / num_definers
                        weight = count / num_defs
                        if G.has_edge(fp, definer_fp):
                            G[fp][definer_fp]["weight"] += weight
                        else:
                            G.add_edge(fp, definer_fp, weight=weight)

    # Step 4: PageRank
    if G.number_of_nodes() > 0:
        try:
            rankings = nx.pagerank(G, weight="weight")
        except nx.NetworkXError:
            # Graph might be empty or disconnected
            rankings = {fp: 1.0 / len(file_infos) for fp in file_infos}
    else:
        rankings = {fp: 1.0 / max(len(file_infos), 1) for fp in file_infos}

    # Ensure all files have a ranking (even if not in graph)
    for fp in file_infos:
        if fp not in rankings:
            rankings[fp] = 0.0

    return RepoMap(files=file_infos, file_rankings=rankings)
