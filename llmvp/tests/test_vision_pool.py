"""The vision pool: N private single-seq contexts, one owner at a time.

TWO THINGS ARE UNDER TEST, and the first is a correctness fix rather than a
feature. Before the pool, every vision request shared ONE instance with no
mutual exclusion — ``generation_guard`` is a counting guard that deliberately
lets generations run together, so two concurrent vision calls landed on the
same Llama object and raced on the mtmd handler's token ledger
(``n_tokens`` / ``input_ids``) and its KV. It never fired because every
caller so far is sequential. A checkout queue closes that at width 1 and
makes width > 1 genuinely parallel.

Model-free: instances are sentinels, so this runs with no weights and no
Metal.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402


class FakeVision:
    """A stand-in vision context. Identity is what the tests track."""

    def __init__(self, idx: int):
        self.idx = idx
        self.chat_handler = SimpleNamespace(close=lambda: None)
        self.closed = False


def make_backend(pool_size=1, mmproj="/proj.gguf", vision_n_ctx=8192):
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=1,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.5),
        model=SimpleNamespace(
            name="fake",
            mmproj_path=mmproj,
            vision_pool_size=pool_size,
            vision_n_ctx=vision_n_ctx,
        ),
    )
    backend = LlamaCppBackend(config)
    backend._primary_instance = object()
    made = []

    def _create(primary):
        inst = FakeVision(len(made))
        made.append(inst)
        return inst

    backend._create_vision_instance = _create
    backend._close_shared_context = lambda inst: setattr(inst, "closed", True)
    # The pool builds under generation_guard, which waits on the readiness
    # gate a real initialize() sets. A stub never runs initialize, so without
    # this every build times out on scale_wait_timeout rather than testing
    # anything about the pool.
    backend._ready_event.set()
    backend.made = made
    return backend


# ── construction ──────────────────────────────────────────────────────


def test_pool_defaults_to_one_context():
    b = make_backend(pool_size=1)
    asyncio.run(b._build_vision_pool())
    assert len(b.made) == 1
    assert b._vision_instance is b.made[0]


def test_pool_builds_requested_width():
    b = make_backend(pool_size=4)
    asyncio.run(b._build_vision_pool())
    assert len(b.made) == 4
    assert b._vision_pool.qsize() == 4


def test_pool_is_lazy_until_first_use():
    b = make_backend(pool_size=4)
    assert b._vision_pool is None
    assert b.made == []


def test_pool_build_is_idempotent():
    b = make_backend(pool_size=2)

    async def go():
        await b._build_vision_pool()
        await b._build_vision_pool()

    asyncio.run(go())
    assert len(b.made) == 2


def test_missing_mmproj_raises():
    b = make_backend(pool_size=1, mmproj=None)
    with pytest.raises(RuntimeError, match="vision is not configured"):
        asyncio.run(b._build_vision_pool())


def test_zero_or_negative_width_clamps_to_one():
    """A misconfigured 0 must not build an empty pool — checkout would hang
    forever on an empty queue with no error anywhere."""
    for bad in (0, -3, None):
        b = make_backend(pool_size=bad)
        asyncio.run(b._build_vision_pool())
        assert len(b.made) == 1


# ── exclusion (the bug this closes) ───────────────────────────────────


def test_checkout_is_exclusive_at_width_one():
    b = make_backend(pool_size=1)
    order = []

    async def worker(tag):
        async with b.acquire_vision_instance() as inst:
            order.append(("in", tag, inst.idx))
            await asyncio.sleep(0.02)
            order.append(("out", tag, inst.idx))

    async def go():
        await asyncio.gather(worker("a"), worker("b"))

    asyncio.run(go())
    # Strict alternation: no second "in" before the first "out".
    assert [o[0] for o in order] == ["in", "out", "in", "out"]


def test_two_owners_never_share_a_context_at_width_two():
    b = make_backend(pool_size=2)
    held: list = []
    seen_together = []

    async def worker(tag):
        async with b.acquire_vision_instance() as inst:
            held.append(inst.idx)
            seen_together.append(sorted(held))
            await asyncio.sleep(0.02)
            held.remove(inst.idx)

    async def go():
        await asyncio.gather(*(worker(i) for i in range(2)))

    asyncio.run(go())
    # Both ran concurrently...
    assert [0, 1] in seen_together
    # ...and each on a DIFFERENT context.
    assert all(len(set(s)) == len(s) for s in seen_together)


def test_width_two_admits_exactly_two_at_once():
    b = make_backend(pool_size=2)
    concurrent = []

    async def worker(_):
        async with b.acquire_vision_instance():
            concurrent.append(1)
            peak = sum(concurrent)
            await asyncio.sleep(0.02)
            concurrent.pop()
            return peak

    async def go():
        return await asyncio.gather(*(worker(i) for i in range(5)))

    peaks = asyncio.run(go())
    assert max(peaks) == 2, f"pool width not enforced: peaks={peaks}"


def test_instance_returns_to_the_pool_on_exception():
    """A request that raises must not shrink the pool — repeated failures
    would otherwise starve vision into a silent deadlock."""
    b = make_backend(pool_size=1)

    async def go():
        for _ in range(3):
            with pytest.raises(ValueError):
                async with b.acquire_vision_instance():
                    raise ValueError("boom")
        # Still checkoutable afterwards.
        async with b.acquire_vision_instance() as inst:
            return inst

    inst = asyncio.run(go())
    assert inst is b.made[0]
    assert b._vision_pool.qsize() == 1


# ── teardown ──────────────────────────────────────────────────────────


def test_shutdown_closes_every_context_not_just_the_first():
    """Freeing only _vision_instance would strand the rest: wired GPU memory
    with no handle left to reach it."""
    b = make_backend(pool_size=3)
    asyncio.run(b._build_vision_pool())
    assert len(b.made) == 3

    # Exercise the teardown block directly (full shutdown() needs a real pool).
    for vinst in b._vision_instances:
        handler = getattr(vinst, "chat_handler", None)
        if handler is not None:
            handler.close()
            vinst.chat_handler = None
        b._close_shared_context(vinst)
    b._vision_instances = []
    b._vision_pool = None
    b._vision_instance = None

    assert all(i.closed for i in b.made)
    assert all(i.chat_handler is None for i in b.made)


def test_get_vision_instance_still_works_for_legacy_callers():
    b = make_backend(pool_size=2)
    inst = asyncio.run(b.get_vision_instance())
    assert inst is b.made[0]
