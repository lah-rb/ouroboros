"""Optional redaction of secret-shaped strings before a turn is stored.

Off by default: the point of the store is the FULL prompt and response, and a
redaction pass that silently rewrote text would make replay and diff lie.
``OURO_HISTORY_REDACT=1`` turns it on for shared machines: the patterns below
(cloud keys, bearer tokens, private-key blocks, ``password=`` values) are
replaced by ``«redacted:<kind>»``; everything else is untouched, and the row
says it happened (``content_redacted`` in ``extra_json``).
"""

from __future__ import annotations

import os
import re

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        "private_key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.S,
        ),
    ),
    (
        "password",
        re.compile(
            r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*['\"]?[^\s'\"]{6,}"
        ),
    ),
)


def enabled(env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    return str(env.get("OURO_HISTORY_REDACT", "")).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def redact(text: str) -> tuple[str, list[str]]:
    """(redacted text, kinds found). Idempotent; empty kinds = unchanged."""
    if not text:
        return text, []
    kinds: list[str] = []
    for kind, pat in _PATTERNS:
        if pat.search(text):
            kinds.append(kind)
            if kind == "password":
                text = pat.sub(lambda m: f"{m.group(1)}=«redacted:{kind}»", text)
            else:
                text = pat.sub(f"«redacted:{kind}»", text)
    return text, kinds


__all__ = ["enabled", "redact"]
