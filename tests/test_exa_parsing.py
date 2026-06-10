"""Regression tests for Exa MCP response parsing.

Motivating incident: the 66e challenge run showed the Exa MCP server
completed successfully (3 searches, 5 results each, wall times in the
seconds range) but ``action_exa_search`` always reported
``results_found: 0`` — the research flow unconditionally landed at
``no_results``. Root cause: Exa's MCP wraps hits in a text-formatted
envelope (``{"content": "Title: ...\\nURL: ...\\n---\\n..."}``) rather
than the structured ``{"results": [...]}`` or
``{"content": [{"type": "text", "text": "<json>"}]}`` shapes the
parser had been written for. Every call silently dropped all hits.

These tests lock in parsing for Exa's current text format while
preserving the legacy shape fallbacks.
"""

from __future__ import annotations

from agent.actions.refinement_actions import _extract_exa_hits

# ── Exa's actual current format: text-envelope with --- separators ──


def test_exa_text_envelope_with_multiple_hits():
    """The shape observed in the 66e run — dict with a single
    ``content`` string, hits separated by ``\\n\\n---\\n\\n``, each
    hit prefixed with Title:/URL:/Published:/Author:/Highlights: and
    a multi-line body."""
    payload = {
        "content": (
            "Title: First Article\n"
            "URL: https://example.com/first\n"
            "Published: 2024-01-01T00:00:00.000Z\n"
            "Author: Someone\n"
            "Highlights:\n"
            "First article body line 1\n"
            "First article body line 2\n"
            "\n"
            "---\n"
            "\n"
            "Title: Second Article\n"
            "URL: https://example.com/second\n"
            "Published: N/A\n"
            "Author: N/A\n"
            "Highlights:\n"
            "Second article body"
        )
    }
    hits = _extract_exa_hits(payload)
    assert len(hits) == 2, f"expected 2 hits, got {len(hits)}"
    assert hits[0]["title"] == "First Article"
    assert hits[0]["url"] == "https://example.com/first"
    assert "First article body line 1" in hits[0]["content"]
    assert "First article body line 2" in hits[0]["content"]
    assert hits[1]["title"] == "Second Article"
    assert hits[1]["url"] == "https://example.com/second"
    assert "Second article body" in hits[1]["content"]


def test_exa_text_envelope_single_hit_no_separator():
    """A single-result response has no --- separator at all."""
    payload = {
        "content": (
            "Title: Only Result\n"
            "URL: https://example.com/only\n"
            "Published: N/A\n"
            "Author: N/A\n"
            "Highlights:\n"
            "Just one body"
        )
    }
    hits = _extract_exa_hits(payload)
    assert len(hits) == 1
    assert hits[0]["title"] == "Only Result"
    assert hits[0]["content"] == "Just one body"


def test_exa_text_envelope_preserves_content_whitespace():
    """Content may contain blank lines, code blocks, markdown — must
    survive intact (after initial strip) so the summarizer gets rich
    context rather than fragments."""
    payload = {
        "content": (
            "Title: Rich Content\n"
            "URL: https://example.com/\n"
            "Highlights:\n"
            "Paragraph 1.\n"
            "\n"
            "Paragraph 2 with a > quote.\n"
            "\n"
            "```python\n"
            "def hello():\n"
            "    pass\n"
            "```\n"
            "\n"
            "---\n"
            "\n"
            "Title: Other\n"
            "URL: u\n"
            "Highlights:\n"
            "short"
        )
    }
    hits = _extract_exa_hits(payload)
    assert len(hits) == 2
    assert "Paragraph 1." in hits[0]["content"]
    assert "Paragraph 2" in hits[0]["content"]
    assert "def hello():" in hits[0]["content"]
    assert "```python" in hits[0]["content"]


# ── Legacy / defensive shapes ───────────────────────────────────────


def test_exa_structured_results_list():
    """Backcompat: structured ``{"results": [...]}`` still works."""
    payload = {
        "results": [
            {"url": "u1", "title": "t1", "text": "content1"},
            {"url": "u2", "title": "t2", "content": "content2"},
        ]
    }
    hits = _extract_exa_hits(payload)
    assert len(hits) == 2
    assert hits[0]["content"] == "content1"
    assert hits[1]["content"] == "content2"


def test_exa_mcp_content_blocks_with_json():
    """Backcompat: MCP envelope with content blocks containing
    JSON-serialized payload."""
    import json as _json

    payload = {
        "content": [
            {
                "type": "text",
                "text": _json.dumps(
                    {"results": [{"url": "u", "title": "t", "text": "body"}]}
                ),
            }
        ]
    }
    hits = _extract_exa_hits(payload)
    assert len(hits) == 1
    assert hits[0]["title"] == "t"
    assert hits[0]["content"] == "body"


def test_exa_bare_list():
    """Defensive: if the server returns a list directly, we handle it."""
    payload = [
        {"url": "u1", "title": "t1", "content": "c1"},
        {"url": "u2", "title": "t2", "content": "c2"},
    ]
    hits = _extract_exa_hits(payload)
    assert len(hits) == 2


# ── Edge cases ──────────────────────────────────────────────────────


def test_exa_empty_response_shapes():
    """Empty/null inputs return [] without raising."""
    assert _extract_exa_hits(None) == []
    assert _extract_exa_hits({}) == []
    assert _extract_exa_hits({"content": ""}) == []
    assert _extract_exa_hits({"results": []}) == []
    assert _extract_exa_hits([]) == []
    assert _extract_exa_hits("just a string") == []


def test_exa_text_envelope_with_empty_block():
    """Trailing/leading --- separators or blank blocks are ignored,
    not counted as empty hits."""
    payload = {
        "content": (
            "---\n\n" "Title: Real Hit\n" "URL: u\n" "Highlights:\n" "body\n" "\n---\n"
        )
    }
    hits = _extract_exa_hits(payload)
    assert len(hits) == 1
    assert hits[0]["title"] == "Real Hit"
