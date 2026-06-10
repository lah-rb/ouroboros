"""Tests for the degenerate-repetition guard (inference/repetition.py).

The guard aborts a turn that collapses into token-level repetition (the Gemma-4
defect) before it fills max_tokens. These tests pin the two detection signals
(run-length, short-cycle), the thresholds, and — critically — that legitimate
repetition does NOT trip it.
"""

from inference.repetition import (
    DEFAULT_MAX_RUN,
    DEFAULT_MIN_CYCLE_REPS,
    DegenerateGenerationError,
    RepetitionGuard,
)


def _run(tokens, **kw):
    """Feed tokens; return the first trip reason (or None)."""
    g = RepetitionGuard(**kw)
    for t in tokens:
        r = g.observe(t)
        if r:
            return r
    return None


# ── run-length ───────────────────────────────────────────────────────
def test_run_length_trips_at_threshold():
    assert _run([5] * DEFAULT_MAX_RUN) is not None
    assert "run-length" in _run([5] * DEFAULT_MAX_RUN)


def test_run_length_just_under_threshold_ok():
    # 47 identical then a different token — never reaches a 48-run.
    assert _run([5] * (DEFAULT_MAX_RUN - 1) + [6]) is None


def test_run_length_resets_on_different_token():
    # Alternating a long-but-broken run never accumulates 48 in a row.
    seq = []
    for _ in range(100):
        seq += [5] * 10 + [6]
    assert _run(seq) is None


# ── short-cycle ──────────────────────────────────────────────────────
def test_two_cycle_trips():
    assert _run([1, 2] * DEFAULT_MIN_CYCLE_REPS) is not None
    assert "cycle" in _run([1, 2] * DEFAULT_MIN_CYCLE_REPS)


def test_cycle_under_min_reps_ok():
    # period-3 block repeated only 4x (< 12 reps) is not degenerate.
    assert _run([1, 2, 3] * 4) is None


def test_max_period_boundary():
    # period 8 (the default max) repeated enough times trips.
    block = list(range(1, 9))  # 8 distinct tokens
    assert _run(block * DEFAULT_MIN_CYCLE_REPS) is not None
    # period 9 is beyond the default window → not caught by default config,
    # but a config with max_cycle_period=9 catches it (configurability).
    block9 = list(range(1, 10))
    assert _run(block9 * DEFAULT_MIN_CYCLE_REPS) is None
    assert _run(block9 * DEFAULT_MIN_CYCLE_REPS, max_cycle_period=9) is not None


# ── no false positives on legitimate repetition ──────────────────────
def test_distinct_stream_ok():
    assert _run(list(range(500))) is None


def test_indentation_like_stream_ok():
    # Newline + indent + a VARYING content token per line: the period-3 block
    # changes every line, so it is not an exact repeated block.
    NL, IND = 10, 20
    seq = []
    for i in range(300):
        seq += [NL, IND, 1000 + i]
    assert _run(seq) is None


def test_short_separator_run_ok():
    # A handful of identical "=" tokens (a markdown rule) under the run cap.
    assert _run([61] * 20 + list(range(50))) is None


# ── exception type ───────────────────────────────────────────────────
def test_error_carries_reason_and_count():
    e = DegenerateGenerationError("run-length 48 of token 5", tokens_generated=48)
    assert isinstance(e, RuntimeError)  # caught by broad except → clean failure
    assert e.reason == "run-length 48 of token 5"
    assert e.tokens_generated == 48


def test_guard_disabled_via_zero_max_run_still_catches_cycle():
    # max_run=0 disables run-length; cycle detection still active.
    assert _run([7] * 100, max_run=0) is None
    assert _run([1, 2] * DEFAULT_MIN_CYCLE_REPS, max_run=0) is not None
