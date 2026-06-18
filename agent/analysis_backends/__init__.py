"""Per-language deep static-analysis backends — the pluggable "deep" tier.

Code analysis splits into two tiers. The SHALLOW tier (symbol *definitions* via
tree-sitter) is universal and lives in ``repomap`` for every language. The DEEP tier
— reference resolution and attribute-access tracing (the inputs to call-graph
ranking, the unresolved-reference scan, and the contract/copy-paste sibling-defect
scan) — is per-language and pluggable HERE, selected by the ``agent/languages.py``
registry.

Python is the only language with a real deep backend today (tree-sitter now; jedi
added behind the same facade). Every other language gets the null backend (empty),
exactly as before — non-Python deep analysis was always empty. A future LSP backend
(gopls / tsserver / …) slots in as one new module + one ``get_backend`` branch,
touching no consumer. See ``dev/DEEP_ANALYSIS_BACKENDS.md`` for the landscape.

Import discipline: this package imports only the leaf modules (``analysis_types``,
``languages``) at load time; the tree-sitter backend reaches repomap's machinery via
a function-local import, so the dependency graph stays acyclic
(``analysis_types`` ← ``analysis_backends`` ← ``repomap`` ← consumers).
"""

from __future__ import annotations

import logging
from typing import Protocol

from agent import languages
from agent.analysis_backends import jedi_python
from agent.analysis_backends.null_backend import NullBackend
from agent.analysis_backends.treesitter_python import TreeSitterPythonBackend
from agent.analysis_types import FunctionAccesses, SymbolRef

logger = logging.getLogger(__name__)


class DeepAnalysisBackend(Protocol):
    """The contract a per-language deep backend satisfies. Two operations — the
    definitions tier (SymbolDef) is deliberately NOT here; it's the universal
    shallow layer in repomap."""

    name: str

    def references(self, file_path: str, content: str) -> list[SymbolRef]: ...

    def attribute_accesses(
        self, file_path: str, content: str
    ) -> list[FunctionAccesses]: ...


class PythonBackend:
    """Facade for Python deep analysis.

    The jedi↔tree-sitter preference order lives HERE, not in ``get_backend`` — so the
    selector stays purely language-keyed and a future per-language backend (e.g. a Go
    gopls+tree-sitter pair) brings its own internal fallback chain without changing
    the selector. This sprint it is tree-sitter only; ``jedi_python`` is wired in at
    Step 3 as the preferred ``references`` engine with this as the fallback.
    """

    name = "python"

    def __init__(self) -> None:
        self._ts = TreeSitterPythonBackend()

    def references(self, file_path: str, content: str) -> list[SymbolRef]:
        # Prefer jedi (scope/import-aware); fall back to the tree-sitter walk when
        # jedi is absent (optional dep) or raises — the fallback is byte-identical
        # to the pre-seam behavior, so a jedi-less install loses nothing.
        if jedi_python.jedi_available():
            try:
                return jedi_python.references(file_path, content)
            except Exception as e:  # noqa: BLE001 — never let analysis break a run
                logger.debug(
                    "jedi references failed for %s (%s) — tree-sitter fallback",
                    file_path,
                    e,
                )
        return self._ts.references(file_path, content)

    def attribute_accesses(
        self, file_path: str, content: str
    ) -> list[FunctionAccesses]:
        # Stays on tree-sitter regardless of jedi: the consumers use accesses
        # STRUCTURALLY (syntactic chain/root/attribute), not semantically.
        return self._ts.attribute_accesses(file_path, content)


_PYTHON: DeepAnalysisBackend = PythonBackend()
_NULL: DeepAnalysisBackend = NullBackend()


def get_backend(path: str) -> DeepAnalysisBackend:
    """Select the deep-analysis backend for a file by its language.

    Python → the Python facade; every other / unknown language → the null backend
    (empty). Keyed on the canonical registry so adding a language's backend is a
    one-line change here.
    """
    spec = languages.spec_for_path(path)
    if spec is not None and spec.name == "python":
        return _PYTHON
    return _NULL
