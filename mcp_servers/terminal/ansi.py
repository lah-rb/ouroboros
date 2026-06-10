"""ANSI escape code stripping for terminal output.

Strips CSI sequences, color codes, cursor movement, and other terminal
control sequences to produce clean text suitable for LLM consumption.

This is a regex-based first pass. For full VT100 emulation (handling
cursor positioning, screen clearing, etc.), pyte can be added later.
"""

from __future__ import annotations

import re

# CSI (Control Sequence Introducer) sequences: ESC [ ... final_byte
# Covers colors, cursor movement, erase, scroll, etc.
_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

# OSC (Operating System Command) sequences: ESC ] ... ST
# Covers window title, hyperlinks, etc.
_OSC_RE = re.compile(r"\x1b\].*?(?:\x1b\\|\x07)")

# Single-character escape sequences: ESC followed by one char
_ESC_SINGLE_RE = re.compile(r"\x1b[^[\]()]")

# Carriage return without newline (progress bars, spinners)
# Replace \r followed by non-\n with empty (overwrite effect)
_CR_OVERWRITE_RE = re.compile(r"[^\n]*\r(?!\n)")

# Common control characters (bell, backspace, etc.)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences and control characters from text.

    Preserves newlines and printable content. Handles:
    - CSI sequences (colors, cursor movement, erase)
    - OSC sequences (window title, hyperlinks)
    - Single-char escapes
    - Carriage return overwrites (progress bars)
    - Control characters (bell, backspace)

    Args:
        text: Raw terminal output potentially containing escape sequences.

    Returns:
        Clean text with escape sequences removed.
    """
    if not text:
        return ""

    result = text
    result = _CSI_RE.sub("", result)
    result = _OSC_RE.sub("", result)
    result = _ESC_SINGLE_RE.sub("", result)
    result = _CR_OVERWRITE_RE.sub("", result)
    result = _CONTROL_RE.sub("", result)

    # Collapse runs of blank lines (3+ newlines → 2)
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result
