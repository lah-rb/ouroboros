"""Backend factory lifecycle — the global reference is a resource handle.

`_backend_instance` is not a convenience cache. It is the ONLY handle that can
reach the pool's C-level llama contexts and their wired GPU memory. Teardown
used to null it inside a `finally`, so a shutdown that threw partway — the
scaler task, refresh loop, seat reaper, decode thread, N contexts, then the
primary, any one of them — orphaned everything it had not yet freed, and the
next initialize saw None and allocated a FULL second pool on top.

On a 128GB unified-memory machine that is the reboot class this codebase has
already paid for three times, and the swap path (core/model_swap.py:195,211)
runs teardown-then-initialize once per model change. `test_model_swap.py`
covers the swap against a shutdown that always succeeds; these cover the one
that does not.
"""

from __future__ import annotations

import pytest

import inference.backends.factory as factory
from tests.conftest import make_config


class _Backend:
    backend_name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.shutdown_calls = 0

    async def initialize(self):
        return None

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.fail:
            raise RuntimeError("context teardown exploded halfway")


@pytest.fixture(autouse=True)
def _clean_factory_state():
    """Every test starts and ends with no backend and no failure latch."""
    factory._reset_teardown_state()
    yield
    factory._reset_teardown_state()


@pytest.mark.asyncio
async def test_clean_shutdown_clears_the_global():
    factory.set_backend(_Backend())
    await factory.shutdown_backend_async()
    assert factory.get_backend() is None
    assert factory.teardown_failed() is False


@pytest.mark.asyncio
async def test_failed_shutdown_keeps_the_reference():
    """The orphan-prevention invariant: a throwing shutdown must NOT drop the
    only handle to still-allocated GPU resources."""
    be = _Backend(fail=True)
    factory.set_backend(be)

    await factory.shutdown_backend_async()  # must not raise

    assert be.shutdown_calls == 1
    assert factory.get_backend() is be, (
        "a failed teardown dropped the backend reference — the C contexts and "
        "their wired memory are now unreachable"
    )
    assert factory.teardown_failed() is True


@pytest.mark.asyncio
async def test_initialize_refuses_after_a_failed_teardown():
    """...and the next initialize must not stack a second pool on top."""
    factory.set_backend(_Backend(fail=True))
    await factory.shutdown_backend_async()

    with pytest.raises(RuntimeError, match="teardown previously FAILED"):
        await factory.initialize_backend_async(make_config())


def test_sync_initialize_refuses_after_a_failed_teardown():
    """The sync wrapper is a separate entry point and needs the same guard —
    a CLI bootstrap must not be the way a second pool gets allocated."""
    factory._teardown_failed = True
    with pytest.raises(RuntimeError, match="teardown previously FAILED"):
        factory.initialize_backend(make_config())


@pytest.mark.asyncio
async def test_shutdown_is_a_noop_when_nothing_is_initialized():
    assert factory.get_backend() is None
    await factory.shutdown_backend_async()
    assert factory.teardown_failed() is False


@pytest.mark.asyncio
async def test_reset_clears_the_latch_for_a_recovered_operator():
    """The latch is escapable, deliberately: an operator who has confirmed the
    resources were reclaimed (or a test) can clear it. It is a safety
    interlock, not a permanent brick."""
    factory.set_backend(_Backend(fail=True))
    await factory.shutdown_backend_async()
    assert factory.teardown_failed() is True

    factory._reset_teardown_state()

    assert factory.teardown_failed() is False
    assert factory.get_backend() is None
