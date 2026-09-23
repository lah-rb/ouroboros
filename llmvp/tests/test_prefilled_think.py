"""A prefilled think that never closes is thinking, not an answer.

The generation prompt of an always-thinking family ends with the opener
(`<|im_start|>assistant\\n<think>\\n`), so the OUTPUT can only announce the
phase by closing it. The FSM used to infer "prefilled" from seeing `</think>`
— so a turn cut off mid-thought looked exactly like a plain-content answer and
its whole deliberation labelled C. On 2026-09-22 that handed 325k chars of a
qwen4exp turn's unfinished thinking to the file extractor, which wrote a
187-byte snippet from inside it as engine.py. The caller KNOWS whether it
prefilled (renderer.prefills_think_opener); the FSM now takes that as a hint.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.fsm_labeller import fsm_extract_phases

UNTERMINATED = (
    "We need write engine.py. Need GameEngine.\n\n"
    "```python\nclass GameEngine:\n    pass\n```\n\n"
    "Potential issue: `_sanitize_state` dedup. Good.\n"
)
CLOSED = "Plan the file.\n</think>\n\n```python\nprint('hi')\n```"


def _renderer(family: str, thinking: str):
    from formats.registry import clear_cache, get_renderer
    import core.config as ccfg

    cfg = SimpleNamespace(
        model=SimpleNamespace(thinking=thinking, thinking_available=True, family=family)
    )
    patcher = patch.object(ccfg, "get_config", lambda: cfg)
    patcher.start()
    clear_cache()
    return get_renderer(family), patcher


# ── renderer truth table ──────────────────────────────────────────────


@pytest.mark.parametrize("level", [None, "low", "medium", "high", "xhigh"])
def test_qwen38_thinking_on_prefills_at_every_level(level):
    r, p = _renderer("qwen38", "on")
    try:
        assert r.prefills_think_opener(level) is True
    finally:
        p.stop()


@pytest.mark.parametrize("level", [None, "low", "high"])
def test_qwen38_thinking_off_is_the_closed_literal_not_a_prefill(level):
    # `<think>\n\n</think>\n\n` ends with the CLOSER — the stream starts in
    # content, and the hint must not claim otherwise.
    r, p = _renderer("qwen38", "off")
    try:
        assert r.prefills_think_opener(level) is False
    finally:
        p.stop()


def test_qwen_gate_closed_low_does_not_prefill():
    r, p = _renderer("qwen", "per_request")
    try:
        assert r.prefills_think_opener("low") is False
        assert r.prefills_think_opener("high") is True
    finally:
        p.stop()


@pytest.mark.parametrize("family", ["gemma", "harmony"])
def test_families_that_never_prefill_an_inline_opener(family):
    r, p = _renderer(family, "per_request")
    try:
        for lvl in (None, "low", "medium", "high"):
            assert r.prefills_think_opener(lvl) is False
    finally:
        p.stop()


# ── FSM ───────────────────────────────────────────────────────────────


def test_unterminated_prefilled_think_is_all_thinking():
    ph = fsm_extract_phases(UNTERMINATED, family="qwen38", prefilled_think=True)
    assert ph["C"] == ""
    assert "GameEngine" in ph["T"] and "Potential issue" in ph["T"]


def test_without_the_hint_it_reads_as_content_the_old_failure():
    # Pinned so the hint's necessity stays visible: nothing in the stream
    # itself distinguishes this from an answer.
    ph = fsm_extract_phases(UNTERMINATED, family="qwen38")
    assert "class GameEngine" in ph["C"]


@pytest.mark.parametrize("hint", [None, True])
def test_closed_think_extracts_the_same_either_way(hint):
    ph = fsm_extract_phases(CLOSED, family="qwen38", prefilled_think=hint)
    assert ph["C"] == "```python\nprint('hi')\n```"
    assert ph["T"] == "Plan the file."


def test_bracket_family_prompt_dump_keeps_its_delim_start():
    raw = "[INST]do it[/INST]answer"
    assert (
        fsm_extract_phases(raw, family="tekken", prefilled_think=True)["C"]
        == fsm_extract_phases(raw, family="tekken")["C"]
    )


def test_strip_delimiter_threads_the_hint(monkeypatch):
    import core.inference as ci

    monkeypatch.setattr(ci, "_get_delimiter", lambda: "</think>")
    monkeypatch.setattr(ci, "_get_fsm_family", lambda: "qwen38")
    assert ci._strip_delimiter(UNTERMINATED, prefilled_think=True) == ""
    assert "class GameEngine" in ci._strip_delimiter(UNTERMINATED)
