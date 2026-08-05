"""Server-side retirement of abandoned non-streaming completions.

THE GAP (Block E1, 2026-07-30): a POST client that abandoned a non-streaming
`completion` was invisible — the server decoded to completion for nobody. E1
demonstrated it at scale: a 10-minute client timeout left all 64 generations
running to the end. The STREAMING path never had this gap; non-streaming just
never LEARNED of the disconnect.

The fix polls ``request.is_disconnected()`` and cancels the work task, landing
the cancellation in machinery that already exists (shielded ``agen.aclose()`` →
bridge ``closed`` → ``_retire_abandoned``). Scoping is deliberate and each
boundary has a test: batched-only (a pool worker thread cannot be cancelled —
cancelling the awaiter would release the generation guard while the thread
still decodes), and absent-info stands down (direct-call tests, older shims).
"""

from __future__ import annotations

import asyncio

import pytest

from api.graphql_api import _run_retiring_on_disconnect


class _Req:
    def __init__(self, disconnect_after: int = 10**9):
        self.polls = 0
        self._after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.polls += 1
        return self.polls > self._after


class _Info:
    def __init__(self, request):
        self.context = {"request": request}


def _backend(mode="batched"):
    class _B:
        _decode_mode = mode

    return _B()


@pytest.fixture
def batched(monkeypatch):
    import api.graphql_api as mod

    async def fake():
        return _backend("batched")

    monkeypatch.setattr(mod, "_get_backend_for_mode", fake)


@pytest.fixture
def pool(monkeypatch):
    import api.graphql_api as mod

    async def fake():
        return _backend("pool")

    monkeypatch.setattr(mod, "_get_backend_for_mode", fake)


@pytest.mark.asyncio
async def test_disconnect_cancels_the_work(batched):
    """The E1 shape: client gone mid-generation → the work task is CANCELLED
    (which is what reaches _retire_abandoned) and the resolver raises."""
    cancelled = asyncio.Event()

    async def slow_work():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    req = _Req(disconnect_after=0)  # gone on the first poll
    with pytest.raises(RuntimeError, match="retired server-side"):
        await _run_retiring_on_disconnect(_Info(req), slow_work)
    assert cancelled.is_set(), "the generation must actually be cancelled"


@pytest.mark.asyncio
async def test_fast_completion_returns_before_any_poll_matters(batched):
    async def quick():
        return "done"

    req = _Req()
    assert await _run_retiring_on_disconnect(_Info(req), quick) == "done"


@pytest.mark.asyncio
async def test_pool_mode_stands_down(pool):
    """A pool worker thread cannot be cancelled; cancelling the awaiter would
    release the generation guard while the thread still decodes — the
    instance-reuse hazard. Pool keeps run-to-completion until its generation
    is routed through the closeable streaming path."""

    async def work():
        # LONGER than one poll interval (2s), on purpose: a fast work item
        # finishes before the first poll either way, so it cannot distinguish
        # "watcher stood down" from "watcher active but never got a turn" —
        # the mutant that watches pool mode survived on exactly that.
        await asyncio.sleep(2.3)
        return "ran to completion"

    req = _Req(disconnect_after=0)  # would cancel on the FIRST poll if watched
    out = await _run_retiring_on_disconnect(_Info(req), work)
    assert out == "ran to completion"
    assert req.polls == 0, "pool mode must not even start the watcher"


@pytest.mark.asyncio
async def test_absent_info_stands_down(batched):
    async def work():
        return "ok"

    assert await _run_retiring_on_disconnect(None, work) == "ok"


@pytest.mark.asyncio
async def test_poll_failure_never_kills_the_work(batched):
    """A broken is_disconnected (test double, proxy quirk) must be read as
    'still connected' — the poll is advisory, the WORK is the point."""

    class _BrokenReq:
        async def is_disconnected(self):
            raise OSError("transport gone weird")

    async def work():
        # Past one poll interval so the broken poll actually FIRES mid-work
        # (at 0.05s the first poll never happened and the gone=True mutant
        # survived untested).
        await asyncio.sleep(2.3)
        return "survived"

    out = await _run_retiring_on_disconnect(_Info(_BrokenReq()), work)
    assert out == "survived"


@pytest.mark.asyncio
async def test_work_exception_propagates_unchanged(batched):
    async def bad():
        raise ValueError("model said no")

    with pytest.raises(ValueError, match="model said no"):
        await _run_retiring_on_disconnect(_Info(_Req()), bad)


@pytest.mark.asyncio
async def test_resolver_cancellation_takes_the_work_down(batched):
    """Server shutdown cancels the resolver — the work must not be orphaned
    (that would recreate the exact gap this closes, one level up)."""
    cancelled = asyncio.Event()

    async def slow_work():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    req = _Req()
    outer = asyncio.create_task(_run_retiring_on_disconnect(_Info(req), slow_work))
    await asyncio.sleep(0.05)
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert cancelled.is_set()
