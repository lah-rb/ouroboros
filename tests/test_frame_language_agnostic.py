"""Frame + symbol extraction are language-agnostic, not Python-only.

Step 1: the diagnose symbol table / bodies cover any tree-sitter grammar (bash,
js, …), so `trace` works on non-Python code (the processing-pipeline gap, where a
.sh script's functions were invisible because the table was .py-gated).
Step 2: the frame editor (build_frame / splice_frame) edits the non-symbol frame
of ANY language, validated per-language — Python via stdlib ast (precise), others
via tree-sitter error nodes — re-based on repomap's multi-language byte offsets.
"""

from __future__ import annotations

import pytest

from agent.repomap import is_tree_sitter_available

pytestmark = pytest.mark.skipif(
    not is_tree_sitter_available(), reason="tree-sitter grammars unavailable"
)

BASH = (
    "#!/usr/bin/env bash\nset -e\n"
    "run_pipeline() {\n  echo hi\n}\n"
    "main() {\n  run_pipeline\n}\nmain\n"
)
JS = "import x from 'y'\nfunction build() { return 1 }\nclass App {}\n"


# ── Step 1: symbol table / bodies are multi-language ───────────────────
def test_symbol_table_covers_bash_and_js():
    from agent.projections import _extract_symbol_table

    bash = {(s["name"], s["kind"]) for s in _extract_symbol_table("p.sh", BASH)}
    assert ("run_pipeline", "function") in bash and ("main", "function") in bash
    js = {(s["name"], s["kind"]) for s in _extract_symbol_table("a.js", JS)}
    assert ("build", "function") in js and ("App", "class") in js


def test_symbol_bodies_sliced_for_bash():
    from agent.projections import _extract_imported_symbol_bodies

    bodies = _extract_imported_symbol_bodies("p.sh", BASH, ["run_pipeline"])
    assert "echo hi" in bodies.get("run_pipeline", "")


def test_symbol_less_file_yields_empty_table():
    from agent.projections import _extract_symbol_table

    # No functions → empty table; the trace full-file fallback handles display.
    assert _extract_symbol_table("x.sh", "echo a\necho b\n") == []


# ── Step 2: frame editor is language-agnostic ──────────────────────────
def test_bash_frame_roundtrips():
    from agent.actions.frame_actions import build_frame, splice_frame

    frame, preserved, ok, _ = build_frame("p.sh", BASH)
    assert ok and set(preserved) == {"run_pipeline", "main"}
    assert "OUROBOROS-SYMBOL run_pipeline" in frame  # body replaced by sentinel
    assert "set -e" in frame and "echo hi" not in frame  # top-level kept, body hidden
    edited = frame.replace("set -e\n", "set -euo pipefail\n")
    content, sok, _ = splice_frame(edited, preserved, "p.sh")
    assert sok
    assert "set -euo pipefail" in content and "echo hi" in content  # edit + body back


def test_splice_rejects_broken_nonpython_edit_via_tree_sitter():
    from agent.actions.frame_actions import build_frame, splice_frame

    frame, preserved, _, _ = build_frame("p.sh", BASH)
    broken = frame.replace("main\n", "main(\n")  # introduce a bash syntax error
    content, ok, reason = splice_frame(broken, preserved, "p.sh")
    assert ok is False and "tree-sitter" in reason


def test_python_frame_still_validated_by_stdlib_ast():
    from agent.actions.frame_actions import build_frame, splice_frame

    py = "import os\ndef f():\n    return 1\n"
    frame, preserved, ok, _ = build_frame("a.py", py)
    edited = frame.replace("import os\n", "import os\nimport sys\n")
    content, sok, _ = splice_frame(edited, preserved, "a.py")
    assert sok and "import sys" in content and "return 1" in content


def test_frame_lang_label_and_fence():
    from agent.actions.frame_actions import _frame_lang

    assert _frame_lang("x.sh") == ("shell script", "bash")
    assert _frame_lang("x.go") == ("Go file", "go")
    assert _frame_lang("x.py") == ("Python file", "python")
    assert _frame_lang("x.zzz") == ("file", "")
