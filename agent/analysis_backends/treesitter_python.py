"""The tree-sitter Python deep-analysis backend — the default/fallback for Python.

Delegates to repomap's existing Python tree-sitter walks (reached via a function-
local import so this package keeps NO module-level dependency on repomap — the
dependency graph stays acyclic: analysis_types ← analysis_backends ← repomap). This
is the byte-for-byte behavior the seam preserves; the jedi backend layers on top as
the preferred reference engine and falls back here on any error.
"""

from __future__ import annotations

from agent.analysis_types import FunctionAccesses, SymbolRef


class TreeSitterPythonBackend:
    """Python references + attribute-accesses via repomap's tree-sitter extractors
    (stdlib-ast fallback when tree-sitter is unavailable) — unchanged from pre-seam."""

    name = "treesitter-python"

    def references(self, file_path: str, content: str) -> list[SymbolRef]:
        from agent import repomap  # function-local: avoid an import cycle

        if repomap._TREE_SITTER_AVAILABLE:
            return repomap._extract_python_tree_sitter(file_path, content)[1]
        return repomap._extract_python_regex(file_path, content)[1]

    def attribute_accesses(
        self, file_path: str, content: str
    ) -> list[FunctionAccesses]:
        from agent import repomap

        if repomap._TREE_SITTER_AVAILABLE:
            return repomap._extract_accesses_python(content)
        return repomap._extract_accesses_python_ast(content)
