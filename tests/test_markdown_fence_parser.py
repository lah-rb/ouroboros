"""Tests for agent/markdown_fence.py::parse_file_blocks.

The parser is responsible for converting LLM-produced code responses
into (path, content) tuples that the write_files action writes to disk.

Step C Batch A migrated the output protocol from top-level
`=== FILE: path ===` markers (outside fences) to fence-with-path-comment
markers as the first comment line INSIDE each fence. These tests
confirm the new protocol is parsed correctly across the shapes the
three migrated sites produce (#1 create, #12 project_ops, #15 rewrite).
"""

from __future__ import annotations

from agent.markdown_fence import parse_file_blocks

# ──────────────────────────────────────────────────────────────────────
# Single-file case (Sites #1, #15)
# ──────────────────────────────────────────────────────────────────────


def test_single_file_with_python_comment_marker() -> None:
    text = (
        "```python\n"
        "# === FILE: main.py ===\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
    )
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    path, content = blocks[0]
    assert path == "main.py"
    assert "def hello():" in content
    assert "# === FILE:" not in content  # marker stripped


def test_single_file_content_preserves_internal_structure() -> None:
    text = (
        "```python\n"
        "# === FILE: app/main.py ===\n"
        "from typing import Any\n"
        "\n"
        "\n"
        "def run(x: Any) -> None:\n"
        "    print(x)\n"
        "```\n"
    )
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    _, content = blocks[0]
    assert "from typing import Any" in content
    assert "\n\n\n" in content  # blank lines preserved
    assert "def run" in content


# ──────────────────────────────────────────────────────────────────────
# Multi-file case (Site #12)
# ──────────────────────────────────────────────────────────────────────


def test_multi_file_mixed_languages() -> None:
    """Multi-file output — each fence carries its own language tag and
    its own FILE marker."""
    text = (
        "```toml\n"
        "# === FILE: pyproject.toml ===\n"
        "[project]\n"
        'name = "app"\n'
        "```\n"
        "\n"
        "```python\n"
        "# === FILE: src/main.py ===\n"
        "def main():\n"
        "    pass\n"
        "```\n"
        "\n"
        "```markdown\n"
        "# === FILE: README.md ===\n"
        "# App\n"
        "A thing.\n"
        "```\n"
    )
    blocks = parse_file_blocks(text)
    paths = [p for p, _ in blocks]
    assert paths == ["pyproject.toml", "src/main.py", "README.md"]

    # Content integrity per block
    assert "[project]" in blocks[0][1]
    assert "def main():" in blocks[1][1]
    assert "# App" in blocks[2][1]


def test_multi_file_with_double_slash_comment() -> None:
    """The FILE marker tolerates `//` comment style for JS/TS/C-family
    fences."""
    text = (
        "```javascript\n"
        "// === FILE: src/index.js ===\n"
        "console.log('hi');\n"
        "```\n"
    )
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    assert blocks[0][0] == "src/index.js"
    assert "console.log" in blocks[0][1]
    assert "// === FILE:" not in blocks[0][1]


def test_multi_file_with_no_comment_prefix_marker() -> None:
    """Some fences (e.g., markdown, raw) have no comment syntax; the
    marker can appear bare. Still recognized."""
    text = "```markdown\n" "=== FILE: docs/README.md ===\n" "# Heading\n" "```\n"
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    assert blocks[0][0] == "docs/README.md"
    assert "# Heading" in blocks[0][1]


# ──────────────────────────────────────────────────────────────────────
# Fallback path for blocks without markers
# ──────────────────────────────────────────────────────────────────────


def test_no_marker_uses_fallback_path() -> None:
    """Single fenced block with no marker + fallback_path → use fallback."""
    text = "```python\n" "def hello():\n" "    return 'world'\n" "```\n"
    blocks = parse_file_blocks(text, fallback_path="main.py")
    assert len(blocks) == 1
    assert blocks[0][0] == "main.py"
    assert "def hello" in blocks[0][1]


def test_no_marker_no_fallback_skipped() -> None:
    """Fenced block with no marker and no fallback — parser logs but
    skips (not our file to write)."""
    text = "```python\n" "def hello(): pass\n" "```\n"
    blocks = parse_file_blocks(text)
    assert blocks == []


def test_bare_text_with_fallback_path_still_wraps() -> None:
    """No fences at all + fallback_path — legacy behavior preserved:
    treat whole text as the file body."""
    text = "def hello():\n    return 'world'\n"
    blocks = parse_file_blocks(text, fallback_path="main.py")
    assert len(blocks) == 1
    assert blocks[0][0] == "main.py"
    assert "def hello" in blocks[0][1]


# ──────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────


def test_empty_fence_with_marker_writes_empty_file() -> None:
    """Empty file (e.g., `__init__.py`) is valid — marker present, no
    body. Should emit (path, '')."""
    text = "```python\n" "# === FILE: src/__init__.py ===\n" "```\n"
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    assert blocks[0][0] == "src/__init__.py"
    assert blocks[0][1] == ""


def test_empty_fence_no_marker_with_fallback_writes_empty_file() -> None:
    """Completely empty fence + fallback → empty file at fallback path."""
    text = "```python\n```\n"
    blocks = parse_file_blocks(text, fallback_path="src/__init__.py")
    assert len(blocks) == 1
    assert blocks[0] == ("src/__init__.py", "")


def test_duplicate_paths_first_wins() -> None:
    """If the model emits the same path twice, keep the first block.
    Protects against echoed prompt examples contaminating real content."""
    text = (
        "```python\n"
        "# === FILE: main.py ===\n"
        "def real_impl(): pass\n"
        "```\n"
        "\n"
        "```python\n"
        "# === FILE: main.py ===\n"
        "def echoed_from_prompt(): pass\n"
        "```\n"
    )
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    assert "real_impl" in blocks[0][1]
    assert "echoed_from_prompt" not in blocks[0][1]


def test_marker_tolerates_whitespace_variations() -> None:
    """Parser is tolerant of extra spaces around `===` and colon —
    LLMs produce minor formatting variation."""
    variations = [
        "#    ===  FILE:   path.py   ===",
        "# ===FILE: path.py===",
        "# === FILE:path.py ===",
    ]
    for first_line in variations:
        text = f"```python\n{first_line}\ndef x(): pass\n```\n"
        blocks = parse_file_blocks(text)
        assert len(blocks) == 1, f"Failed for variation: {first_line!r}"
        assert (
            blocks[0][0] == "path.py"
        ), f"Failed for variation: {first_line!r}, got {blocks[0][0]!r}"


def test_marker_in_middle_of_fence_not_matched() -> None:
    """Protocol says first comment line — a FILE marker in the middle
    of a fence body doesn't trigger splitting. Block uses fallback."""
    text = (
        "```python\n"
        "def setup():\n"
        "    # === FILE: injected.py ===  # not the first line\n"
        "    pass\n"
        "```\n"
    )
    blocks = parse_file_blocks(text, fallback_path="main.py")
    assert len(blocks) == 1
    assert blocks[0][0] == "main.py"
    # Marker-looking line stays in content because it wasn't first
    assert "injected.py" in blocks[0][1]


def test_leading_blank_lines_in_fence_allowed_before_marker() -> None:
    """The first non-blank line is what counts — blank lines before the
    marker don't break recognition."""
    text = (
        "```python\n"
        "\n"
        "\n"
        "# === FILE: main.py ===\n"
        "def hello(): pass\n"
        "```\n"
    )
    blocks = parse_file_blocks(text)
    assert len(blocks) == 1
    assert blocks[0][0] == "main.py"
    assert "def hello" in blocks[0][1]


def test_content_has_trailing_newline() -> None:
    """File content should end with exactly one newline — tool
    convention + the models' expected output format."""
    text = "```python\n" "# === FILE: main.py ===\n" "x = 1\n" "```\n"
    blocks = parse_file_blocks(text)
    _, content = blocks[0]
    assert content.endswith("\n")
    assert not content.endswith("\n\n")


def test_blocks_with_and_without_markers_in_same_response() -> None:
    """Mixed: one block has a marker, another uses fallback. Only the
    first block's matching path wins if paths collide — but here they
    differ so both blocks land."""
    text = (
        "```python\n"
        "# === FILE: a.py ===\n"
        "print('a')\n"
        "```\n"
        "\n"
        "```python\n"
        "print('b')  # no marker\n"
        "```\n"
    )
    blocks = parse_file_blocks(text, fallback_path="b.py")
    paths = [p for p, _ in blocks]
    # a.py from marker, b.py from fallback
    assert "a.py" in paths
    assert "b.py" in paths
