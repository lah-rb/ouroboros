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


def _looks_like_empty_fence(raw_content: str) -> bool:
    """Detect raw content that is just markdown fence markers with no body.

    Matches patterns like:
        ```python\\n```
        ```\\n```
        ``` python \\n ```
        ~~~yaml\\n~~~

    These occur when the LLM wraps an intentionally empty file
    (e.g., ``__init__.py``) in markdown fences.  The fence extractor
    correctly returns no content, but the caller needs to distinguish
    "empty fence" (write empty file) from "no fence at all" (use raw text).
    """
    stripped = raw_content.strip()
    # Remove all whitespace, backticks, tildes, and common language tags
    residue = stripped
    for char in "`~ \t\n\r":
        residue = residue.replace(char, "")
    # After stripping fence characters and whitespace, only a language
    # tag (e.g., "python", "yaml") should remain — or nothing at all.
    lang_tags = {
        "python",
        "yaml",
        "yml",
        "json",
        "toml",
        "javascript",
        "js",
        "typescript",
        "ts",
        "rust",
        "go",
        "java",
        "c",
        "cpp",
        "sh",
        "bash",
        "markdown",
        "md",
        "txt",
        "ini",
        "cfg",
        "xml",
        "html",
        "css",
        "sql",
        "ruby",
        "rb",
        "perl",
        "lua",
        "r",
        "swift",
        "kotlin",
        "scala",
        "php",
    }
    return residue.lower() in lang_tags or residue == ""


# Pattern matching the FILE marker as the first line inside a fenced
# block. Tolerates multiple comment prefixes so the same protocol works
# across languages: `#` (Python, TOML, YAML, Makefile, Markdown), `//`
# (JS/TS/C family), `--` (SQL, Lua), or no prefix at all.
_FILE_MARKER_RE = re.compile(r"^\s*(?:#|//|--)?\s*===\s*FILE:\s*(.+?)\s*===\s*$")


def _segment_lines(lines: list[str], start: int) -> list[tuple[str, str]]:
    """Split fence-body ``lines`` into (path, content) at every FILE marker.

    ``start`` is the index of a line ALREADY known to be a FILE marker, which
    is what licenses the split: a fence whose first substantive line is a
    marker is unambiguously in multi-file protocol, so further markers in it
    are separators rather than content.

    Callers must not use this on a body that does not begin with a marker.
    Generated files legitimately contain marker-looking text — ``renderers.py``
    emits exactly this syntax — and splitting those would corrupt them. The
    first-line gate keeps that case on the untouched single-file path.
    """
    out: list[tuple[str, str]] = []
    path = _FILE_MARKER_RE.match(lines[start]).group(1).strip()  # type: ignore[union-attr]
    buf: list[str] = []
    for line in lines[start + 1 :]:
        nxt = _FILE_MARKER_RE.match(line)
        if nxt:
            out.append((path, _join_body(buf)))
            path, buf = nxt.group(1).strip(), []
            continue
        buf.append(line)
    out.append((path, _join_body(buf)))
    return out


def _join_body(lines: list[str]) -> str:
    """Trim leading blanks, rstrip, and terminate with exactly one newline."""
    while lines and not lines[0].strip():
        lines.pop(0)
    content = "\n".join(lines).rstrip() + "\n"
    return "" if content == "\n" else content


# ── Markdown truncation re-stitch (OPEN_TASKS §20) ───────────────────
#
# CommonMark cannot nest same-length fences: a ```-wrapped markdown FILE whose
# body contains its own ``` blocks is terminated at the body's FIRST interior
# close, and everything after — in every observed case the "how to run"
# section — is silently discarded. 11 of 12 campaign READMEs shipped truncated
# this way, and blind judges docked the models for it. Both extraction paths
# (markdown-it and the regex fallback) share the semantics, so the guard runs
# post-hoc on the raw reply text rather than inside either parser.

_FENCE_LINE_RE = re.compile(r"^\s{0,3}`{3,}")
_BARE_CLOSE_RE = re.compile(r"^\s{0,3}```\s*$")


def _fence_parity_odd(content: str) -> bool:
    return sum(1 for ln in content.split("\n") if _FENCE_LINE_RE.match(ln)) % 2 == 1


def _restitch_truncated_md(
    text: str, path: str, content: str
) -> tuple[str, int] | None:
    """Repair a fence-truncated markdown file from the raw reply.

    Trigger: the extracted content of a ``.md`` target has ODD fence parity —
    the signature of an interior close having been read as the outer close.
    Repair: re-read the raw reply from this file's FILE marker up to the next
    FILE marker (stopping before that marker's own fence opener) or end of
    reply, then peel the true outer close — the LAST bare ``` in the span —
    keeping it only if parity demands it (the model never closed the outer
    fence).

    Returns ``(repaired_content, resume_line_idx)`` where ``resume_line_idx``
    is the line in ``text`` from which parsing should RESUME — the truncation
    does not only amputate the markdown file, it re-pairs every fence after it
    (a bare close swallows the next block's opener), so blocks following the
    md file are mis-parsed too and must be re-read from the boundary. Returns
    None to leave the original alone: non-.md targets, even parity, an
    unlocatable marker, a repair that does not extend the original, or one
    that cannot reach even parity all decline.
    """
    if not path.endswith(".md") or not _fence_parity_odd(content):
        return None

    lines = text.split("\n")
    marker_idx = None
    for i, ln in enumerate(lines):
        m = _FILE_MARKER_RE.match(ln)
        if m and m.group(1).strip() == path:
            marker_idx = i
            break
    if marker_idx is None:
        return None

    # Boundary: the next FILE marker's fence opener (or the marker itself when
    # no opener precedes it), else end of reply.
    end = len(lines)
    for j in range(marker_idx + 1, len(lines)):
        if _FILE_MARKER_RE.match(lines[j]):
            end = j - 1 if j > 0 and _FENCE_LINE_RE.match(lines[j - 1]) else j
            break
    span = lines[marker_idx + 1 : end]
    while span and not span[-1].strip():
        span.pop()
    if not span:
        return None

    # Peel the outer close: prefer dropping the LAST bare ``` line; keep it
    # only if parity needs it (unterminated outer fence at end of reply).
    last_close = None
    for k in range(len(span) - 1, -1, -1):
        if _BARE_CLOSE_RE.match(span[k]):
            last_close = k
            break
    for candidate_lines in (
        [] if last_close is None else span[:last_close],
        span,
    ):
        if not candidate_lines:
            continue
        repaired = _join_body(list(candidate_lines))
        if _fence_parity_odd(repaired):
            continue
        # The repair must EXTEND the truncated content, never replace it —
        # guards against anchoring on a lookalike marker elsewhere in the reply.
        if repaired.startswith(content.rstrip("\n")) and len(repaired) > len(content):
            return repaired, end
    return None


def parse_file_blocks(text: str, fallback_path: str = "") -> list[tuple[str, str]]:
    """Parse text containing fenced code blocks with `# === FILE: path ===`
    markers as the first comment line inside each fence.

    This is the main entry point for multi-file LLM output parsing.
    Scans every fenced block for a FILE marker on its first line; when
    present, strips that line from the block body and uses the captured
    path. Blocks without a marker fall back to `fallback_path` (useful
    for single-file sites where the path comes from the flow input).

    Deduplicates by path: first meaningful block wins. This prevents
    LLM-generated duplicate FILE markers (e.g., an echo of prompt
    instructions) from overwriting valid content.

    The marker pattern tolerates comment styles `#`, `//`, `--`, or
    none at all — the FILE marker works regardless of which language
    tag the fence uses.

    Returns list of (path, content) tuples.
    """
    blocks: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    # Line indices to RESUME parsing from after a §20 markdown re-stitch — the
    # truncation re-pairs every fence after the md file, so blocks following
    # it were mis-parsed on this pass and are recovered by a re-parse below.
    restitch_resumes: list[int] = []

    for fenced in extract_fenced_blocks(text):
        body = fenced.content
        if not body:
            # Empty fence — could be an intentionally empty file if the
            # caller supplied a fallback_path (single-file site).
            # Without a path, we skip.
            if fallback_path and fallback_path not in seen_paths:
                blocks.append((fallback_path, ""))
                seen_paths.add(fallback_path)
            continue

        # Peek at the first non-blank line for a FILE marker.
        lines = body.split("\n")
        first_nonblank_idx = None
        for idx, line in enumerate(lines):
            if line.strip():
                first_nonblank_idx = idx
                break

        if first_nonblank_idx is None:
            # All blank — treat as empty fence.
            if fallback_path and fallback_path not in seen_paths:
                blocks.append((fallback_path, ""))
                seen_paths.add(fallback_path)
            continue

        marker = _FILE_MARKER_RE.match(lines[first_nonblank_idx])
        if marker:
            # The marker line is stripped so the written file contains only
            # the actual source. A fence in this protocol may carry MORE than
            # one file: "one fence per file" and "one fence, files separated
            # by markers" are both reasonable readings of the instruction, and
            # models pick either. Laguna-S-2.1 emitted all 7 files in a single
            # fence, which used to parse as 0 usable files (2026-07-26).
            # Splitting here is unambiguous because the marker syntax is
            # explicit — see _segment_lines for why the first-line gate above
            # is what makes it safe.
            candidates = _segment_lines(lines, first_nonblank_idx)
            for cand_path, cand_content in candidates:
                if not cand_path or cand_path in seen_paths:
                    if cand_path in seen_paths:
                        logger.debug(
                            "Skipping duplicate FILE block for %r (first kept)",
                            cand_path,
                        )
                    continue
                # Empty content under a DECLARED path is an intentionally empty
                # file (__init__.py is the common case) — the marker is the
                # declaration, so emit it. Reject only non-empty content that
                # is placeholder echo ("# complete modified file content").
                if not cand_content or _is_meaningful_content(cand_content):
                    restitched = _restitch_truncated_md(text, cand_path, cand_content)
                    if restitched is not None:
                        repaired, resume_at = restitched
                        logger.info(
                            "Re-stitched fence-truncated markdown for %r "
                            "(%d -> %d chars) — see OPEN_TASKS §20",
                            cand_path,
                            len(cand_content),
                            len(repaired),
                        )
                        cand_content = repaired
                        restitch_resumes.append(resume_at)
                    blocks.append((cand_path, cand_content))
                    seen_paths.add(cand_path)
            if len(candidates) > 1:
                logger.info(
                    "Fence carried %d FILE markers; split into %d files",
                    len(candidates),
                    len(candidates),
                )
            continue
        else:
            # No marker — use fallback_path if provided.
            if not fallback_path:
                logger.debug(
                    "Fenced block with no FILE marker and no fallback_path; "
                    "skipping. Fence language tag: %r",
                    fenced.language,
                )
                continue
            file_path = fallback_path
            content = body.strip()
            if content and not content.endswith("\n"):
                content += "\n"

        if not file_path:
            continue
        if file_path in seen_paths:
            logger.debug(
                "Skipping duplicate FILE block for %r (first block kept)",
                file_path,
            )
            continue

        if content or _is_meaningful_content(body):
            blocks.append((file_path, content))
            seen_paths.add(file_path)

    # §20 resume: re-parse the reply from each re-stitched md file's true end.
    # The fence re-pairing after a truncated md mis-parses every subsequent
    # block on the pass above (a bare close swallows the next block's opener),
    # so files after the md would otherwise be dropped as unmarked content.
    # Dedup-by-path makes this safe: only paths not already seen are added.
    if restitch_resumes:
        all_lines = text.split("\n")
        for resume_at in restitch_resumes:
            tail = "\n".join(all_lines[resume_at:])
            if not tail.strip():
                continue
            for sub_path, sub_content in parse_file_blocks(tail):
                if sub_path not in seen_paths:
                    logger.info(
                        "Recovered %r from the post-restitch tail "
                        "(mis-paired by the §20 truncation)",
                        sub_path,
                    )
                    blocks.append((sub_path, sub_content))
                    seen_paths.add(sub_path)

    # Final fallback: no fences extracted. Two sub-cases:
    #   - An intentionally empty fence (```lang\n```) — the LLM meant an
    #     empty file. Detected via _looks_like_empty_fence.
    #   - Bare text with no fences — legacy behavior, wrap in the file.
    # Both require a fallback_path to know where to write.
    if not blocks and fallback_path:
        if _looks_like_empty_fence(text):
            blocks = [(fallback_path, "")]
        else:
            content = strip_fences(text)
            if _is_meaningful_content(content):
                if not content.endswith("\n"):
                    content += "\n"
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
