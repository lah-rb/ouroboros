"""Shared data models for code analysis — a dependency-light leaf.

These dataclasses are the contract between the analysis PRODUCERS (the tree-sitter
machinery in ``repomap.py`` and the per-language ``analysis_backends``) and the many
CONSUMERS (projections, research/diagnosis actions, frame/ast/trace actions). They
were hoisted out of ``repomap.py`` so the backend seam can return them without an
import cycle: ``analysis_types`` (this leaf) ← ``analysis_backends`` ← ``repomap`` ←
consumers is a clean DAG.

Like ``agent/languages.py``, this module is a LEAF: it imports only the stdlib and
NOTHING from ``agent`` or ``tree_sitter*``. The graph/PageRank machinery and the
parsers stay in ``repomap.py``; only the static data shapes live here. ``repomap``
re-exports every name below, so ``from agent.repomap import SymbolDef`` (etc.) keeps
working unchanged — the same class object, imported once.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal


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


@dataclass
class AccessSiteMatch:
    """A project-wide site where ``<root>.<attribute>`` is accessed."""

    file_path: str
    function: str  # qualified enclosing def, e.g. "GameEngine.process_command"
    line: int  # 1-indexed source line
    chain: str  # full dotted access, e.g. "command.name"
    root: str  # leftmost identifier, e.g. "command"
    attribute: str  # accessed member, e.g. "name"
