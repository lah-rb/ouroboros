"""Pool-mode release_instance hardening (epoch-batch W7, 2026-08-03).

The batched path got shield+finally discipline in the 2026-07-24 seat-leak
fix (test_batched_seat_release.py); the POOL path kept the original shape —
``await self._heal_instance(inst)`` ran bare before the requeue, so a
CancelledError landing inside the heal (watchdog cancel, client disconnect)
escaped past the requeue and the checkout decrement. On a limit=1 pool that
permanently strands the ONLY instance: every later acquire times out until
the server restarts.

Pins the mirrored discipline:

  1. a heal that RAISES still requeues + decrements (except Exception);
  2. a cancel landing DURING the heal interrupts it (recoverable BY
     DESIGN — _heal_instance leaves the flag set, the next
     release/acquire retries) but the finally still requeues +
     decrements: THE leak this file exists for;
  3. a duplicate release cannot double-queue the instance (QueueFull is
     swallowed, counter clamps at 0).

Style follows test_batched_seat_release.py: the REAL backend with fake
instances, no model, sync tests driving asyncio.run.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402


def _backend() -> LlamaCppBackend:
    config = SimpleNamespace(
        resources=SimpleNamespace(
            cpu_threads=1,
            max_concurrent_requests=2,
            jit_concurrency_limit=None,
            scale_wait_timeout=0.5,
            instance_idle_ttl=0.1,
        ),
        app=SimpleNamespace(backend_timeout=0.2),
        model=SimpleNamespace(
            family="harmony",
            context_refresh_interval=75,
            context_refresh_seconds=1800,
            context_refresh_drain_s=0.0,
        ),
    )
    be = LlamaCppBackend(config)
    be._decode_mode = "pool"
    be._ready_event.set()
    return be


def _wire(be, maxsize=2):
    be._pool_queue = asyncio.Queue(maxsize=maxsize)
    be._persona_queues = {"default": be._pool_queue}
    inst = SimpleNamespace(_needs_context_refresh=False, _persona="default")
    be._instance_meta[id(inst)] = SimpleNamespace(last_released_at=None)
    be._checked_out = 1
    return inst


# ── 1. a raising heal still returns the instance ──────────────────────


def test_release_requeues_when_heal_raises():
    be = _backend()

    async def broken_heal(inst):
        raise RuntimeError("context rebuild exploded")

    be._heal_instance = broken_heal

    async def scenario():
        inst = _wire(be)
        inst._needs_context_refresh = True

        await be.release_instance(inst)

        assert be._pool_queue.qsize() == 1, "instance not requeued"
        assert be._pool_queue.get_nowait() is inst
        assert be._checked_out == 0, "checkout counter leaked"
        assert be._instance_meta[id(inst)].last_released_at is not None

    asyncio.run(scenario())


# ── 2. a cancel mid-heal cannot skip the requeue — THE leak ───────────


def test_release_requeues_under_cancel_during_heal():
    be = _backend()
    gate = {}

    async def slow_heal(inst):
        # Mimic the real contract: the flag clears only on a COMPLETED heal.
        gate["entered"] = asyncio.Event()
        gate["entered"].set()
        await asyncio.Event().wait()  # cancel lands while suspended HERE
        inst._needs_context_refresh = False  # pragma: no cover — never reached

    be._heal_instance = slow_heal

    async def scenario():
        inst = _wire(be)
        inst._needs_context_refresh = True

        task = asyncio.create_task(be.release_instance(inst))
        for _ in range(50):
            await asyncio.sleep(0)
            if "entered" in gate:
                break
        assert "entered" in gate, "heal never entered"

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # the CancelledError may propagate — AFTER the finally

        # The old shape leaked exactly here: CancelledError sailed past the
        # bare heal await and neither line below held on a limit=1 pool.
        assert be._pool_queue.qsize() == 1, "instance not requeued"
        assert be._checked_out == 0, "checkout counter leaked"
        # Interrupted heal is recoverable: flag stays set for the retry.
        assert inst._needs_context_refresh is True

    asyncio.run(scenario())


# ── 3. duplicate release cannot seat one instance twice ───────────────


def test_duplicate_release_does_not_double_queue():
    be = _backend()

    async def scenario():
        inst = _wire(be, maxsize=1)

        await be.release_instance(inst)
        assert be._pool_queue.qsize() == 1 and be._checked_out == 0

        # late duplicate (leaked consumer finally): QueueFull swallowed
        await be.release_instance(inst)
        assert be._pool_queue.qsize() == 1, "instance double-queued"
        assert be._checked_out == 0, "counter went negative"

    asyncio.run(scenario())
