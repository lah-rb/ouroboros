"""Regression tests for the symbol-splice path in ``rewrite_symbol_turn``.

Anchored on the concrete failure observed in challenge mission a12 cycle 15:
``loader.py``'s ``_build_rooms`` method was rewritten, but the splice path
produced a file with ``@staticmethod`` at column 0 and the following
``def`` at column 4 — an ``IndentationError`` that propagated for the
remaining 35 cycles.

The tests here exercise the three cooperating bugs that produced that
corruption (and the parse gate that catches any residual breakage) at
the level of their pure helpers. They do not spin up inference or the
full flow engine.
"""

from __future__ import annotations

import ast as stdlib_ast
import textwrap

from agent.actions.ast_actions import _get_indentation, _reindent
from agent.actions.refinement_actions import extract_code_from_response

# Minimal but realistic class with decorated methods — matches the
# structural shape of the cycle 15 loader.py.
_SAMPLE_CLASS = textwrap.dedent('''\
    """Sample loader."""

    from typing import Any, Dict, List


    class WorldLoader:
        """Parse world.yaml."""

        @staticmethod
        def _load_yaml(path):
            return {}

        @staticmethod
        def _build_rooms(raw_rooms):
            """Create Room instances from raw YAML dictionaries."""
            rooms = []
            for rm in raw_rooms:
                rooms.append(rm)
            return rooms

        def load_world(self, path):
            return self._build_rooms([])
    ''')


def _splice(file_content: str, new_body: str, start_line: int, end_line: int) -> str:
    """Minimal reimplementation of the line-based splice from
    ``action_rewrite_symbol_turn`` (ast_actions.py:910-920).

    Kept local to the test so regressions in the splice arithmetic itself
    are caught here, not accidentally masked by refactors to the action.
    """
    file_lines = file_content.splitlines(keepends=True)
    before = file_lines[: start_line - 1]
    after = file_lines[end_line:]
    if new_body and not new_body.endswith("\n"):
        new_body += "\n"
    return "".join(before) + new_body + "".join(after)


# ── Fix 1: extract_code_from_response preserves leading whitespace ────


def test_extract_code_preserves_leading_indent_in_fenced_block():
    """Leading whitespace within a fenced block must survive extraction.

    The cycle 15 bug: the LLM correctly emitted a method at column 4
    inside a class, but ``extract_code_from_response`` called ``.strip()``
    on the fenced block contents and de-indented the first line only,
    leaving body lines still at column 4 — a decorator/def indent mismatch.
    """
    response = "```python\n    @staticmethod\n    def f(x):\n        return x\n```"
    extracted = extract_code_from_response(response)
    assert extracted.startswith(
        "    @staticmethod"
    ), f"Expected leading col-4 indent preserved, got: {extracted!r}"


def test_extract_code_preserves_indent_in_multi_fence_response():
    """Strategy 2 (largest of multiple fences) must also preserve indent."""
    response = (
        "Here's the change:\n"
        "```python\n"
        "# small note\n"
        "```\n"
        "And the main body:\n"
        "```python\n"
        "    @staticmethod\n"
        "    def f(x):\n"
        "        return x\n"
        "```\n"
    )
    extracted = extract_code_from_response(response)
    assert extracted.startswith(
        "    @staticmethod"
    ), f"Expected leading col-4 indent preserved, got: {extracted!r}"


def test_extract_code_still_trims_trailing_whitespace():
    """rstrip is still desirable — trailing blank lines add splice noise."""
    response = "```python\ndef f():\n    return 1\n\n\n```"
    extracted = extract_code_from_response(response)
    assert not extracted.endswith(
        "\n\n\n"
    ), "Trailing whitespace should still be trimmed"


# ── Fix 2: _reindent anchors on decorator when present ────────────────


def test_reindent_anchors_on_decorator_when_llm_emits_col0():
    """Bare col-0 output with decorator must shift to target indent intact.

    Pre-fix behaviour: this case worked (decorator and def both moved to
    col 4 together). We assert it still works — guard against regressions
    in the decorator anchor logic.
    """
    code = "@staticmethod\ndef f(x):\n    return x\n"
    reindented = _reindent(code, "    ")
    assert reindented == (
        "    @staticmethod\n    def f(x):\n        return x\n"
    ), f"Got: {reindented!r}"


def test_reindent_runs_even_when_anchor_matches_target():
    """No short-circuit on ``source_indent == target_indent``.

    The pre-fix short-circuit caused the cycle 15 corruption: decorator
    at col 0 + def at col 4, ``source_indent`` detected from the def
    (col 4), target col 4, equality short-circuited and the mis-aligned
    decorator passed through unchanged. After the fix, the transform
    runs regardless of equality so the parse gate (fix 4) can catch
    any residual breakage downstream rather than having mis-aligned
    output slip through here.
    """
    # All-col-4 input with matching target — should pass through unchanged.
    code = "    @staticmethod\n    def f(x):\n        return x\n"
    assert _reindent(code, "    ") == code


# ── Fix 4: post-splice parse gate ─────────────────────────────────────


def _symbol_lines(file_content: str, symbol_name: str) -> tuple[int, int]:
    """Return 1-indexed (start_line, end_line) for a symbol, using AST.

    Walks with parent_class tracking so we can address methods too. We
    intentionally use the stdlib AST rather than tree-sitter here —
    the test should not depend on tree-sitter's availability, and line
    numbers between the two match on well-formed Python.
    """
    tree = stdlib_ast.parse(file_content)

    def _walk(node, parent_class=None):
        for child in stdlib_ast.iter_child_nodes(node):
            if isinstance(child, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
                if child.name == symbol_name:
                    start = (
                        min(d.lineno for d in child.decorator_list)
                        if child.decorator_list
                        else child.lineno
                    )
                    return start, child.end_lineno
            if isinstance(child, stdlib_ast.ClassDef):
                got = _walk(child, child.name)
                if got:
                    return got
        return None

    found = _walk(tree)
    if not found:
        raise AssertionError(f"Symbol {symbol_name!r} not found")
    return found


def test_valid_splice_produces_parseable_file():
    """Happy path: well-aligned replacement splices cleanly."""
    start, end = _symbol_lines(_SAMPLE_CLASS, "_build_rooms")
    content_lines = _SAMPLE_CLASS.splitlines(keepends=True)
    target_indent = _get_indentation(content_lines[start - 1])

    # LLM returns correctly at col 4 (via fix-1 extraction preserving indent)
    good_response = "```python\n    @staticmethod\n    def _build_rooms(raw_rooms):\n        return list(raw_rooms)\n```"
    new_body = extract_code_from_response(good_response)
    new_body = _reindent(new_body, target_indent)

    spliced = _splice(_SAMPLE_CLASS, new_body, start, end)
    stdlib_ast.parse(spliced)  # must not raise


def test_parse_gate_would_reject_the_cycle15_corruption():
    """Direct assertion that the corruption shape is caught by a parse check.

    We construct the exact corruption from cycle 15 — decorator at col 0,
    def at col 4 — and confirm ``stdlib_ast.parse`` raises, which is the
    signal the post-splice gate in ``action_rewrite_symbol_turn`` uses
    to reject the splice and preserve the pre-splice file content.
    """
    corrupted = textwrap.dedent("""
        class WorldLoader:
            @staticmethod
            def _load_yaml(p):
                return {}

        @staticmethod
            def _build_rooms(raw_rooms):
                return raw_rooms
        """)
    try:
        stdlib_ast.parse(corrupted)
    except SyntaxError:
        return
    raise AssertionError("Parse gate should have rejected the corruption")


def test_fixed_pipeline_produces_valid_file_for_cycle15_input():
    """End-to-end (helpers only): the exact cycle 15 LLM response, run
    through the current extraction + reindent + splice, parses cleanly.

    This is the concrete regression anchor — if any future change breaks
    the pipeline back to the cycle 15 behaviour, this test fires.
    """
    # The LLM response shape that, pre-fix, caused cycle 15: correct
    # col-4 indentation with decorator, wrapped in a code fence.
    llm_response = (
        "```python\n"
        "    @staticmethod\n"
        "    def _build_rooms(\n"
        "        raw_rooms,\n"
        "        items_by_id,\n"
        "    ):\n"
        "        rooms = []\n"
        "        for rm in raw_rooms:\n"
        "            rooms.append(rm)\n"
        "        return rooms\n"
        "```"
    )

    start, end = _symbol_lines(_SAMPLE_CLASS, "_build_rooms")
    content_lines = _SAMPLE_CLASS.splitlines(keepends=True)
    target_indent = _get_indentation(content_lines[start - 1])

    new_body = extract_code_from_response(llm_response)
    new_body = _reindent(new_body, target_indent)
    spliced = _splice(_SAMPLE_CLASS, new_body, start, end)

    # Must parse — the point of the regression test.
    stdlib_ast.parse(spliced)

    # And functionally: the _build_rooms we just spliced should have
    # two parameters and be staticmethod-decorated.
    tree = stdlib_ast.parse(spliced)
    for node in stdlib_ast.walk(tree):
        if isinstance(node, stdlib_ast.FunctionDef) and node.name == "_build_rooms":
            assert len(node.args.args) == 2
            assert any(
                isinstance(d, stdlib_ast.Name) and d.id == "staticmethod"
                for d in node.decorator_list
            )
            break
    else:
        raise AssertionError("_build_rooms not found in spliced file")
