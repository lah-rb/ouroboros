"""Unit tests for the shared per-token decode state machine.

TokenPipeline is driven by BOTH generation paths (pool generate_stream_sync
and the batched engine), so these tests pin the exact semantics the two
paths rely on: UTF-8 boundary emission, buffer_mode, bounded stop scan,
guard/long-cycle/detok verdicts, detok-tail bounding, and lazy capture meta.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.decode_constants import DETOK_TAIL  # noqa: E402
from inference.token_pipeline import (  # noqa: E402
    TokenPipeline,
    Verdict,
    build_capture_meta,
)


class FakeLlama:
    """Token id -> its own UTF-8 byte via a supplied mapping."""

    def __init__(self, pieces: dict[int, bytes]):
        self.pieces = pieces
        self.detok_calls: list[tuple[list[int], list[int]]] = []

    def detokenize(self, tokens, prev_tokens=None):
        self.detok_calls.append((list(tokens), list(prev_tokens or [])))
        return b"".join(self.pieces[t] for t in tokens)


def _pipe(llama, stop_texts=(), **kw):
    return TokenPipeline(llama, list(stop_texts), **kw)


def test_streams_text_and_flushes_nothing_when_fully_emitted():
    llama = FakeLlama({1: b"he", 2: b"llo"})
    p = _pipe(llama)
    assert p.feed(1) == Verdict()
    assert p.pop_text() == "he"
    assert p.feed(2) == Verdict()
    assert p.pop_text() == "llo"
    assert p.flush() is None


def test_holds_incomplete_multibyte_char_until_complete():
    snowman = "☃".encode("utf-8")  # 3 bytes
    llama = FakeLlama({1: snowman[:1], 2: snowman[1:2], 3: snowman[2:]})
    p = _pipe(llama)
    p.feed(1)
    assert p.pop_text() is None  # incomplete
    p.feed(2)
    assert p.pop_text() is None  # still incomplete
    p.feed(3)
    assert p.pop_text() == "☃"


def test_flush_replaces_trailing_partial_char():
    snowman = "☃".encode("utf-8")
    llama = FakeLlama({1: b"ok", 2: snowman[:2]})
    p = _pipe(llama)
    p.feed(1)
    assert p.pop_text() == "ok"
    p.feed(2)
    assert p.pop_text() is None
    assert p.flush() == "�"  # errors="replace" collapses the partial char


def test_buffer_mode_emits_only_on_flush():
    llama = FakeLlama({1: b"a", 2: b"b"})
    p = _pipe(llama, buffer_mode=True)
    p.feed(1)
    p.feed(2)
    assert p.pop_text() is None
    assert p.flush() == "ab"


def test_stop_text_detected_and_kept_in_output():
    llama = FakeLlama({1: b"answer", 2: b"<|im_", 3: b"end|>"})
    p = _pipe(llama, stop_texts=["<|im_end|>"])
    assert p.feed(1).stop is False
    assert p.feed(2).stop is False
    v = p.feed(3)
    assert v.stop is True and v.end_reason is None
    # stop text is NOT stripped — downstream consumers see the full output
    assert p.pop_text() == "answer<|im_end|>"


def test_guard_verdict_short_circuits_before_detok():
    class TrippingGuard:
        def observe(self, token):
            return "run of 48"

    llama = FakeLlama({1: b"x"})
    p = _pipe(llama, guard=TrippingGuard())
    v = p.feed(1)
    assert v.degenerate == "run of 48"
    assert llama.detok_calls == []  # never reached detok


def test_detok_failure_becomes_degenerate_verdict():
    class BrokenLlama:
        def detokenize(self, tokens, prev_tokens=None):
            raise ValueError("Negative size passed")

    p = TokenPipeline(BrokenLlama(), [])
    v = p.feed(1)
    assert v.degenerate is not None and "detokenization failed" in v.degenerate


def test_prior_tail_seeded_from_prompt_and_bounded():
    llama = FakeLlama({i: b"x" for i in range(200)})
    prompt = list(range(100))
    p = _pipe(llama, initial_prior_tokens=prompt)
    p.feed(150)
    # first detok call saw the prompt TAIL (bounded), not the whole prompt
    _, prev = llama.detok_calls[0]
    assert prev == prompt[-DETOK_TAIL:]
    for t in range(151, 151 + DETOK_TAIL + 5):
        p.feed(t)
    _, prev_last = llama.detok_calls[-1]
    assert len(prev_last) == DETOK_TAIL


def test_capture_meta_thunk_resolved_lazily(tmp_path, monkeypatch):
    calls = {"n": 0}

    def meta_thunk():
        calls["n"] += 1
        return {"request_id": "r1"}

    llama = FakeLlama({1: b"x"})
    p = _pipe(llama, capture_dir=str(tmp_path), capture_meta=meta_thunk)
    p.feed(1)
    assert calls["n"] == 0  # not resolved per token

    dumped = {}

    def fake_dump(directory, reason, acc, n, meta=None):
        dumped.update(meta=meta, reason=reason)

    from inference import runaway_capture

    monkeypatch.setattr(runaway_capture, "dump_capture", fake_dump)
    p.dump_capture("test-reason")
    assert calls["n"] == 1
    assert dumped["meta"] == {"request_id": "r1"}


def test_build_capture_meta_prompt_tail():
    llama = FakeLlama({i: bytes([65 + (i % 26)]) for i in range(1000)})
    meta = build_capture_meta(llama, "req9", 0.35, list(range(900)))
    assert meta["request_id"] == "req9"
    assert meta["prompt_tokens"] == 900
    assert len(meta["prompt_tail"]) == 768  # tail-only detok


# ── refresh-drain seams (session expiry hook) ──────────────────────────


def test_session_manager_registers_expirer_and_expires_all():
    import asyncio

    from core.session_manager import SessionManager, SessionState

    class FakeBackend:
        pass

    be = FakeBackend()
    sm = SessionManager(be)
    # construction registers the straggler hook on the backend
    assert be._session_expirer == sm.expire_all_sessions

    ended = []

    async def fake_end(sid):
        ended.append(sid)
        sm._sessions.pop(sid, None)
        return True

    sm.end_session = fake_end
    sm._sessions["s1"] = SessionState(instance=object(), current_state=None)
    sm._sessions["s2"] = SessionState(instance=object(), current_state=None)
    n = asyncio.run(sm.expire_all_sessions("drain deadline test"))
    assert n == 2 and sorted(ended) == ["s1", "s2"]
    assert sm._sessions == {}
