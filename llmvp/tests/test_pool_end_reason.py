"""The pool decode loop reports WHY and WHERE it stopped.

A turn cut at the context ceiling stops far below the caller's max_tokens, so
the old `generated >= max_tokens` derivation read it as a clean finish — the
2026-09-22 qwen4exp engine.py turn stopped at 78,120 of a 262,144 request,
inside unfinished thinking, and was logged `truncated: False`. The loop now
stashes `_last_end_reason` ("length" on a budget stop), the budget it
enforced, and which bound hit; the session layer derives `truncated` from it.
"""

from __future__ import annotations

import pytest

from conftest import FakeGenLlama, make_pool_backend


@pytest.fixture
def eog(monkeypatch):
    """Token 0 is end-of-generation; everything else is ordinary."""
    import llama_cpp

    monkeypatch.setattr(llama_cpp, "llama_token_is_eog", lambda vocab, t: t == 0)


def _run(backend, inst, *, max_tokens, prompt=(5, 6), stop_texts=None):
    return list(
        backend.generate_stream_sync(
            inst,
            list(prompt),
            max_tokens,
            0.7,
            stop_texts=stop_texts,
            static_in_prompt=False,
        )
    )


def test_eog_stop_is_completed(eog):
    backend = make_pool_backend()
    inst = FakeGenLlama([7, 8, 0, 9])
    _run(backend, inst, max_tokens=100)
    assert inst._last_end_reason == "completed"
    assert inst._last_completion_tokens == [7, 8]
    assert inst._last_length_cause == ""
    assert inst._last_effective_max == 100


def test_budget_stop_is_length_from_max_tokens(eog):
    backend = make_pool_backend()
    inst = FakeGenLlama([7] * 50)
    _run(backend, inst, max_tokens=5)
    assert inst._last_end_reason == "length"
    assert inst._last_length_cause == "max_tokens"
    assert len(inst._last_completion_tokens) == 5


def test_context_ceiling_is_length_with_context_cause(eog):
    # n_ctx 32, 20 already in KV + 2 prompt tokens -> 10 left of a 1000 ask.
    backend = make_pool_backend()
    inst = FakeGenLlama([7] * 50, n_tokens=20, n_ctx=32)
    _run(backend, inst, max_tokens=1000)
    assert inst._last_end_reason == "length"
    assert inst._last_length_cause == "context"
    assert inst._last_effective_max == 10
    assert len(inst._last_completion_tokens) == 10


def test_context_guard_leaves_no_stale_stash(eog):
    backend = make_pool_backend()
    inst = FakeGenLlama([7, 0])
    _run(backend, inst, max_tokens=100)
    assert inst._last_completion_tokens == [7]
    # Now a request that dies at the context-window guard: the previous
    # request's outcome must not survive to be read as this one's.
    inst.n_tokens = inst._n_ctx
    with pytest.raises(ValueError, match="exceeds context"):
        _run(backend, inst, max_tokens=100)
    assert inst._last_completion_tokens is None
    assert inst._last_gen_start_pos is None
    assert inst._last_end_reason == ""


def test_fork_checkpoint_fifo_disabled_at_construction():
    backend = make_pool_backend()
    seen = {}

    class RecordingLlama:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    backend._get_llama_class = lambda: RecordingLlama
    backend._create_primary_instance()
    # 0 short-circuits every binding save site (N-1 split, generate's finally,
    # periodic eval checkpoints) — nothing here would ever read them.
    assert seen["ctx_checkpoints"] == 0
