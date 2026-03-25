"""Markdown fence extraction using markdown-it-py.

Provides robust extraction of fenced code blocks from LLM responses.
Uses a CommonMark-compliant parser instead of regex, handling:
- Language tags (```python, ```yaml, ```toml, etc.)
- Nested fences (longer fence sequences)
- Tilde fences (~~~)
- Missing closing fences
- Mixed fence formats in the same response
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    from markdown_it import MarkdownIt

    _MD_PARSER = MarkdownIt()
    _HAS_MARKDOWN_IT = True
except ImportError:
    _HAS_MARKDOWN_IT = False
    _MD_PARSER = None
    logger.warning(
        "markdown-it-py not installed — falling back to regex fence extraction. "
        "Install with: pip install markdown-it-py"
    )


@dataclass
class FencedBlock:
    """A single fenced code block extracted from markdown."""

    language: str
    content: str


def extract_fenced_blocks(text: str) -> list[FencedBlock]:
    """Extract all fenced code blocks from markdown text.

    Uses markdown-it-py for CommonMark-compliant parsing when available,
    falls back to regex otherwise.

    Returns a list of FencedBlock with language and content.
    """
    if _HAS_MARKDOWN_IT:
        return _extract_with_markdown_it(text)
    return _extract_with_regex(text)


def strip_fences(text: str) -> str:
    """Strip markdown fences from text that contains a single code block.

    If the text contains exactly one fenced block, returns its content.
    If multiple blocks, returns the largest one.
    If no blocks found, returns the text as-is (stripped).
    """
    blocks = extract_fenced_blocks(text)
    if not blocks:
        return text.strip()
    if len(blocks) == 1:
        return blocks[0].content
    # Multiple blocks — return the largest
    return max(blocks, key=lambda b: len(b.content)).content


def parse_file_blocks(text: str, fallback_path: str = "") -> list[tuple[str, str]]:
    """Parse text containing === FILE: path === markers with fenced content.

    This is the main entry point for multi-file LLM output parsing.
    Splits on FILE markers first, then uses markdown-it-py to extract
    the fenced content from each section.

    Returns list of (path, content) tuples.
    """
    blocks: list[tuple[str, str]] = []

    # Split on === FILE: ... === markers
    marker_pattern = r"===\s*FILE:\s*(.+?)\s*===\s*\n"
    parts = re.split(marker_pattern, text)

    # parts[0] = before first marker (discard)
    # parts[1] = path, parts[2] = content, parts[3] = path, ...
    i = 1
    while i + 1 < len(parts):
        file_path = parts[i].strip()
        raw_content = parts[i + 1]

        # Extract code from fenced blocks
        fenced = extract_fenced_blocks(raw_content)
        if fenced:
            # Use the first (usually only) fenced block
            content = fenced[0].content
        else:
            # No fences — use raw content stripped
            content = raw_content.strip()

        if file_path and content.strip():
            blocks.append((file_path, content))
        i += 2

    # Fallback: no FILE markers — treat as single file
    if not blocks and fallback_path:
        content = strip_fences(text)
        if content.strip():
            blocks = [(fallback_path, content)]

    return blocks


# ── markdown-it-py implementation ─────────────────────────────────


def _extract_with_markdown_it(text: str) -> list[FencedBlock]:
    """Extract fenced blocks using markdown-it-py parser."""
    tokens = _MD_PARSER.parse(text)
    blocks = []
    for token in tokens:
        if token.type == "fence" and token.content:
            blocks.append(FencedBlock(
                language=token.info.strip() if token.info else "",
                content=token.content,
            ))
    return blocks


# ── Regex fallback ────────────────────────────────────────────────


def _extract_with_regex(text: str) -> list[FencedBlock]:
    """Fallback regex extraction when markdown-it-py is unavailable.

    Matches ```[language]\\n...content...``` patterns.
    """
    pattern = re.compile(
        r"^```([a-zA-Z]*)\s*\n(.*?)^```\s*$",
        re.MULTILINE | re.DOTALL,
    )
    blocks = []
    for match in pattern.finditer(text):
        lang = match.group(1) or ""
        content = match.group(2)
        if content.strip():
            blocks.append(FencedBlock(language=lang, content=content))
    return blocks
