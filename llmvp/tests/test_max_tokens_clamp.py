"""prompt + generation must fit the window, not just the prompt.

The prompt-length guard has always checked that the PROMPT fits
``stream_context_limit``. Nothing checked that prompt + max_tokens fits — and a
config written to avoid artificial truncation naturally sets
``max_tokens_default`` equal to ``n_ctx``, so a request could ask for more
window than exists.

Observed 2026-07-27 on the APEX arm: a 21,085-token prompt was granted a
65,536-token budget against a 65,536-token window. Generation ran until the KV
pool evicted the stream, and eviction DISCARDS EVERYTHING — a batch that had
already emitted 15 complete files returned nothing. The clamp turns that into
an ordinary truncation, which keeps the work.

Deliberately keyed off ``stream_context_limit`` rather than raw ``n_ctx``: a
large pool must not admit a single stream beyond the model's trained range.
"""

from __future__ import annotations

import pytest

from core.inference import (
    _GENERATION_SLACK,
    _MIN_GENERATION_TOKENS,
    resolve_max_tokens,
)


class _Model:
    def __init__(self, limit):
        self.stream_context_limit = limit


class _Gen:
    def __init__(self, default):
        self.max_tokens_default = default


@pytest.fixture
def cfg(monkeypatch):
    """Point the module's live config view at a controllable stand-in."""
    import core.inference as inf

    class _Cfg:
        model = _Model(65536)
        generation = _Gen(65536)

    monkeypatch.setattr(inf, "config", _Cfg)
    return _Cfg


# ── Behaviour without a measurement (unchanged) ──────────────────────


def test_no_prepopulated_keeps_the_old_chain(cfg):
    assert resolve_max_tokens(None) == 65536
    assert resolve_max_tokens(4096) == 4096


def test_a_caller_that_cannot_measure_is_not_penalised(cfg):
    """Three completion entry points delegate and never build the prompt
    themselves; they must keep today's behaviour rather than silently get a
    tiny budget."""
    assert resolve_max_tokens(None, prepopulated=0) == 65536


# ── The clamp ────────────────────────────────────────────────────────


def test_the_live_case_that_caused_the_eviction(cfg):
    """21,085-token prompt, 65,536 window, 65,536 requested."""
    got = resolve_max_tokens(None, prepopulated=21085)
    assert got == 65536 - 21085 - _GENERATION_SLACK
    assert 21085 + got <= 65536, "prompt + generation must fit the window"


def test_it_only_ever_lowers(cfg):
    assert resolve_max_tokens(2048, prepopulated=21085) == 2048


def test_an_explicit_request_is_clamped_too(cfg):
    """A caller asking for more than the window gets the window, not its ask."""
    assert resolve_max_tokens(60000, prepopulated=40000) == 65536 - 40000 - _GENERATION_SLACK


def test_the_result_always_leaves_slack(cfg):
    for prompt in (1, 1000, 32768, 60000):
        got = resolve_max_tokens(None, prepopulated=prompt)
        assert prompt + got + _GENERATION_SLACK <= 65536, prompt


def test_a_prompt_that_fills_the_window_is_a_clear_error(cfg):
    """Better than handing back a stream that emits two tokens and stops."""
    with pytest.raises(ValueError, match="No room to generate"):
        resolve_max_tokens(None, prepopulated=65536 - 10)


def test_the_floor_is_enforced_not_approximated(cfg):
    just_under = 65536 - _GENERATION_SLACK - _MIN_GENERATION_TOKENS
    assert resolve_max_tokens(None, prepopulated=just_under) == _MIN_GENERATION_TOKENS
    with pytest.raises(ValueError):
        resolve_max_tokens(None, prepopulated=just_under + 1)


# ── It must key off the per-stream ceiling, not the pool ─────────────


def test_it_keys_off_stream_context_limit_not_n_ctx(cfg):
    """A 393k pool must not grant a 131k-trained model a 300k generation.
    stream_context_limit already encodes that rule; the clamp must inherit it
    rather than reading n_ctx."""
    cfg.model.stream_context_limit = 8192
    got = resolve_max_tokens(None, prepopulated=1000)
    assert got == 8192 - 1000 - _GENERATION_SLACK


def test_a_smaller_config_default_still_wins_when_it_is_smaller(cfg):
    cfg.generation.max_tokens_default = 512
    assert resolve_max_tokens(None, prepopulated=1000) == 512
