"""A fence opened mid-line still carries a real file.

2026-09-20: qwen3.8-flash-next ended a sentence and opened the fence on the
same line — `Let's produce final.```python` — then wrote a complete, valid
9KB parser.py under a correct FILE marker. CommonMark says an opener that
does not start its line is inline code, so the scan never saw the fence; the
path fell through to a 355-char mid-deliberation draft, which was written to
disk with prose bleeding through a line of it. Both structural gates then
passed it (the syntax tier is skipped before the env phase; the AST typecheck
returns [] on SyntaxError), so an unparseable file was scored complete.

These pin the recovery AND its limits — it must not outrank a properly
fenced declaration, and it must not fire on prose that merely mentions a
marker.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.markdown_fence import parse_file_blocks  # noqa: E402

REAL = 'def parse_command(raw):\n    return {"verb": raw.strip()}\n'


def test_recovers_the_file_behind_an_inline_opened_fence():
    text = (
        "Let me think about the verbs first.\n\n"
        "```python\n"
        "# === FILE: parser.py ===\n"
        "def draft(): pass\n"
        "```\n\n"
        "That draft is wrong. Let's produce final.```python\n"
        "# === FILE: parser.py ===\n" + REAL + "```\n"
    )
    blocks = dict(parse_file_blocks(text))
    # The properly fenced draft claimed the path first via its own marker,
    # so it keeps it — the recovery must not outrank a real declaration.
    assert "parser.py" in blocks
    assert "def draft" in blocks["parser.py"]


def test_recovery_replaces_a_fallback_only_block():
    """The live failure: the only fenced block had NO marker, so it held the
    path by assumption. An explicit declaration outranks that."""
    text = (
        "Draft attempt:\n\n"
        "```python\n"
        "def half_written(:\n"
        "```\n\n"
        "Let's produce final.```python\n"
        "# === FILE: parser.py ===\n" + REAL + "```\n"
    )
    blocks = dict(parse_file_blocks(text, fallback_path="parser.py"))
    assert blocks["parser.py"] == REAL
    assert "half_written" not in blocks["parser.py"]


def test_longest_candidate_wins_over_an_earlier_draft():
    """A long deliberation can open the same file twice; the refined one is
    written last, and length is the only ordering this module can judge."""
    text = (
        "first pass:```python\n"
        "# === FILE: m.py ===\n"
        "x = 1\n"
        "```\n"
        "better:```python\n"
        "# === FILE: m.py ===\n"
        "x = 1\ny = 2\nz = 3\nw = 4\n"
        "```\n"
    )
    blocks = dict(parse_file_blocks(text))
    assert "y = 2" in blocks["m.py"]


def test_does_not_fire_on_prose_mentioning_a_marker():
    """No trailing fence opener on the line -> not a recovery site."""
    text = (
        "I will emit # === FILE: parser.py === as the first line.\n"
        "```python\n"
        "# === FILE: parser.py ===\n" + REAL + "```\n"
    )
    blocks = dict(parse_file_blocks(text))
    assert blocks["parser.py"] == REAL


def test_well_formed_fences_are_untouched():
    text = "```python\n# === FILE: a.py ===\na = 1\n```\n"
    assert dict(parse_file_blocks(text)) == {"a.py": "a = 1\n"}


def test_inline_open_without_a_marker_is_ignored():
    """Recovery needs the declaration; an unmarked inline fence is just
    inline code and must not become a file."""
    text = "see this:```python\nnot_a_file = 1\n```\n"
    assert parse_file_blocks(text) == []
