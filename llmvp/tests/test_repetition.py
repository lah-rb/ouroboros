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


# ── empty table cells (2026-10-02) ──────────────────────────────────
# muse tokenizes a row of empty cells as '<tr' then ['><', 'td', '></', 'td']
# repeated: an exact period-4 cycle. Ids here stand in for those pieces.
_LT_GT, _TD, _CLOSE = 501, 502, 503
_CELL = [_LT_GT, _TD, _CLOSE, _TD]
_MARKUP = frozenset({500, _LT_GT, _TD, _CLOSE, 504})


def test_empty_table_cells_pass_with_the_markup_set():
    row = [500] + _CELL * 30 + [504]  # 30 empty cells, then '></tr>'
    assert (
        _run(row) == f"cycle period 4 x {DEFAULT_MIN_CYCLE_REPS}"
    ), "strict guard trips"
    assert _run(row, markup_tokens=_MARKUP) is None


def test_a_markup_loop_still_dies():
    from inference.repetition import DEFAULT_MARKUP_CYCLE_REPS

    r = _run(_CELL * 200, markup_tokens=_MARKUP)
    assert r == f"cycle period 4 x {DEFAULT_MARKUP_CYCLE_REPS} (table markup)"


def test_a_cell_with_content_keeps_the_strict_threshold():
    zero = 600  # '0.00' is content, never markup
    cell = [_LT_GT, _TD, 504, zero, _CLOSE, _TD]
    assert _run(cell * 20, markup_tokens=_MARKUP) is not None
    assert "(table markup)" not in _run(cell * 20, markup_tokens=_MARKUP)


def test_markup_pieces():
    from inference.repetition import is_markup_piece as m

    for piece in (
        "<tr",
        "><",
        "td",
        "></",
        "</td>",
        " |",
        "|---",
        "---|---|",
        ":-:",
        "th>",
    ):
        assert m(piece), piece
    # whitespace alone is a real degeneration; content is content
    for piece in ("", " ", "\n", "  \n", "0", "the", "t", "rd", "<b>"):
        assert not m(piece), piece


def test_markup_token_ids_skips_unrenderable_ids():
    from inference.repetition import markup_token_ids

    pieces = {0: "<td", 1: "hello", 2: "></", 3: " "}

    def piece(t):
        if t == 4:
            raise ValueError("control token")
        return pieces[t]

    assert markup_token_ids(5, piece) == frozenset({0, 2})


def test_a_markdown_row_of_empty_cells_passes_with_its_whitespace():
    """2026-10-03: muse wrote `|  |  |  |` while reasoning about a wide table
    -- ' |' and '  ' alternating, a period-2 cycle with a whitespace token in
    it -- and two translate turns died at "cycle period 2 x 12"."""
    from inference.repetition import table_token_classes

    PIPE, SP, NL = 0, 1, 2
    markup, space = table_token_classes(3, {PIPE: " |", SP: "  ", NL: "\n"}.__getitem__)
    assert markup == {PIPE} and space == {SP, NL}
    row = [PIPE, SP] * 30
    assert _run(row, markup_tokens=markup) == "cycle period 2 x 12"
    assert _run(row, markup_tokens=markup, space_tokens=space) is None
    # still bounded, and whitespace alone is never a table
    assert "(table markup)" in _run(row * 5, markup_tokens=markup, space_tokens=space)
    assert (
        _run([SP, NL] * 30, markup_tokens=markup, space_tokens=space)
        == "cycle period 2 x 12"
    )
