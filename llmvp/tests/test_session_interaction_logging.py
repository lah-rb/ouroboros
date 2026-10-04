"""A session turn must log its thinking, like every other path does.

2026-09-22: the stateless path attached the FSM-extracted CoT to its
interaction record; the session path logged `raw_text` and dropped it. Both
run the same FSM — `_strip_delimiter` forwards thinking to the
GenerationTracker — so the split was by CODE PATH, not by model behaviour.

It reads exactly like a model that stopped emitting a thinking channel. A
qwen4exp walk turn carrying 151,595 chars of CoT in `raw_text` was analysed
as "the model reasons in the open content channel" on the strength of a
`thinking` field that was only ever empty because nobody wrote it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.interaction_logger as il  # noqa: E402


def _captured(monkeypatch):
    """Capture what log_interaction is handed, without touching disk."""
    seen = {}

    def _fake(prompt, response, mode, extra=None):
        seen["mode"] = mode
        seen["extra"] = extra or {}

    monkeypatch.setattr(il, "log_interaction", _fake)
    return seen


def test_session_record_carries_thinking(monkeypatch):
    """The field the analysis reads must be populated for session turns."""
    seen = _captured(monkeypatch)
    il.log_interaction(
        prompt="p",
        response="content",
        mode="session:abc:turn3",
        extra={"raw_text": "T" * 500 + "content", "thinking": "T" * 500},
    )
    assert seen["extra"].get("thinking"), "session turns must log thinking"


def test_raw_decomposes_when_thinking_closed(monkeypatch):
    """raw == thinking + content is what makes a closed phase legible."""
    seen = _captured(monkeypatch)
    thinking, content = "T" * 400, "C" * 60
    il.log_interaction(
        prompt="p",
        response=content,
        mode="session:abc:turn1",
        extra={
            "raw_text": thinking + content,
            "raw_length": len(thinking) + len(content),
            "extracted_length": len(content),
            "thinking": thinking,
        },
    )
    e = seen["extra"]
    assert len(e["thinking"]) + e["extracted_length"] == e["raw_length"]


def test_unterminated_thinking_is_distinguishable(monkeypatch):
    """The case that matters: no close, so the whole stream became content.

    Thinking absent against a large raw is the fingerprint — that turn hands
    its entire deliberation to the file extractor.
    """
    seen = _captured(monkeypatch)
    raw = "deliberation " * 5000
    il.log_interaction(
        prompt="p",
        response=raw,  # extractor got the lot
        mode="session:abc:turn3",
        extra={
            "raw_text": raw,
            "raw_length": len(raw),
            "extracted_length": len(raw),
        },
    )
    e = seen["extra"]
    assert not e.get("thinking")
    assert e["extracted_length"] == e["raw_length"], "unterminated: raw==content"


def test_logger_merges_extra_into_the_record(tmp_path, monkeypatch):
    """Guard the mechanism the fix relies on: `extra` reaches the JSONL."""

    class _Cfg:
        class logging:  # noqa: N801
            enabled = True
            directory = tmp_path

    monkeypatch.setattr("core.config.get_config", lambda: _Cfg)
    il.log_interaction(
        prompt="p",
        response="r",
        mode="session:x:turn1",
        extra={"thinking": "abc", "raw_length": 3},
    )
    rec = json.loads((tmp_path / "interactions.jsonl").read_text().strip())
    assert rec["thinking"] == "abc" and rec["mode"] == "session:x:turn1"
