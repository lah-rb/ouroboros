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


def _is_meaningful_content(content: str) -> bool:
    """Check if content is meaningful code/data, not just fence markers or placeholders.

    Rejects content that is only backticks, whitespace, or generic placeholder
    text that LLMs sometimes echo from prompt instructions.
    """
    stripped = content.strip()
    if not stripped:
        return False
    # Reject content that is only fence markers (e.g., "```\n```")
    if not stripped.replace("`", "").replace("~", "").strip():
        return False
    # Reject common placeholder echoes from prompt templates
    placeholder_patterns = [
        "# complete modified file content",
        "# new implementation",
        "# your code here",
    ]
    lower = stripped.lower()
    for pattern in placeholder_patterns:
        if lower == pattern:
            return False
    return True


def parse_file_blocks(text: str, fallback_path: str = "") -> list[tuple[str, str]]:
    """Parse text containing === FILE: path === markers with fenced content.

    This is the main entry point for multi-file LLM output parsing.
    Splits on FILE markers first, then uses markdown-it-py to extract
    the fenced content from each section.

    Deduplicates by path: first meaningful block wins. This prevents
    LLM-generated duplicate FILE markers (e.g., an echo of prompt
    instructions) from overwriting valid content.

    Returns list of (path, content) tuples.
    """
    blocks: list[tuple[str, str]] = []
    seen_paths: set[str] = set()

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

        if file_path and _is_meaningful_content(content):
            # First-write-wins: skip duplicate paths
            if file_path not in seen_paths:
                blocks.append((file_path, content))
                seen_paths.add(file_path)
            else:
                logger.debug(
                    "Skipping duplicate FILE block for %r (first block kept)",
                    file_path,
                )
        i += 2

    # Fallback: no FILE markers — treat as single file
    if not blocks and fallback_path:
        content = strip_fences(text)
        if _is_meaningful_content(content):
            blocks = [(fallback_path, content)]

    return blocks


# ── Text content extraction ──────────────────────────────────────


def extract_first_text_content(text: str, max_length: int = 200) -> str:
    """Extract the first substantive text content from markdown.

    Uses markdown-it-py to parse the token stream and find the first
    inline content that isn't a bare heading label or empty paragraph.
    This handles whatever format the model chose — bold headers, list
    items, plain paragraphs — without opinionated heuristics.

    Args:
        text: Raw markdown text (e.g. from an LLM response).
        max_length: Truncate result to this many characters.

    Returns:
        The first substantive text found, or the first line of the
        input if parsing yields nothing.
    """
    if not text or not text.strip():
        return ""

    if _HAS_MARKDOWN_IT:
        result = _extract_text_with_markdown_it(text)
        if result:
            return result[:max_length]

    # Fallback: skip decorative lines, take first content line
    return _extract_text_with_fallback(text, max_length)


def _extract_text_with_markdown_it(text: str) -> str:
    """Walk markdown-it tokens for first substantive inline content.

    Skips heading tokens and empty paragraphs. Extracts text from
    paragraph, list item, and blockquote inline children.
    """
    tokens = _MD_PARSER.parse(text)

    for token in tokens:
        # inline tokens carry the actual text content of block elements.
        # Their parent (previous token) tells us the block type.
        if token.type != "inline" or not token.content:
            continue

        content = token.content.strip()
        if not content:
            continue

        # Strip bold/italic markers for a clean summary string
        clean = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", content)
        clean = re.sub(r"_{1,2}([^_]+)_{1,2}", r"\1", clean)
        clean = clean.strip()

        # Skip lines that are ONLY a heading-style label with no info
        # e.g. "Error location" alone isn't useful, but
        # "Error location: parser.py line 42" is.
        # Heuristic: if it's short and has no punctuation / path chars,
        # it's likely a bare label.
        if len(clean) < 40 and not any(c in clean for c in ":./-_()"):
            continue

        return clean

    return ""


def _extract_text_with_fallback(text: str, max_length: int) -> str:
    """Regex fallback: skip markdown headers, take first content line."""
    for line in text.strip().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip markdown headings
        if stripped.startswith("#"):
            continue
        # Skip bold-only headers like "**Error location**"
        bare = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", stripped).strip()
        if len(bare) < 40 and not any(c in bare for c in ":./-_()"):
            continue
        # Skip list markers to get content
        bare = re.sub(r"^[-*+]\s+", "", bare)
        return bare[:max_length]

    # Nothing passed filters — return first non-empty line
    for line in text.strip().splitlines():
        if line.strip():
            return line.strip()[:max_length]
    return ""


# ── markdown-it-py implementation ─────────────────────────────────


def _extract_with_markdown_it(text: str) -> list[FencedBlock]:
    """Extract fenced blocks using markdown-it-py parser."""
    tokens = _MD_PARSER.parse(text)
    blocks = []
    for token in tokens:
        if token.type == "fence" and token.content:
            blocks.append(
                FencedBlock(
                    language=token.info.strip() if token.info else "",
                    content=token.content,
                )
            )
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
