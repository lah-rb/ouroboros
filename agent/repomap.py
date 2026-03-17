"""AST-based repository map — tree-sitter parsing with PageRank ranking.

Inspired by Aider's repo map approach. Extracts symbol definitions and
references using tree-sitter, builds a file dependency graph, ranks files
with PageRank, and formats a token-budgeted map for LLM consumption.

Falls back to regex-based extraction for unsupported languages.
"""

from __future__ import annotations

import os
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
    signature: str  # e.g. "def process(items: list[str]) -> Result:"
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
                definitions.append(
                    SymbolDef(
                        name=name,
                        kind="class",
                        file_path=file_path,
                        line=node.start_point[0] + 1,
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
                definitions.append(
                    SymbolDef(
                        name=name,
                        kind=kind,
                        file_path=file_path,
                        line=node.start_point[0] + 1,
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
                    definitions.append(
                        SymbolDef(
                            name=name,
                            kind="variable",
                            file_path=file_path,
                            line=node.start_point[0] + 1,
                            signature=sig,
                        )
                    )
                    defined_names.add(name)

        # Recurse into children
        for child in node.children:
            _walk(child, parent_class)

    _walk(root)

    # Extract references (identifiers not in definitions)
    _extract_references(root, file_path, defined_names, references)

    return definitions, references


def _extract_references(
    root: Any,
    file_path: str,
    defined_names: set[str],
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


# ── Graph Building & PageRank ─────────────────────────────────────────


def build_repo_map(
    files: dict[str, str],
    root_dir: str = ".",
) -> RepoMap:
    """Build a complete repository map from file contents.

    1. Extracts definitions and references from each file.
    2. Builds a directed graph: referencer → definer, weighted by ref count.
    3. Runs PageRank to rank files by importance.

    Args:
        files: Dictionary mapping file paths to file contents.
        root_dir: Root directory for relative path computation.

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
