"""jedi-backed Python references — scope/import-aware, in-memory.

jedi is the preferred Python reference engine (see dev/DEEP_ANALYSIS_BACKENDS.md):
it is scope-aware, so it excludes *definition* occurrences that the syntactic
tree-sitter walk wrongly reports as references (e.g. a function's own name at its
`def` site), and it follows imports/scoping rather than matching bare identifiers.

It runs on the in-memory source STRING only — `jedi.Script(code=content,
path=file_path)` — analysing the names *within* the given file. It is never pointed
at the host filesystem/venv (which would be the wrong project's environment); only
the file's own content is analysed, which is exactly the scope the two consumers
(PageRank related-file ranking + the "referenced-but-never-defined-in-project" scan)
want.

To keep the change low-churn, the SAME filters as the tree-sitter walk are applied
(builtin names, length > 1, no leading underscore, dedupe by name) so the only
behavioural difference is jedi's better classification. Any failure raises to the
caller (``PythonBackend.references``), which falls back to the tree-sitter walk.
"""

from __future__ import annotations

from agent.analysis_types import SymbolRef

_JEDI_AVAILABLE = False
try:
    import jedi

    _JEDI_AVAILABLE = True
except ImportError:  # optional dependency — fall back to tree-sitter
    jedi = None  # type: ignore[assignment]


# Mirror agent.repomap._extract_references' builtin filter so the ref SET differs
# from tree-sitter only by jedi's scoping, not by a different skip-list.
_BUILTIN_NAMES = frozenset({
    "self", "cls", "None", "True", "False", "print", "len", "range", "str", "int",
    "float", "bool", "list", "dict", "set", "tuple", "type", "isinstance",
    "issubclass", "super", "property", "staticmethod", "classmethod",
    "abstractmethod", "dataclass", "field", "Any", "Optional", "Union", "Literal",
    "Protocol", "TypeVar",
})


def jedi_available() -> bool:
    return _JEDI_AVAILABLE


def references(file_path: str, content: str) -> list[SymbolRef]:
    """Scope-aware references for a Python source string via jedi.

    Returns one ``SymbolRef`` per unique referenced name (first occurrence),
    excluding definitions, builtins, single-character names, and dunder/private
    names — matching the tree-sitter walk's contract. Raises propagate to the
    caller's fallback.
    """
    script = jedi.Script(code=content, path=file_path)
    names = script.get_names(all_scopes=True, definitions=False, references=True)
    out: list[SymbolRef] = []
    seen: set[str] = set()
    for n in names:
        name = n.name
        if (
            name in seen
            or name in _BUILTIN_NAMES
            or len(name) <= 1
            or name.startswith("_")
        ):
            continue
        seen.add(name)
        out.append(SymbolRef(name=name, file_path=file_path, line=n.line))
    return out
