"""Flow-KV head arithmetic — the number that pins KV across turns.

`flow_head_tokens` (inference/tokenizer.py) answers: how many leading tokens
of a rendered turn are determined SOLELY by the flow's static prefix, and
therefore safe to pin in the KV under a flow_key? `run_completion`
(core/inference.py:287-291) adds that count to the global static buffer length
and hands the sum to the backend as `flow_prefix_len`.

WHY THE FAILURE IS ASYMMETRIC, and why the dangerous direction is the silent
one:

  too SHORT -> the pinned head covers less than it could. A cache miss. Costs
               prefill time and nothing else.
  too LONG  -> the head includes a token belonging to THIS turn's content.
               Every later request on that flow_key then reuses a KV prefix
               that does not match its own prompt. No crash, no error — the
               model generates against subtly wrong context, for that flow,
               indefinitely.

The two-probe design exists for exactly that asymmetry: render the prefix with
two DIFFERENT tails and keep only the common token prefix, so a tokenizer that
merges across the prefix/tail boundary cannot smuggle a content token into the
head. These tests pin that property. Both `flow_head_tokens` and
`run_completion` were at 0% coverage before this file.

NOTE the head includes the renderer's role framing (`<|im_start|>user\\n...`),
not just the prefix — measured, not assumed. The first draft of this file
asserted the head equalled the bare prefix and failed 8/8, which is the
correct outcome for testing an assumption instead of the code.
"""

from __future__ import annotations

import pytest

from core.config import set_config
from inference.tokenizer import build_full_prompt, flow_head_tokens
from tests.conftest import make_config


@pytest.fixture(autouse=True)
def _chatml_config():
    """flow_head_tokens renders through the family renderer, so a config must
    be installed. chatml keeps the framing short and readable."""
    prev = None
    try:
        from core.config import get_config

        prev = getattr(get_config, "_config", None)
    except Exception:  # noqa: BLE001
        pass
    set_config(make_config(model_extra={"family": "chatml"}))
    yield
    if prev is not None:
        set_config(prev)


class _CharTok:
    """One id per character — no merging anywhere."""

    def tokenize(self, b, add_bos=False, special=False):
        return [ord(c) for c in b.decode("utf-8")]


class _MergingTok(_CharTok):
    """Merges a boundary character with whatever follows it.

    The adversary the two-probe design was built against: the token at the
    prefix/tail seam DIFFERS depending on the tail, so a naive "tokenize the
    prefix and count" would pin a token that encodes part of the caller's
    content.
    """

    def __init__(self, boundary: str) -> None:
        self.boundary = boundary

    def tokenize(self, b, add_bos=False, special=False):
        text = b.decode("utf-8")
        ids, i = [], 0
        while i < len(text):
            if text[i] == self.boundary and i + 1 < len(text):
                ids.append(10_000 + ord(text[i]) * 256 + ord(text[i + 1]))
                i += 2
            else:
                ids.append(ord(text[i]))
                i += 1
        return ids


def _text(ids):
    return "".join(chr(c) for c in ids if c < 10_000)


def test_head_covers_the_framing_and_the_whole_prefix_when_nothing_merges():
    head = flow_head_tokens("SYSTEM RULES", _CharTok())
    assert _text(head).endswith("SYSTEM RULES")
    assert "<|im_start|>" in _text(head)


def test_head_stops_at_a_token_that_merges_with_the_following_content():
    """The load-bearing case. ':' merges with whatever follows, so the token at
    the seam is tail-dependent and must NOT be pinned."""
    tok = _MergingTok(boundary=":")
    head = flow_head_tokens("RULES:", tok)

    assert _text(head).endswith("RULES"), _text(head)
    assert all(t < 10_000 for t in head), (
        "a merged boundary token reached the pinned head — every later request "
        "on this flow_key would reuse KV that does not match its prompt"
    )


def test_head_is_always_a_genuine_prefix_of_the_real_render():
    """Structural invariant: whatever the tokenizer does, the head must be a
    true prefix of the turn actually sent, or the pin is misaligned by
    construction."""
    for tok in (_CharTok(), _MergingTok(boundary=":")):
        for prefix in ("SYSTEM RULES", "RULES:", "A", "· unicode ·"):
            head = flow_head_tokens(prefix, tok)
            p1 = build_full_prompt(prefix + "\nAlpha one two", tok)
            assert head == p1[: len(head)], f"{tok.__class__.__name__} {prefix!r}"


def test_head_never_reaches_into_the_tail():
    """The head must be shorter than the shortest full render — if it ever
    equalled it, tail tokens would be inside the pinned span."""
    tok = _CharTok()
    prefix = "SYSTEM RULES"
    head = flow_head_tokens(prefix, tok)
    p1 = build_full_prompt(prefix + "\nAlpha one two", tok)
    assert 0 < len(head) < len(p1)


def test_confirm_with_can_only_shrink_the_head():
    """`run_completion` passes the ACTUAL turn's ids as `confirm_with`, so a
    head both probes agreed on but the real prompt contradicts gets clipped
    rather than trusted."""
    tok = _CharTok()
    prefix = "SYSTEM RULES"
    full = flow_head_tokens(prefix, tok)

    diverging = full[:4] + [ord("!")] * 4
    clipped = flow_head_tokens(prefix, tok, confirm_with=diverging)

    assert len(clipped) == 4
    assert len(clipped) < len(full)
    assert clipped == full[:4]


def test_confirm_with_matching_leaves_the_head_intact():
    """The complement: a confirming render that agrees must not shorten it —
    otherwise every flow would silently under-pin."""
    tok = _CharTok()
    prefix = "SYSTEM RULES"
    full = flow_head_tokens(prefix, tok)
    real = build_full_prompt(prefix + " the actual user turn", tok)
    assert flow_head_tokens(prefix, tok, confirm_with=real) == full
