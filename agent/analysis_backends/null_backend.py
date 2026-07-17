"""The default deep-analysis backend: no deep analysis (empty results).

Used for every language without a real backend — i.e. everything except Python
today. Matches the pre-seam behavior exactly: non-Python files produced zero
references and zero attribute-accesses (the deep Python heuristics downstream were
simply dormant for them).
"""

from __future__ import annotations

from agent.analysis_types import FunctionAccesses, SymbolRef


class NullBackend:
    """Empty deep analysis. The honest default until a language gets a real backend."""

    name = "null"

    def references(self, file_path: str, content: str) -> list[SymbolRef]:
        return []

    def attribute_accesses(
        self, file_path: str, content: str
    ) -> list[FunctionAccesses]:
        return []
