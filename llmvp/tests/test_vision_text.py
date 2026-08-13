"""The vision path must not leak the family's own scaffolding.

The text path renders through formats/*.yaml and extracts through the FSM, so
channel markers never reach a caller. The vision path can reuse neither —
MTMDChatHandler builds its own prompt from the model's chat template and drives
its own decode loop — so /v1/vision was returning the raw stream.

Measured 2026-08-12: a blind judge scoring endpoint answers found
`<|start|>assistant to=user<|message|>` verbatim mid-answer, followed by the
model restarting and re-answering the whole figure.
"""

from __future__ import annotations

import pytest

from inference.vision_text import clean, stop_strings

MUSE = "muse-glimmer"
OPEN, CONTENT, EOT, EOM = "<|start|>", "<|message|>", "<|eot|>", "<|eom|>"
CONTENT_HEAD = f"{OPEN}assistant to=user{CONTENT}"
REASONING_HEAD = f"{OPEN}assistant to=self{CONTENT}"


# ── the seal ──────────────────────────────────────────────────────────


def test_the_turn_closer_is_offered_to_the_sampler():
    assert EOT in stop_strings(MUSE)


def test_the_reasoning_closer_is_NEVER_a_stop():
    """<|eom|> ends the ANALYSIS message. Stopping there truncates the answer
    before the content block opens — the format file calls this out
    explicitly, and it is the bug that made every muse completion empty."""
    assert EOM not in stop_strings(MUSE)


def test_an_unknown_family_offers_no_stops_rather_than_guessing():
    assert stop_strings("not-a-family") == []
    assert stop_strings("") == []


# ── the repair ────────────────────────────────────────────────────────


def test_the_leaked_marker_is_removed():
    got = clean(f"The card reads 00-071-0879.{EOT}", MUSE)
    assert EOT not in got
    assert got == "The card reads 00-071-0879."


def test_a_restart_after_a_short_first_pass_keeps_the_later_one():
    """A pass interrupted BY a restart is the short one."""
    raw = f"cut off{CONTENT_HEAD}the full and complete answer here{EOT}"
    assert clean(raw, MUSE) == "the full and complete answer here"


def test_a_restart_TRUNCATED_BY_THE_BUDGET_keeps_the_EARLIER_pass():
    """THE BUG THIS GUARDS. Measured 2026-08-12: the model wrote a complete
    2,360-char reading, emitted the content head to start again, and
    max_tokens cut the second pass at 193 chars. Taking the LAST pass returned
    the fragment and threw the finished answer away — silently, in production
    figtext."""
    complete = "a complete reading of the figure with axes and values"
    raw = f"{complete}{CONTENT_HEAD}restarted but cut"
    assert clean(raw, MUSE) == complete


def test_the_pass_choice_is_by_length_not_position():
    """Neither 'first' nor 'last' is right; longest is right in both
    directions, which is the whole point."""
    long_mid = "x" * 200
    raw = f"short{CONTENT_HEAD}{long_mid}{CONTENT_HEAD}also short"
    assert clean(raw, MUSE) == long_mid


def test_deliberation_before_the_content_channel_is_dropped():
    raw = f"{REASONING_HEAD}Probably X. Might be Y.{EOM}{CONTENT_HEAD}X.{EOT}"
    got = clean(raw, MUSE)
    assert got == "X."
    assert "Probably" not in got


def test_content_after_a_terminator_is_cut():
    raw = f"the answer{EOT}{OPEN}assistant to=user{CONTENT}and again"
    assert clean(raw, MUSE) == "the answer"


def test_every_declared_marker_is_stripped_even_without_a_channel_head():
    raw = f"{OPEN}alpha{CONTENT}beta{EOM}gamma"
    got = clean(raw, MUSE)
    for marker in (OPEN, CONTENT, EOM):
        assert marker not in got


# ── it must never make things worse ───────────────────────────────────


def test_a_clean_answer_passes_through_unchanged():
    answer = "Panel (a) shows XRD with 2θ from 10 to 80 degrees."
    assert clean(answer, MUSE) == answer


def test_stripping_never_turns_an_answer_into_nothing():
    """A caller that asked for a figure to be read is better served by a messy
    answer than by ''. The empty-completion defect cost most of a day once."""
    assert clean(f"{OPEN}{CONTENT}{EOT}", MUSE).strip() != ""


def test_an_unknown_family_passes_text_through():
    raw = f"answer{EOT}"
    assert clean(raw, "not-a-family") == raw


@pytest.mark.parametrize("empty", ["", None])
def test_empty_input_is_returned_as_given(empty):
    assert clean(empty, MUSE) == empty


def test_a_non_channel_family_is_untouched_by_channel_logic():
    """chatml has no recipient channels; nothing should be invented for it."""
    answer = "a plain answer"
    assert clean(answer, "chatml") == answer
