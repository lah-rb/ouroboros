"""Tests for agent/markdown_fence.py::parse_file_blocks.

The parser converts LLM-produced code responses into (path, content)
tuples the write_files action writes to disk. Protocol: a fence's FIRST
comment line may carry `=== FILE: path ===`; blocks without markers use
fallback_path; bare unfenced text + fallback wraps as the file body.

Table-driven (2026-07-16 suite-quality pass, consolidation exemplar):
each row is (id, text, fallback_path, expected [(path, content-predicate)]).
Behavior that doesn't fit the table shape keeps its own test below.
"""

from __future__ import annotations

import pytest

from agent.markdown_fence import parse_file_blocks


def _contains(*subs):
    return lambda c: all(s in c for s in subs)


def _equals(v):
    return lambda c: c == v


CASES = [
    (
        "single_python_marker",
        "```python\n# === FILE: main.py ===\ndef hello():\n    return 'world'\n```\n",
        None,
        [("main.py", _contains("def hello():"))],
    ),
    (
        "internal_structure_preserved",
        "```python\n# === FILE: app/main.py ===\nfrom typing import Any\n\n\n"
        "def run(x: Any) -> None:\n    print(x)\n```\n",
        None,
        [("app/main.py", _contains("from typing import Any", "\n\n\n", "def run"))],
    ),
    (
        "multi_file_mixed_languages",
        '```toml\n# === FILE: pyproject.toml ===\n[project]\nname = "app"\n```\n\n'
        "```python\n# === FILE: src/main.py ===\ndef main():\n    pass\n```\n\n"
        "```markdown\n# === FILE: README.md ===\n# App\nA thing.\n```\n",
        None,
        [
            ("pyproject.toml", _contains("[project]")),
            ("src/main.py", _contains("def main():")),
            ("README.md", _contains("# App")),
        ],
    ),
    (
        "double_slash_comment_marker",
        "```javascript\n// === FILE: src/index.js ===\nconsole.log('hi');\n```\n",
        None,
        [("src/index.js", _contains("console.log"))],
    ),
    (
        "bare_marker_no_comment_prefix",
        "```markdown\n=== FILE: docs/README.md ===\n# Heading\n```\n",
        None,
        [("docs/README.md", _contains("# Heading"))],
    ),
    (
        "no_marker_uses_fallback",
        "```python\ndef hello():\n    return 'world'\n```\n",
        "main.py",
        [("main.py", _contains("def hello"))],
    ),
    (
        "no_marker_no_fallback_skipped",
        "```python\ndef hello(): pass\n```\n",
        None,
        [],
    ),
    (
        "bare_text_with_fallback_wraps",
        "def hello():\n    return 'world'\n",
        "main.py",
        [("main.py", _contains("def hello"))],
    ),
    (
        "empty_fence_with_marker_empty_file",
        "```python\n# === FILE: src/__init__.py ===\n```\n",
        None,
        [("src/__init__.py", _equals(""))],
    ),
    (
        "empty_fence_fallback_empty_file",
        "```python\n```\n",
        "src/__init__.py",
        [("src/__init__.py", _equals(""))],
    ),
    (
        "marker_whitespace_variant_spaced",
        "```python\n#    ===  FILE:   path.py   ===\ndef x(): pass\n```\n",
        None,
        [("path.py", _contains("def x"))],
    ),
    (
        "marker_whitespace_variant_tight",
        "```python\n# ===FILE: path.py===\ndef x(): pass\n```\n",
        None,
        [("path.py", _contains("def x"))],
    ),
    (
        "marker_whitespace_variant_nocolon_space",
        "```python\n# === FILE:path.py ===\ndef x(): pass\n```\n",
        None,
        [("path.py", _contains("def x"))],
    ),
    (
        "mid_fence_marker_not_matched",
        "```python\ndef setup():\n"
        "    # === FILE: injected.py ===  # not the first line\n    pass\n```\n",
        "main.py",
        [("main.py", _contains("injected.py"))],  # stays in content
    ),
    (
        "leading_blank_lines_before_marker",
        "```python\n\n\n# === FILE: main.py ===\ndef hello(): pass\n```\n",
        None,
        [("main.py", _contains("def hello"))],
    ),
    (
        "mixed_marker_and_fallback_blocks",
        "```python\n# === FILE: a.py ===\nprint('a')\n```\n\n"
        "```python\nprint('b')  # no marker\n```\n",
        "b.py",
        [("a.py", _contains("print('a')")), ("b.py", _contains("print('b')"))],
    ),
]


@pytest.mark.parametrize(
    "text,fallback,expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES]
)
def test_parse_file_blocks(text, fallback, expected):
    blocks = parse_file_blocks(text, fallback_path=fallback)
    assert [p for p, _ in blocks] == [p for p, _ in expected]
    for (path, content), (_, predicate) in zip(blocks, expected):
        assert predicate(content), f"content predicate failed for {path}: {content!r}"
    for _, content in blocks:
        # tool convention: markers never leak into written content
        assert "=== FILE:" not in content or "injected.py" in content


def test_duplicate_paths_first_wins():
    """Same path twice → keep the FIRST block. Protects against echoed
    prompt examples contaminating real content."""
    text = (
        "```python\n# === FILE: main.py ===\ndef real_impl(): pass\n```\n\n"
        "```python\n# === FILE: main.py ===\ndef echoed_from_prompt(): pass\n```\n"
    )
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    assert "real_impl" in blocks[0][1]
    assert "echoed_from_prompt" not in blocks[0][1]


def test_content_has_exactly_one_trailing_newline():
    blocks = parse_file_blocks("```python\n# === FILE: main.py ===\nx = 1\n```\n")
    _, content = blocks[0]
    assert content.endswith("\n") and not content.endswith("\n\n")
