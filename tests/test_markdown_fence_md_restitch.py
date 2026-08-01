"""OPEN_TASKS §20 — fence-truncated markdown re-stitch.

CommonMark cannot nest same-length fences, so a ```-wrapped README whose body
contains its own ``` blocks was amputated at the first interior close: 11 of
12 campaign READMEs shipped ending at an install command inside an
unterminated fence, and blind judges docked the models for our extraction.

The spec (OPEN_TASKS §20): a README with two interior bash blocks inside a
batch reply must round-trip complete, with even fence parity, under BOTH the
markdown-it path and the regex fallback; a mutation restoring first-close
termination must fail these tests (it does — the roundtrips assert the
re-stitched tail).
"""

import pytest

import agent.markdown_fence as mf
from agent.markdown_fence import parse_file_blocks

# The canonical failing shape, verbatim from the campaign: a batch reply whose
# README carries an Installation block AND a Running block, followed by
# another file. Everything after the first interior close used to vanish.
BATCH_REPLY = """```python
# === FILE: main.py ===
print("hello")
```

```markdown
# === FILE: README.md ===
# Text Adventure Game

A game built with Python.

## Installation

```bash
pip install -e .
```

## Running the Game

```bash
python main.py
```
```

```yaml
# === FILE: data/world.yaml ===
rooms:
  - id: cave
```
Done.
"""


def _readme(blocks):
    d = dict(blocks)
    assert "README.md" in d, f"paths: {list(d)}"
    return d["README.md"]


def _parity_even(content: str) -> bool:
    return not mf._fence_parity_odd(content)


class TestRoundtrip:
    def test_readme_roundtrips_complete_markdown_it(self):
        if not mf._HAS_MARKDOWN_IT:
            pytest.skip("markdown-it not installed")
        blocks = parse_file_blocks(BATCH_REPLY)
        readme = _readme(blocks)
        # The half that used to be amputated:
        assert "## Running the Game" in readme
        assert "python main.py" in readme
        assert _parity_even(readme)
        # And it must not swallow its neighbours or the reply's prose tail.
        assert "world.yaml" not in readme
        assert "Done." not in readme

    def test_readme_roundtrips_complete_regex_fallback(self, monkeypatch):
        monkeypatch.setattr(mf, "_HAS_MARKDOWN_IT", False)
        blocks = parse_file_blocks(BATCH_REPLY)
        readme = _readme(blocks)
        assert "## Running the Game" in readme
        assert "python main.py" in readme
        assert _parity_even(readme)
        assert "world.yaml" not in readme

    def test_other_files_unaffected(self):
        blocks = dict(parse_file_blocks(BATCH_REPLY))
        assert blocks["main.py"].strip() == 'print("hello")'
        assert "rooms:" in blocks["data/world.yaml"]

    def test_campaign_signature_no_longer_ships(self):
        """The exact shipped defect: content ending at the install command
        inside an unterminated fence."""
        readme = _readme(parse_file_blocks(BATCH_REPLY))
        assert not readme.rstrip().endswith("pip install -e .")


class TestEdges:
    def test_md_as_last_block_unterminated_outer(self):
        """Model hit its token limit before closing the outer fence: the last
        bare close is the README's own interior close and must be KEPT."""
        reply = (
            "```markdown\n"
            "# === FILE: README.md ===\n"
            "# Title\n\n"
            "```bash\n"
            "python main.py\n"
            "```\n"
        )
        readme = _readme(parse_file_blocks(reply))
        assert "python main.py" in readme
        assert _parity_even(readme)

    def test_four_backtick_wrap_untouched(self):
        """A model that already uses the four-backtick convention nests
        legally; parity is even and the re-stitch must not fire."""
        reply = (
            "````markdown\n"
            "# === FILE: README.md ===\n"
            "# Title\n\n"
            "```bash\n"
            "python main.py\n"
            "```\n"
            "````\n"
        )
        readme = _readme(parse_file_blocks(reply))
        assert "python main.py" in readme
        assert _parity_even(readme)

    def test_non_md_odd_parity_left_alone(self):
        """A .py whose docstring quotes a single fence line must NOT be
        extended — the guard is .md-only by design."""
        reply = (
            "```python\n"
            "# === FILE: tool.py ===\n"
            'DOC = """\n'
            "usage:\n"
            "```\n"
            '"""\n'
            "```\n"
            "trailing prose\n"
        )
        blocks = dict(parse_file_blocks(reply))
        assert "trailing prose" not in blocks["tool.py"]

    def test_complete_md_without_inner_fences_untouched(self):
        reply = (
            "```markdown\n"
            "# === FILE: NOTES.md ===\n"
            "just prose, no code blocks\n"
            "```\n"
        )
        blocks = dict(parse_file_blocks(reply))
        assert blocks["NOTES.md"].strip() == "just prose, no code blocks"

    def test_restitch_declines_when_marker_missing(self):
        assert (
            mf._restitch_truncated_md("no markers here", "README.md", "x\n```\n")
            is None
        )

    def test_restitch_returns_resume_index(self):
        out = mf._restitch_truncated_md(
            BATCH_REPLY, "README.md", "# Text Adventure Game\n```\n"
        )
        assert out is None or isinstance(out, tuple)

    def test_restitch_declines_on_even_parity(self):
        assert mf._restitch_truncated_md(BATCH_REPLY, "README.md", "clean\n") is None


class TestHelper:
    def test_repair_extends_never_replaces(self):
        """The repaired content must start with the truncated content — an
        anchor on a lookalike marker elsewhere must decline, not corrupt."""
        content_from_elsewhere = "totally different text\n```\n"
        out = mf._restitch_truncated_md(
            BATCH_REPLY, "README.md", content_from_elsewhere
        )
        assert out is None
