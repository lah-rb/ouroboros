"""Tests for the InferenceResult.truncated observability flag.

Motivating context: a class of bugs where a session_inference hit the
backend's max_tokens budget mid-analysis channel, the FSM correctly
stripped the unfinished reasoning, and the caller received an empty
``text`` field with no way to distinguish "model chose to say nothing"
from "budget exhausted, output cut off before the content channel
began." The LLMVP backend now derives a ``truncated`` flag from
``tokens_generated >= max_tokens`` and surfaces it through the
GraphQL response; this test locks in the round-trip on the Ouroboros
side.
"""

from __future__ import annotations

from agent.effects.protocol import InferenceResult


def test_truncated_defaults_to_false():
    """Legacy callsites and error paths that construct InferenceResult
    without the new field must default to False — a non-generation
    (connection error, timeout, 0 tokens produced) is definitionally
    NOT truncated."""
    result = InferenceResult(text="hi", tokens_generated=1)
    assert result.truncated is False


def test_truncated_can_be_set_true():
    result = InferenceResult(
        text="partial output",
        tokens_generated=2048,
        truncated=True,
    )
    assert result.truncated is True


def test_truncated_coexists_with_error():
    """Error responses can carry truncated=False without conflict.
    The fields are independent: error describes transport failure,
    truncated describes successful-but-budget-capped generation."""
    result = InferenceResult(
        text="",
        tokens_generated=0,
        finished=False,
        error="connection refused",
    )
    assert result.truncated is False
    assert result.error == "connection refused"


def test_truncated_field_is_keyword_only_from_dataclass_ordering():
    """Positional construction must still work — truncated goes at
    the end of the dataclass field order after error. This checks
    the field order hasn't silently shifted."""
    # text, tokens_generated, finished, error, truncated
    result = InferenceResult("t", 5, True, None, True)
    assert result.text == "t"
    assert result.tokens_generated == 5
    assert result.finished is True
    assert result.error is None
    assert result.truncated is True
