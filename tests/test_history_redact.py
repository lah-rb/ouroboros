"""Redaction is opt-in and honest about itself."""

from __future__ import annotations

import asyncio

from agent.history import reader
from agent.history.redact import enabled, redact
from agent.history.store import HistoryStore
from agent.trace import InferenceCall


def test_off_by_default_and_patterns_cover_the_usual_shapes():
    assert enabled({}) is False and enabled({"OURO_HISTORY_REDACT": "1"}) is True
    text = (
        "key AKIAABCDEFGHIJKLMNOP and sk-abcdefghijklmnopqrstuvwxyz1234 "
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz password=hunter22 "
        "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY----- keep this"
    )
    out, kinds = redact(text)
    assert set(kinds) == {"aws_key", "openai_key", "bearer", "password", "private_key"}
    assert "AKIA" not in out and "hunter22" not in out and "MIIE" not in out
    assert "keep this" in out
    assert redact("nothing secret here") == ("nothing secret here", [])


def test_the_store_redacts_only_when_asked(tmp_path, monkeypatch):
    (tmp_path / ".agent").mkdir()
    loop = asyncio.new_event_loop()
    store = HistoryStore(str(tmp_path), "m1", "full")
    store.ingest(
        InferenceCall(
            mission_id="m1",
            prompt_content="token=abcdef123456 x",
            response_content="ok",
        )
    )
    monkeypatch.setenv("OURO_HISTORY_REDACT", "1")
    store.ingest(
        InferenceCall(
            mission_id="m1",
            prompt_content="token=abcdef123456 x",
            response_content="ok",
        )
    )
    loop.run_until_complete(store.close())
    plain, scrubbed = reader.load_turns(str(tmp_path / ".agent"))
    assert (
        plain["prompt_content"] == "token=abcdef123456 x"
        and "content_redacted" not in plain
    )
    assert "abcdef123456" not in scrubbed["prompt_content"]
    assert scrubbed["content_redacted"] == ["password"]
