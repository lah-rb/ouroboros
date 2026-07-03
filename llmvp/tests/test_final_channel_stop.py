"""FinalChannelStop — the stateful Harmony session terminator.

Pins the live-stream analog of the labeller's single_turn seal: generation
stops when a NON-EMPTY final-channel message closes, so a deep session ends at
the answer instead of rambling into self-play (the astropy-2 runaway). Fed the
cumulative byte accumulator incrementally, exactly as the backend feeds it.
"""

from __future__ import annotations

from inference.final_channel_stop import FinalChannelStop


def _feed_bytewise(text: str) -> tuple[bool, int]:
    """Feed `text` one byte at a time (worst case for split markers). Returns
    (triggered, first_trigger_len) — the acc length at which it first fired."""
    raw = text.encode("utf-8")
    stop = FinalChannelStop()
    for i in range(1, len(raw) + 1):
        if stop.update(raw[:i]):
            return True, i
    return False, -1


def _feed_whole(text: str) -> bool:
    return FinalChannelStop().update(text.encode("utf-8"))


def test_stops_on_first_nonempty_final_close():
    raw = (
        "<|channel|>analysis<|message|>reasoning<|end|>"
        "<|start|>assistant<|channel|>final<|message|>the answer<|end|>"
    )
    triggered, at = _feed_bytewise(raw)
    assert triggered
    # Fires exactly at the final message's closing <|end|>, not before.
    assert at == raw.encode().index(b"the answer<|end|>") + len(b"the answer<|end|>")


def test_does_not_stop_on_analysis_close():
    # Analysis closes with <|end|>, then final opens — must NOT stop early.
    raw = "<|channel|>analysis<|message|>thinking<|end|>"
    assert not _feed_whole(raw)


def test_does_not_stop_on_empty_final_then_stops_on_real():
    # e75: an empty/truncated final must not seal; the real one after it does.
    raw = (
        "<|channel|>final<|message|><|end|>"
        "<|start|>assistant<|channel|>final<|message|>real<|end|>"
    )
    triggered, at = _feed_bytewise(raw)
    assert triggered
    assert at == len(raw.encode())  # only the SECOND (non-empty) final closes it


def test_stops_before_self_play_ramble():
    # The astropy-2 shape: perfect final, then a hallucinated next turn. The
    # detector fires at the first final close — the ramble is never generated.
    raw = (
        "<|channel|>analysis<|message|>inspect _line_type<|end|>"
        "<|start|>assistant<|channel|>final<|message|>"
        '{"choice": "trace", "symbol_ref": "qdp.py:_line_type"}<|end|>'
        "<|start|>assistant<|channel|>analysis<|message|><|end|>"
        "<|start|>assistant<|channel|>final<|message|>hallucinated observation<|end|>"
    )
    triggered, at = _feed_bytewise(raw)
    assert triggered
    first_close = raw.encode().index(b'_line_type"}<|end|>') + len(b'_line_type"}<|end|>')
    assert at == first_close  # stopped at the answer; ramble not reached


def test_latches_after_trigger():
    stop = FinalChannelStop()
    raw = "<|channel|>final<|message|>a<|end|>".encode("utf-8")
    assert stop.update(raw)
    assert stop.update(raw + b"more") is True  # stays triggered
    assert stop.triggered


def test_no_markers_never_stops():
    assert not _feed_whole("just some plain text with no channels at all")


def test_commentary_channel_does_not_stop():
    # Only `final` terminates — a commentary/tool channel close must not.
    raw = "<|channel|>commentary<|message|>side note<|end|>"
    assert not _feed_whole(raw)
