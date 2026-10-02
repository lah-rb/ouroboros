#!/usr/bin/env python3
"""
Repetition Guard

Token-level degenerate-repetition detector for the generation loop.

Motivation: some models (notably Gemma-4 31b-dense, see google-deepmind/gemma#622
and ollama#15502) collapse into token-level repetition — a single token, or a
short 2-8 token cycle, repeated until ``max_tokens`` is exhausted. On a slow
dense model that is a ~1 hour hang. ``repeat_penalty`` is confirmed ineffective
upstream, so we detect the collapse directly in the generation loop and abort the
turn with :class:`DegenerateGenerationError`, which the session/agent layers turn
into a clean, fast failure (cancel + purge KV + route to mission_control).

The detector is O(1) per token and intentionally conservative: thresholds are set
high enough that legitimate repetition (indentation, ``====`` rules, repeated code
lines — whose token *content* varies) does not trip it. Only an exact identical
token repeated ``max_run`` times, or an exact length-``p`` block repeated
``min_cycle_reps`` times back-to-back, is treated as degenerate.
"""

import re
from typing import Callable, FrozenSet, List, Optional

# Conservative defaults. A single token repeated 48x, or an exact 2-8 token block
# repeated 12x straight, is never legitimate prose/code but caps a degenerate turn
# at tens of tokens instead of 32768. Tunable per model via GenerationConfig.
DEFAULT_MAX_RUN = 48
DEFAULT_MAX_CYCLE_PERIOD = 8
DEFAULT_MIN_CYCLE_REPS = 12
#: EMPTY TABLE CELLS repeat legitimately: a row of a wide table is ``<td></td>``
#: a dozen times over, an exact 4-token cycle. On 2026-10-02 the guard aborted
#: three table transcriptions (a muse table-triage read, two curate turns that
#: rebuilt an HTML table while reasoning) at "cycle period 4 x 12". A cycle
#: whose every token is bare table markup must repeat this many times instead
#: -- more cells than any printed table row has, since ``</tr><tr>`` breaks
#: the cycle at every row, while a markup loop still dies in a few hundred
#: tokens.
DEFAULT_MARKUP_CYCLE_REPS = 64
#: A token is BARE TABLE MARKUP when its text is cell punctuation with only
#: the tag letters of td/th/tr and whitespace around it -- the pieces of
#: ``<td></td>``, ``<th></th>`` and markdown's ``| |`` / ``|---|:-:|`` rows --
#: or a bare tag name. Any content (a digit, a word) puts the cycle back
#: under the normal threshold, and so does whitespace alone: a whitespace
#: loop is a real degeneration.
_MARKUP_PIECE = re.compile(r"[\s<>/|:\-tdhr]+")
_MARKUP_PUNCT = re.compile(r"[<>/|\-]")
_TAG_NAMES = frozenset({"td", "th", "tr"})


def is_markup_piece(piece: str) -> bool:
    """True for the text of a bare-table-markup token (see _MARKUP_PIECE)."""
    if not piece or _MARKUP_PIECE.fullmatch(piece) is None:
        return False
    return _MARKUP_PUNCT.search(piece) is not None or piece.strip() in _TAG_NAMES


def markup_token_ids(n_vocab: int, piece: Callable[[int], str]) -> FrozenSet[int]:
    """Every token id whose text is bare table markup. ``piece`` maps an id to
    its text; an id it cannot render is skipped."""
    out = set()
    for tid in range(n_vocab):
        try:
            text = piece(tid)
        except Exception:  # noqa: BLE001 — control/byte tokens
            continue
        if is_markup_piece(text):
            out.add(tid)
    return frozenset(out)


class DegenerateGenerationError(RuntimeError):
    """Raised when the generation loop detects degenerate token repetition (or a
    detokenization failure on a degenerate token stream).

    Subclasses ``RuntimeError`` so the existing broad ``except Exception`` in the
    GraphQL/effects layer surfaces it as a normal inference error (which routes to
    mission_control) rather than an opaque crash.
    """

    # The count goes in the MESSAGE, not just on the attribute. Only the
    # message survives the GraphQL boundary: the client sees a serialized
    # error string, so an attribute here is invisible to it. Without this the
    # agent recorded `tokens_generated=0` for an aborted call and every
    # token-efficiency figure silently understated the models that degenerate
    # — laguna-s-2.1-apex traced 2,883 generated tokens for a run whose single
    # runaway capture alone held 45,056 (2026-07-30).
    #
    # The suffix is safe to append: the client's `_degenerate_reason()` takes
    # the message from its marker onward, so the count rides along into the
    # logged reason instead of being stripped.
    _TOKENS_SUFFIX = "aborted after"

    def __init__(self, reason: str, tokens_generated: int = 0) -> None:
        super().__init__(
            f"{reason} ({self._TOKENS_SUFFIX} {tokens_generated} generated tokens)"
            if tokens_generated
            else reason
        )
        self.reason = reason
        self.tokens_generated = tokens_generated


class RepetitionGuard:
    """Incremental degenerate-repetition detector.

    Feed every generated token id to :meth:`observe`; it returns a human-readable
    reason string the moment the stream looks degenerate, else ``None``.

    Two independent signals:
      * **run-length** — the same token id repeated ``>= max_run`` times in a row.
      * **short-cycle** — an exact length-``p`` block (``2 <= p <= max_cycle_period``)
        repeated ``>= min_cycle_reps`` times back-to-back; ``>= markup_cycle_reps``
        when every token of the block is in ``markup_tokens`` (empty table
        cells, see DEFAULT_MARKUP_CYCLE_REPS).
    """

    def __init__(
        self,
        max_run: int = DEFAULT_MAX_RUN,
        max_cycle_period: int = DEFAULT_MAX_CYCLE_PERIOD,
        min_cycle_reps: int = DEFAULT_MIN_CYCLE_REPS,
        markup_tokens: FrozenSet[int] = frozenset(),
        markup_cycle_reps: int = DEFAULT_MARKUP_CYCLE_REPS,
    ) -> None:
        self.max_run = max_run
        self.max_cycle_period = max_cycle_period
        self.min_cycle_reps = min_cycle_reps
        self.markup_tokens = markup_tokens
        self.markup_cycle_reps = max(markup_cycle_reps, min_cycle_reps)

        # run-length state
        self._run_token: Optional[int] = None
        self._run_len = 0

        # short-cycle state: a bounded tail ring of recent token ids, just long
        # enough to test the largest cycle we look for.
        self._ring_cap = max_cycle_period * (
            self.markup_cycle_reps if markup_tokens else min_cycle_reps
        )
        self._ring: List[int] = []

    def observe(self, token: int) -> Optional[str]:
        """Record one token; return a reason string if degenerate, else None."""
        # ── run-length ──────────────────────────────────────────────
        if token == self._run_token:
            self._run_len += 1
        else:
            self._run_token = token
            self._run_len = 1
        if self.max_run > 0 and self._run_len >= self.max_run:
            return f"run-length {self._run_len} of token {token}"

        # ── short-cycle ─────────────────────────────────────────────
        self._ring.append(token)
        if len(self._ring) > self._ring_cap:
            del self._ring[0]
        reps = self.min_cycle_reps
        for p in range(2, self.max_cycle_period + 1):
            need = p * reps
            if len(self._ring) < need:
                continue
            block = self._ring[-p:]
            # A constant block is a run, not a cycle — run-length owns it (so the
            # two signals stay independent and keep their distinct thresholds).
            if len(set(block)) < 2:
                continue
            window = self._ring[-need:]
            # window is `reps` consecutive copies of `block`?
            if all(window[i] == block[i % p] for i in range(need)):
                if not (self.markup_tokens and set(block) <= self.markup_tokens):
                    return f"cycle period {p} x {reps}"
                # Empty table cells: legitimate until markup_cycle_reps.
                long_need = p * self.markup_cycle_reps
                if len(self._ring) >= long_need:
                    long_window = self._ring[-long_need:]
                    if all(long_window[i] == block[i % p] for i in range(long_need)):
                        return f"cycle period {p} x {self.markup_cycle_reps} (table markup)"
        return None
