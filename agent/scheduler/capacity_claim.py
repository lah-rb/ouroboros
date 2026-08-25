"""This unit's KV claim, visible to the action that spends it.

WHY A CONTEXTVAR AND NOT AN ARGUMENT. A lane's work runs as a FLOW, through
`execute_flow`, and threading a scheduler object down to an action would put
the dispatcher into every action signature it passes. The two alternatives
were worse: `_build_step_input` filters the accumulator to declared keys plus
`_AMBIENT_CONTEXT_KEYS`, so an undeclared extra input never reaches
`step_input.context`; and declaring one in the `.cue` changes the flow hash
pinned by `tests/test_scraper_v1_frozen.py`. A ContextVar set inside the
lane's task is inherited by everything that task awaits and by nothing else,
which is exactly the isolation four concurrent lanes need.

WHY IT CARRIES `resize`. A lane is admitted against whatever was free and
claims all of it, because the alternative is a fixed estimate that is wrong
in both directions. It then learns what its document actually costs and hands
the remainder back. Without that hand-back, the "several small docs give us
parallelism" half of the design never happens: the first lane would sit on
the whole pool for the length of its unit.

`current_claim()` is None outside the worker pool — the dispatch curator,
single-lane callers, every test that does not build a pool. Callers fall back
to their own sizing, which is the pre-capacity behaviour.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

__all__ = ["Claim", "claim_scope", "current_claim"]


@dataclass
class Claim:
    """KV cells this unit was admitted against, and the seam to give some back."""

    tokens: int
    lane: str = ""
    #: Set by the pool; absent in tests that construct a Claim directly.
    _model: Any = field(default=None, repr=False)
    _token: str = field(default="", repr=False)

    def resize(self, tokens: int) -> None:
        """Correct the claim DOWNWARD once the real cost is known.

        Downward only, and the model enforces it too. Growing a claim is a
        second admission decision, and this object is not the admission
        authority — the engine's own `_size_against_pool` is. A claim that
        could grow would be a client quietly re-admitting itself.
        """
        try:
            tokens = int(tokens)
        except (TypeError, ValueError):
            return
        if tokens <= 0 or tokens >= self.tokens:
            return
        self.tokens = tokens
        model, token = self._model, self._token
        if model is not None and token:
            try:
                model.resize(token, tokens)
            except Exception:  # noqa: BLE001 — never break a lane on bookkeeping
                pass


_CURRENT: ContextVar[Optional[Claim]] = ContextVar("capacity_claim", default=None)


def current_claim() -> Optional[Claim]:
    """This unit's claim, or None when not running under the worker pool."""
    return _CURRENT.get()


@contextmanager
def claim_scope(claim: Optional[Claim]) -> Iterator[Optional[Claim]]:
    """Bind `claim` for the duration of one unit of work."""
    token = _CURRENT.set(claim)
    try:
        yield claim
    finally:
        _CURRENT.reset(token)
