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


class TestSingleFenceMultipleFileMarkers:
    """A fence carrying MORE than one `# === FILE: path ===` marker.

    "One fence per file" and "one fence, files separated by markers" are both
    reasonable readings of the multi-file instruction, and models pick either.
    The marker syntax is explicit enough to disambiguate, so both parse.
    """

    def test_single_fence_with_three_markers_yields_three_files(self):
        text = (
            "```python\n"
            "# === FILE: models.py ===\n"
            "class Player:\n    pass\n"
            "\n"
            "# === FILE: combat.py ===\n"
            "def fight():\n    pass\n"
            "```"
        )
        assert parse_file_blocks(text) == [
            ("models.py", "class Player:\n    pass\n"),
            ("combat.py", "def fight():\n    pass\n"),
        ]

    def test_trailing_marker_with_no_body_is_an_empty_file(self):
        # `__init__.py` is the common case: the marker IS the declaration.
        text = (
            "```python\n"
            "# === FILE: a.py ===\n"
            "A = 1\n"
            "# === FILE: pkg/__init__.py ===\n"
            "```"
        )
        assert parse_file_blocks(text) == [("a.py", "A = 1\n"), ("pkg/__init__.py", "")]

    def test_one_fence_per_file_still_works(self):
        # The pre-existing protocol must be untouched.
        text = (
            "```python\n# === FILE: a.py ===\nA = 1\n```\n"
            "```python\n# === FILE: b.py ===\nB = 2\n```"
        )
        assert parse_file_blocks(text) == [("a.py", "A = 1\n"), ("b.py", "B = 2\n")]

    def test_marker_text_inside_a_body_is_NOT_split(self):
        # THE SAFETY CASE. agent/renderers.py emits this exact marker syntax,
        # so a generated file can legitimately contain marker-looking text.
        # Splitting is licensed only when the fence's FIRST substantive line is
        # a marker; here it is not, so the body must survive intact.
        text = (
            "```python\n"
            'TEMPLATE = """\n'
            "# === FILE: not_a_real_file.py ===\n"
            '"""\n'
            "```"
        )
        blocks = parse_file_blocks(text, fallback_path="renderers.py")
        assert [p for p, _ in blocks] == ["renderers.py"]
        assert "not_a_real_file.py" in blocks[0][1]

    def test_duplicate_paths_within_one_fence_keep_the_first(self):
        text = (
            "```python\n"
            "# === FILE: a.py ===\n"
            "FIRST = 1\n"
            "# === FILE: a.py ===\n"
            "SECOND = 2\n"
            "```"
        )
        assert parse_file_blocks(text) == [("a.py", "FIRST = 1\n")]


# ── unfenced FILE markers (2026-08-03, the DeepSeek-V4 batch) ─────────
#
# A model can honour the marker protocol and omit the fences. DeepSeek-V4's
# first batch emitted all six declared files under correct markers, every
# one compiling, with zero backticks — and the slicer wrote nothing, so
# 10,220 tokens of correct code were rebuilt serially. The wrapper stays
# the instruction (most models cannot delimit a file without it); this is
# recovery, not a relaxation.


class TestUnfencedFileMarkers:
    def test_recovers_multiple_files_without_fences(self):
        text = (
            "# === FILE: models.py ===\n"
            "class Item:\n    pass\n\n"
            "# === FILE: main.py ===\n"
            "def main():\n    print('hi')\n"
        )
        blocks = parse_file_blocks(text)
        assert [p for p, _ in blocks] == ["models.py", "main.py"]
        assert "class Item" in blocks[0][1]
        assert "def main" in blocks[1][1]
        # bodies must not carry the marker line
        assert "=== FILE:" not in blocks[0][1]

    def test_fenced_input_is_unchanged(self):
        """The recovery must never alter the normal path."""
        text = "```python\n# === FILE: a.py ===\nx = 1\n```"
        assert parse_file_blocks(text) == [("a.py", "x = 1\n")]

    def test_prose_without_markers_recovers_nothing(self):
        """A multi-file site (no fallback_path) must still get nothing from
        deliberation — otherwise the recovery would write prose to disk."""
        assert parse_file_blocks("Let me think about the design first...") == []

    def test_marker_text_inside_a_body_does_not_split(self):
        """renderers.py legitimately emits this syntax. _FILE_MARKER_RE is
        anchored, so only a line that is NOTHING BUT a marker separates."""
        text = (
            "# === FILE: renderers.py ===\n"
            'MARKER = "# === FILE: {path} ==="\n'
            "def render():\n    return MARKER\n"
        )
        blocks = parse_file_blocks(text)
        assert len(blocks) == 1 and blocks[0][0] == "renderers.py"
        assert "MARKER =" in blocks[0][1]

    def test_recovery_requires_a_leading_marker(self):
        """Text that merely CONTAINS a marker further down is not in the
        protocol — refusing it keeps prose-then-code out of the writer."""
        text = "Here is my plan.\n\n# === FILE: a.py ===\nx = 1\n"
        assert parse_file_blocks(text) == []

    def test_the_deepseek_shape_end_to_end(self):
        """The real failure, reduced: six markers, no fences, real bodies."""
        names = [
            "models.py",
            "world.py",
            "parser.py",
            "save.py",
            "engine.py",
            "main.py",
        ]
        text = "".join(
            f"# === FILE: {n} ===\n" + f"# module {n}\nVALUE = {i}\n\n"
            for i, n in enumerate(names)
        )
        blocks = parse_file_blocks(text)
        assert [p for p, _ in blocks] == names
        for path, body in blocks:
            compile(body, path, "exec")  # every recovered file must be valid
