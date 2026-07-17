"""DeepAnalysisBackend seam — selection + byte-for-byte characterization.

The seam routes the DEEP analysis tier (references + attribute accesses) per-language.
This sprint only Python has a real backend (tree-sitter; jedi layered on separately);
every other language gets the null backend. These tests are the regression net for the
refactor: the delegating gates must reproduce the underlying extractors EXACTLY, and
non-Python must stay empty (its pre-seam behavior).
"""

from __future__ import annotations

import pytest

from agent import repomap as R
from agent.analysis_backends import NullBackend, PythonBackend, get_backend

pytestmark = pytest.mark.skipif(
    not R.is_tree_sitter_available(), reason="tree-sitter grammars unavailable"
)

PY = """import os


class Cmd:
    def run(self):
        return self.name + os.sep


def helper(c):
    return c.run()


helper(Cmd())
"""

GO = 'package main\nimport "fmt"\nfunc greet() { fmt.Println("x") }\n'
JS = "import x from 'y'\nfunction build(o) { return o.value }\nbuild({value: 1})\n"


# ── selection ──────────────────────────────────────────────────────────
def test_get_backend_selects_by_language():
    assert isinstance(get_backend("a.py"), PythonBackend)
    for path in ("a.go", "a.js", "a.ts", "a.sh", "a.rs", "noext", "a.unknownext"):
        assert isinstance(get_backend(path), NullBackend), path
    assert get_backend("a.py").name == "python"
    assert get_backend("a.go").name == "null"


# ── tree-sitter backend = byte-identical fallback ──────────────────────
def test_treesitter_backend_is_byte_identical_to_repomap_walk():
    # The fallback (used when jedi is absent/raises) must reproduce the pre-seam
    # tree-sitter walk exactly — both references and attribute accesses.
    from agent.analysis_backends.treesitter_python import TreeSitterPythonBackend

    b = TreeSitterPythonBackend()
    assert b.references("m.py", PY) == R._extract_python_tree_sitter("m.py", PY)[1]
    assert b.attribute_accesses("m.py", PY) == R._extract_accesses_python(PY)


def test_attribute_accesses_stay_treesitter_even_with_jedi():
    # attribute_accesses are STRUCTURAL — jedi does not own them; they remain the
    # exact tree-sitter walk regardless of jedi being installed.
    assert R.extract_attribute_accesses("m.py", PY) == R._extract_accesses_python(PY)
    sites = R.find_attribute_access_sites({"m.py": PY}, "run", root_name="c")
    assert any(s.attribute == "run" for s in sites)


# ── jedi: the intended upgrade + the fallback contract ─────────────────
def test_jedi_excludes_definition_names_treesitter_includes():
    # The concrete improvement: a function defined but never referenced is wrongly
    # reported as a reference by the syntactic tree-sitter walk; jedi (scope-aware)
    # excludes definitions.
    from agent.analysis_backends import jedi_python

    if not jedi_python.jedi_available():
        pytest.skip("jedi not installed")
    src = "def helper():\n    return 1\n"
    ts_names = {r.name for r in R._extract_python_tree_sitter("m.py", src)[1]}
    jedi_names = {r.name for r in jedi_python.references("m.py", src)}
    assert "helper" in ts_names  # tree-sitter false positive
    assert "helper" not in jedi_names  # jedi correctly omits the definition


def test_python_references_fall_back_to_treesitter_without_jedi(monkeypatch):
    # jedi-absent install loses nothing: the Python backend reproduces the
    # tree-sitter walk byte-for-byte.
    from agent.analysis_backends import jedi_python

    monkeypatch.setattr(jedi_python, "_JEDI_AVAILABLE", False)
    b = get_backend("m.py")
    assert b.references("m.py", PY) == R._extract_python_tree_sitter("m.py", PY)[1]
    rm = R.build_repo_map({"m.py": PY})
    assert rm.files["m.py"].references == R._extract_python_tree_sitter("m.py", PY)[1]


def test_python_references_fall_back_when_jedi_raises(monkeypatch):
    from agent.analysis_backends import jedi_python

    def _boom(file_path, content):
        raise RuntimeError("jedi exploded")

    monkeypatch.setattr(jedi_python, "_JEDI_AVAILABLE", True)
    monkeypatch.setattr(jedi_python, "references", _boom)
    b = get_backend("m.py")
    # never raises — degrades to the tree-sitter walk
    assert b.references("m.py", PY) == R._extract_python_tree_sitter("m.py", PY)[1]


# ── non-Python: the null backend keeps today's empty behavior ──────────
def test_non_python_deep_analysis_is_empty():
    for path, src in (("m.go", GO), ("a.js", JS), ("run.sh", "echo hi\n")):
        rm = R.build_repo_map({path: src})
        assert rm.files[path].references == [], path
        assert R.extract_attribute_accesses(path, src) == [], path
        assert get_backend(path).references(path, src) == []
        assert get_backend(path).attribute_accesses(path, src) == []


# ── leaf/cycle discipline ──────────────────────────────────────────────
def test_analysis_backends_has_no_module_level_repomap_import():
    """The package must not import repomap at module load (would cycle); the
    tree-sitter backend reaches it function-locally."""
    import ast
    import pathlib

    import agent.analysis_backends as pkg

    pkg_dir = pathlib.Path(pkg.__file__).parent
    for py_file in pkg_dir.glob("*.py"):
        tree = ast.parse(py_file.read_text())
        # collect only MODULE-LEVEL imports (depth 1 in the module body)
        for node in tree.body:
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            assert (
                "agent.repomap" not in mods
            ), f"{py_file.name} imports repomap at module level — cycle risk"
