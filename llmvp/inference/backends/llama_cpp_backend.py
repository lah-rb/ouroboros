#!/usr/bin/env python3
"""
Llama.cpp Backend Implementation

Uses a shared-model architecture: model weights are loaded once
and multiple inference contexts share them.  This drastically
reduces memory usage for large models and avoids GPU memory
bandwidth contention from duplicate weight reads.

JIT pool scaling creates instances on-demand and tears them down
when idle, keeping single-instance performance optimal during
low-traffic periods and scaling up for concurrent load.
"""

import asyncio
import contextlib
import ctypes
import logging
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Iterator, List, Optional

import numpy as np
import numpy.typing as npt

from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .base import BaseBackend, BackendCapabilities

log = logging.getLogger("llm-mvp")


class LlamaCppBackend(BaseBackend):
    """
    Llama.cpp backend implementation using llama-cpp-python.

    Uses a **shared-model** pool strategy:

    *   One primary ``Llama`` instance loads the model weights.
    *   Additional pool slots create lightweight contexts that
        reference the *same* model weights in memory, each with
        its own KV-cache and mutable inference state.

    This avoids duplicating ~15 GB+ of weights per pool slot,
    keeping memory usage at:

        1 × model_weights  +  N × kv_cache

    instead of:

        N × (model_weights + kv_cache)

    Supports hybrid/recurrent models (e.g. Qwen3-Next) via state
    snapshotting of the static context.
    """

    # How often the JIT scaler checks for idle instances to tear down.
    _JIT_SCALER_INTERVAL: float = 300.0

    # Minimum seconds after a scale-up before the scaler may tear down.
    _JIT_COOLDOWN: float = 300.0

    def __init__(self, config: Any):
        self._primary_instance: Any = None  # Owns the model weights
        self._pool_queue: Optional[asyncio.Queue] = None
        self._pool_size = config.resources.max_concurrent_requests
        self._all_instances: List[Any] = []  # For shutdown cleanup
        self._llama_module = None
        self._tokenizer = None
        # Hybrid/recurrent model support
        self._is_hybrid = False
        self._static_state = None  # Saved LlamaState after processing static tokens
        # JIT pool scaling
        jit_limit = config.resources.jit_concurrency_limit
        self._jit_enabled: bool = jit_limit is not None
        self._jit_limit: int = jit_limit or self._pool_size
        self._spawn_lock: asyncio.Lock = asyncio.Lock()
        self._scaler_task: Optional[asyncio.Task] = None
        # Readiness gate — blocks acquire_instance() until initialize() completes
        self._ready_event: asyncio.Event = asyncio.Event()
        # Scaling gate — cleared during JIT scale-up/down to block all
        # acquire_instance() callers until scaling completes.
        self._scaling_gate: asyncio.Event = asyncio.Event()
        self._scaling_gate.set()  # Open by default
        # In-flight tracking — counts instances currently checked out.
        self._in_flight: int = 0
        self._drain_event: asyncio.Event = asyncio.Event()
        self._drain_event.set()  # Initially "drained" (no in-flight)
        # Cooldown tracking — prevents scaler from destroying freshly
        # created instances.
        self._last_scale_up_time: float = 0.0
        super().__init__(config)

    @property
    def backend_name(self) -> str:
        return "llama_cpp"

    def _detect_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            streaming=True,
            batching=False,
            async_api=False,  # llama.cpp is sync-only
            chat_template=True,
            quantization=True,
            gpu_acceleration=True,
            manual_pooling=True,
        )

    def _get_llama_class(self):
        """Lazy import Llama class."""
        if self._llama_module is None:
            from llama_cpp import Llama

            self._llama_module = Llama
        return self._llama_module

    def _create_primary_instance(self) -> Any:
        """Create the primary Llama instance that owns the model weights."""
        Llama = self._get_llama_class()

        return Llama(
            model_path=str(self.config.model.path),
            n_ctx=self.config.model.n_ctx,
            n_gpu_layers=self.config.model.n_gpu_layers,
            gpu_backend="metal",
            flash_attn=bool(self.config.model.flash_attention),
            seed=self.config.model.seed,
            verbose=self.config.model.verbose,
            n_threads=self.config.resources.cpu_threads,
            batch_size=getattr(self.config.model, "batch_size", 64),
        )

    def _create_shared_instance(self, primary: Any) -> Any:
        """Create a pool instance that shares model weights with the primary.

        The returned object is a full ``Llama`` instance (same class,
        same public API) but its ``_model`` attribute points to the
        *primary's* model — no duplicate weight load.  It gets its own
        ``_ctx`` (KV-cache), ``_batch``, ``input_ids``, ``scores``, and
        sampler state so that inference is fully independent.
        """
        import copy

        from llama_cpp import internals, llama_cpp as lc

        # Shallow-copy the primary to inherit all config / metadata.
        inst = copy.copy(primary)

        # --- Replace mutable, per-instance objects ---

        # New ExitStack (we manage cleanup ourselves in shutdown)
        inst._stack = contextlib.ExitStack()

        # New context from the *shared* model
        ctx = internals.LlamaContext(
            model=primary._model,
            params=primary.context_params,
            verbose=False,
        )
        inst._ctx = ctx

        # New batch
        batch = internals.LlamaBatch(
            n_tokens=primary.n_batch,
            embd=0,
            n_seq_max=primary.context_params.n_seq_max,
            verbose=False,
        )
        inst._batch = batch

        # New mutable arrays
        inst.input_ids = np.ndarray((primary._n_ctx,), dtype=np.intc)
        logits_rows = primary._n_ctx if primary._logits_all else primary.n_batch
        inst.scores = np.ndarray((logits_rows, primary._n_vocab), dtype=np.single)
        inst._candidates = internals.LlamaTokenDataArray(n_vocab=primary._n_vocab)
        inst.n_tokens = 0
        inst._mirostat_mu = ctypes.c_float(2.0 * 5.0)
        inst._sampler = None
        inst._sampling_ctx = None  # v0.3.31: generate() creates its own
        inst.cache = None

        # v0.3.31 hybrid/recurrent model support — each instance needs
        # its own checkpoint manager to avoid concurrent state corruption.
        if getattr(primary, "_hybrid_cache_mgr", None) is not None:
            from llama_cpp.llama import HybridCheckpointCache

            inst._hybrid_cache_mgr = HybridCheckpointCache(
                ctx.ctx,
                max_checkpoints=getattr(primary, "ctx_checkpoints", 16),
                verbose=False,
            )
        else:
            inst._hybrid_cache_mgr = None

        return inst

    # ------------------------------------------------------------------
    # Model architecture detection (informational)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_is_hybrid(llm_inst: Any) -> bool:
        """Detect whether the loaded model has hybrid or recurrent layers.

        Used for informational logging only — the state-snapshot warm-up
        strategy is used for *all* model architectures.
        """
        try:
            import llama_cpp as lc

            model_ptr = llm_inst._model.model
            return bool(
                lc.llama_model_is_hybrid(model_ptr)
                or lc.llama_model_is_recurrent(model_ptr)
            )
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Warm-up via state snapshotting
    # ------------------------------------------------------------------

    def _warm_up_instance(self, llm_inst: Any, idx: int = 0) -> None:
        """Warm up by evaluating static tokens and snapshotting model state.

        This strategy is used for every model architecture:

        1. **First pool slot**: evaluates all static tokens through the
           model (populating KV cache and, for hybrid models, the
           recurrent state), then saves a full state snapshot.
        2. **Subsequent pool slots**: loads the pre-computed snapshot
           instantly — no re-evaluation needed.
        3. **Before every request** (in ``acquire_instance``): the
           snapshot is restored so the model always begins from a
           pristine post-static-context state, guaranteeing correct
           prefix matching and perfect request isolation.
        """
        try:
            from preprocessing.static_tokens import get_static_tokens

            static_tokens = get_static_tokens()

            if self._static_state is not None:
                # State already computed by the first instance — just load it.
                llm_inst.load_state(self._static_state)
                log.info(f"✅ Loaded pre-computed static state for pool slot #{idx}")
                return

            n_tokens = len(static_tokens)
            log.info(
                f"🔄 Processing {n_tokens:,} static tokens for state "
                f"snapshot (pool slot #{idx})…"
            )

            llm_inst.reset()
            llm_inst.eval(list(static_tokens))

            self._static_state = llm_inst.save_state()

            state_mb = self._static_state.llama_state_size / (1024 * 1024)
            log.info(
                f"✅ State snapshot saved — {n_tokens:,} tokens, "
                f"{state_mb:,.1f} MiB C-level state (pool slot #{idx})"
            )
        except Exception as exc:
            log.error(f"❌ Warm-up failed for pool slot #{idx}: {exc}")

    def _rewarm_after_teardown(self) -> None:
        """Re-warm the primary instance after scale-down.

        Calls ``reset()`` to release KV-cache contents, then
        ``load_state()`` to restore the static context.  This gives
        the Metal allocator an opportunity to optimise memory layout
        after shared contexts are freed, helping recover
        single-instance inference speed.
        """
        self._primary_instance.reset()
        self._primary_instance.load_state(self._static_state)
        log.info("🔄 Re-warmed primary instance after scale-down")

    # ------------------------------------------------------------------
    # Shared JIT scaling helpers
    # ------------------------------------------------------------------

    def _drain_queue(self) -> List[Any]:
        """Remove and return all instances currently in the pool queue."""
        instances: List[Any] = []
        while not self._pool_queue.empty():
            try:
                instances.append(self._pool_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return instances

    async def _drain_in_flight(self, operation: str) -> bool:
        """Wait for all checked-out instances to be returned.

        Returns ``True`` if all in-flight work completed within the
        configured ``backend_timeout``, ``False`` on timeout.
        """
        if self._in_flight <= 0:
            return True

        timeout = self.config.app.backend_timeout
        log.info(
            f"⏳ Waiting for {self._in_flight} in-flight "
            f"request(s) to complete before {operation}…"
        )
        try:
            await asyncio.wait_for(self._drain_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            log.warning(
                "⚠️ Drain timed out after %ds during %s (in_flight=%d)",
                timeout,
                operation,
                self._in_flight,
            )
            return False

    def _requeue_instances(self, instances: List[Any]) -> None:
        """Put instances back into the pool queue."""
        for inst in instances:
            self._pool_queue.put_nowait(inst)

    async def _cancel_scaler_task(self) -> None:
        """Cancel and await the background scaler task if running."""
        if self._scaler_task is not None:
            self._scaler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._scaler_task
            self._scaler_task = None

    async def _restart_scaler_task(self) -> None:
        """Cancel the current scaler and start a fresh one.

        Resets the sleep timer so the next scale-down check counts
        from NOW, not from when the old task last woke up.
        """
        await self._cancel_scaler_task()
        self._scaler_task = asyncio.create_task(self._scaler_loop())

    @contextlib.asynccontextmanager
    async def _scaling_operation(self):
        """Close the scaling gate on enter, always reopen on exit.

        Prevents ``acquire_instance()`` from handing out instances
        while a JIT scale-up or scale-down is modifying the pool.
        A permanently-closed gate deadlocks the server, so the
        ``finally`` block guarantees reopening even on cancellation.
        """
        self._scaling_gate.clear()
        try:
            yield
        finally:
            self._scaling_gate.set()

    # ------------------------------------------------------------------
    # Pool lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Initialize the Llama instance pool with warm-up.

        In **eager** mode (``jit_concurrency_limit`` not set), all
        ``max_concurrent_requests`` instances are created at startup.

        In **JIT** mode, only the primary instance is created.  Extra
        instances are spawned on-demand by ``acquire_instance()`` up
        to ``jit_concurrency_limit``, then reaped by the background
        scaler when idle.
        """
        if self._primary_instance is not None:
            return  # Already initialized

        mode = "JIT" if self._jit_enabled else "eager"
        limit = self._jit_limit if self._jit_enabled else self._pool_size
        log.info(f"🔥 Initializing Llama pool ({mode} mode, " f"limit={limit})")

        # Create the primary instance (loads model weights once).
        self._primary_instance = self._create_primary_instance()
        self._is_hybrid = self._check_is_hybrid(self._primary_instance)

        if self._is_hybrid:
            log.info("🧬 Hybrid/recurrent model detected")

        # Warm-up primary instance: evaluate static tokens & save snapshot.
        await run_in_threadpool(self._warm_up_instance, self._primary_instance, 0)
        self._all_instances.append(self._primary_instance)

        if self._jit_enabled:
            # JIT mode: start with just the primary.  The queue is
            # unbounded so that dynamically spawned instances can be
            # added without hitting a maxsize cap.
            self._pool_queue = asyncio.Queue()
            self._pool_queue.put_nowait(self._primary_instance)

            # Start the background scaler that tears down idle instances.
            self._scaler_task = asyncio.create_task(self._scaler_loop())

            log.info(
                f"✅ Llama pool ready (JIT mode, 1/{self._jit_limit} "
                f"instances, shared_model=True, hybrid={self._is_hybrid})"
            )
        else:
            # Eager mode: pre-allocate all instances at startup.
            for i in range(1, self._pool_size):
                inst = self._create_shared_instance(self._primary_instance)
                log.info(
                    f"🔗 Created shared context for pool slot #{i} "
                    f"(shared model weights with primary)"
                )
                await run_in_threadpool(self._warm_up_instance, inst, i)
                self._all_instances.append(inst)

            self._pool_queue = asyncio.Queue(maxsize=self._pool_size)
            for inst in self._all_instances:
                self._pool_queue.put_nowait(inst)

            log.info(
                f"✅ Llama pool ready ({len(self._all_instances)} instances, "
                f"shared_model=True, hybrid={self._is_hybrid})"
            )

        # Signal that the backend is fully ready for inference.
        self._ready_event.set()

    async def shutdown(self) -> None:
        """Clean up all instances and saved state.

        Shared instances' contexts are freed first, then the primary
        instance (which owns the model weights) is closed last to
        avoid use-after-free.
        """
        await self._cancel_scaler_task()

        pool_size = len(self._all_instances)

        # Free shared instances' contexts (not the model).
        for inst in reversed(self._all_instances):
            if inst is not self._primary_instance:
                self._close_shared_context(inst)

        # Free the primary instance (model + context).
        if self._primary_instance is not None:
            try:
                self._primary_instance.close()
            except Exception as exc:
                log.warning(f"⚠️ Error closing primary instance: {exc}")

        self._all_instances.clear()
        self._primary_instance = None
        self._pool_queue = None
        self._static_state = None

        log.info(f"✅ Llama backend shutdown complete ({pool_size} instances cleared)")

    async def acquire_instance(self) -> Any:
        """Acquire a Llama instance from the pool (async-safe).

        Enforces two gates before handing out an instance:

        1. **Readiness gate** — blocks until ``initialize()`` has
           finished (startup warm-up complete).
        2. **Scaling gate** — blocks while a JIT scale-up or
           scale-down is in progress, preventing GPU contention
           between warm-up ``load_state()`` calls and inference
           generation on the same Metal/GPU device.

        In **JIT** mode, when the queue is empty and more instances
        can be created, a **batch scale-up** is triggered: the gate
        closes, in-flight generations are drained, then ALL remaining
        instances up to ``jit_limit`` are spawned and warmed at once
        with exclusive GPU access.  Only after every instance is ready
        does the gate reopen and all waiting requests proceed.

        The saved static-context state is always restored before the
        instance is returned.
        """
        timeout = self.config.app.backend_timeout

        # ── Readiness gate ──────────────────────────────────────────
        if not self._ready_event.is_set():
            log.info("⏳ Waiting for backend readiness gate…")
            try:
                await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Backend did not become ready within {timeout}s — "
                    "initialize() may have failed"
                )

        # ── Scaling gate ────────────────────────────────────────────
        if not self._scaling_gate.is_set():
            log.debug("⏳ Waiting for scaling gate…")
            try:
                await asyncio.wait_for(self._scaling_gate.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Scaling operation did not complete within {timeout}s"
                )

        if self._pool_queue is None:
            raise RuntimeError("Backend not initialized")

        inst: Any = None

        # --- Fast path: grab an idle instance if available ----------
        try:
            inst = self._pool_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # --- JIT batch scale-up path --------------------------------
        if inst is None and self._jit_enabled:
            if len(self._all_instances) < self._jit_limit:
                await self._jit_batch_scale_up()
                try:
                    inst = self._pool_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass  # Fall through to slow path

        # --- Slow path: wait for a release ---------------------------
        if inst is None:
            try:
                inst = await asyncio.wait_for(self._pool_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                active = len(self._all_instances)
                raise RuntimeError(
                    "All inference instances are busy — try again later "
                    f"(active={active}, limit={self._jit_limit})"
                )

        # Track in-flight count for drain synchronisation.
        self._in_flight += 1
        self._drain_event.clear()

        # Restore the post-static-tokens state snapshot.
        if self._static_state is not None:
            await run_in_threadpool(inst.load_state, self._static_state)
            log.debug("🔄 Restored static state snapshot before request")

        log.debug(
            "🔧 Acquired instance (idle=%d, total=%d, in_flight=%d)",
            self._pool_queue.qsize(),
            len(self._all_instances),
            self._in_flight,
        )
        return inst

    async def release_instance(self, inst: Any) -> None:
        """Return a used instance back to the pool (async-safe).

        Decrements the in-flight counter and signals the drain event
        when it reaches zero — this unblocks any pending scale-up or
        scale-down operation that is waiting for all generations to
        finish before proceeding with warm-up.
        """
        if self._pool_queue is not None:
            await self._pool_queue.put(inst)

        self._in_flight = max(0, self._in_flight - 1)
        if self._in_flight == 0:
            self._drain_event.set()

        log.debug(
            "🔧 Released instance (idle=%d, total=%d, in_flight=%d)",
            self._pool_queue.qsize() if self._pool_queue else 0,
            len(self._all_instances),
            self._in_flight,
        )

    # ------------------------------------------------------------------
    # JIT scaling operations
    # ------------------------------------------------------------------

    def _close_shared_context(self, inst: Any) -> None:
        """Free the C-level resources of a shared instance.

        Resources are closed in reverse creation order so that
        objects referencing the context are released before the
        context itself is freed.
        """
        try:
            # Sampler resources (may be created during generate() calls)
            for attr in ("_sampler", "_sampling_ctx"):
                obj = getattr(inst, attr, None)
                if obj is not None:
                    with contextlib.suppress(Exception):
                        obj.close()
                    setattr(inst, attr, None)
            # Hybrid checkpoint manager references ctx — clear before
            # closing the context to avoid dangling pointers.
            inst._hybrid_cache_mgr = None
            if inst._ctx is not None:
                inst._ctx.close()
                inst._ctx = None
            if inst._batch is not None:
                inst._batch.close()
                inst._batch = None
        except Exception as exc:
            log.warning(f"⚠️ Error closing shared context: {exc}")

    async def _jit_batch_scale_up(self) -> None:
        """Batch-spawn all remaining instances up to ``jit_limit``.

        Acquires the spawn lock, closes the scaling gate for exclusive
        GPU access, drains in-flight work, then spawns and warms all
        remaining instances before reopening the gate.

        Only the first caller that reaches the ``_spawn_lock`` actually
        performs the scale-up; all others wait on the scaling gate and
        then pick up instances from the queue.
        """
        async with self._spawn_lock:
            # Double-check: another coroutine may have completed
            # scale-up while we waited for the lock.
            if len(self._all_instances) >= self._jit_limit:
                return
            if not self._pool_queue.empty():
                return  # Instances became available

            current = len(self._all_instances)
            target = self._jit_limit

            log.info(
                f"🔒 JIT batch scale-up: spawning " f"{target - current} instance(s)…"
            )

            async with self._scaling_operation():
                # 1. Drain in-flight generations for exclusive GPU access.
                await self._drain_in_flight("scale-up")

                # 2. Collect all idle instances from the queue.
                returned = self._drain_queue()

                # 3. Spawn and warm up new instances.
                spawned: List[Any] = []
                for i in range(current, target):
                    try:
                        new_inst = self._create_shared_instance(self._primary_instance)
                        await run_in_threadpool(self._warm_up_instance, new_inst, i)
                        self._all_instances.append(new_inst)
                        spawned.append(new_inst)
                        log.info(
                            f"📈 JIT batch: warmed instance #{i} "
                            f"({len(self._all_instances)}/{target})"
                        )
                    except Exception as exc:
                        log.warning(f"⚠️ JIT batch spawn failed (slot #{i}): {exc}")
                        break  # Stop trying — OOM or similar

                # 4. Re-queue all instances (returned + newly created).
                self._requeue_instances(returned + spawned)

                # 5. Record scale-up time for cooldown protection.
                self._last_scale_up_time = time.monotonic()

                log.info(
                    f"✅ JIT batch scale-up complete "
                    f"({len(self._all_instances)}/{target} instances, "
                    f"{self._pool_queue.qsize()} idle)"
                )

            # 6. Restart the scaler with a fresh sleep timer so it
            #    counts from NOW, not from when it last woke up.
            await self._restart_scaler_task()

    async def _jit_scale_down(self) -> None:
        """Tear down all shared instances, keeping only the primary.

        Acquires the spawn lock, closes the scaling gate, drains
        in-flight work, destroys shared contexts, then re-warms the
        primary for optimal single-instance GPU performance.
        """
        async with self._spawn_lock:
            log.info("🔒 JIT scale-down: closing gate, " "tearing down idle instances…")

            async with self._scaling_operation():
                # 1. Drain in-flight (safety net for the TOCTOU race
                #    between _scaler_loop pre-checks and lock acquisition).
                if not await self._drain_in_flight("scale-down"):
                    return  # Skip teardown — gate reopens via finally

                # 2. Collect all idle instances from the queue.
                idle = self._drain_queue()

                # 3. Destroy all non-primary instances.
                destroyed = 0
                for inst in idle:
                    if inst is not self._primary_instance:
                        self._close_shared_context(inst)
                        self._all_instances.remove(inst)
                        destroyed += 1

                # 4. Re-warm primary for optimal single-instance speed.
                #    reset() + load_state() gives the Metal allocator a
                #    chance to optimise memory layout after shared
                #    contexts' KV caches are freed.
                if destroyed and self._static_state is not None:
                    await run_in_threadpool(self._rewarm_after_teardown)

                # 5. Re-queue the primary instance.
                self._requeue_instances([self._primary_instance])

                if destroyed:
                    log.info(
                        f"📉 JIT scale-down: destroyed {destroyed} "
                        f"instance(s), re-warmed primary "
                        f"(active={len(self._all_instances)})"
                    )

    async def _scaler_loop(self) -> None:
        """Background task that periodically tears down idle JIT instances.

        Runs every ``_JIT_SCALER_INTERVAL`` seconds.  Each cycle
        evaluates whether a scale-down is appropriate based on pool
        size, in-flight count, and cooldown timer, then delegates
        the actual teardown to ``_jit_scale_down()``.
        """
        try:
            while True:
                await asyncio.sleep(self._JIT_SCALER_INTERVAL)

                if self._pool_queue is None:
                    return

                # Only scale down if we have more than 1 instance.
                if len(self._all_instances) <= 1:
                    continue

                # Skip if all instances are busy — scaling would stall
                # the server for the full generation time.
                if self._in_flight >= len(self._all_instances):
                    continue

                # Cooldown: skip if a batch scale-up completed recently.
                elapsed = time.monotonic() - self._last_scale_up_time
                if elapsed < self._JIT_COOLDOWN:
                    log.debug(
                        f"⏭️ JIT scale-down skipped — cooldown active "
                        f"({elapsed:.0f}s / {self._JIT_COOLDOWN:.0f}s)"
                    )
                    continue

                # Skip if any requests are in-flight — better to check
                # again next cycle than stall active requests.
                if self._in_flight > 0:
                    continue

                await self._jit_scale_down()
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Generation
    # ------------------------------------------------------------------

    @staticmethod
    def _stop_prefix_holdback(text: bytes, stop_bytes: List[bytes]) -> int:
        """Return how many trailing bytes of *text* match a prefix of any
        stop sequence.

        When streaming, we must not yield bytes that could turn out to be
        the beginning of a stop sequence.  This function tells us how many
        bytes at the end of *text* to hold back until subsequent tokens
        either complete the stop sequence (discard) or prove it was a false
        alarm (release).

        For example, with stop sequence ``b"<|im_end|>"``:
            text ending with ``b"<|"``  → holdback = 2
            text ending with ``b"<"``   → holdback = 1
            text ending with ``b"abc"`` → holdback = 0
        """
        if not stop_bytes:
            return 0
        max_holdback = 0
        for sb in stop_bytes:
            # Check suffix lengths from 1..min(len(text), len(sb))
            limit = min(len(text), len(sb))
            for suffix_len in range(1, limit + 1):
                if text[-suffix_len:] == sb[:suffix_len]:
                    if suffix_len > max_holdback:
                        max_holdback = suffix_len
        return max_holdback

    def _build_generate_kwargs(self, temperature: float) -> dict:
        """Build kwargs for the low-level ``Llama.generate()`` method.

        The ``generate()`` API uses different parameter names than
        ``create_completion()`` (e.g. ``temp`` instead of
        ``temperature``).  It also does **not** accept ``max_tokens``,
        ``stream``, or ``stop`` — those are handled by our own loop.
        """
        kwargs: dict[str, Any] = {
            "temp": temperature,
            "top_p": self.config.generation.top_p or 0.95,
            "top_k": self.config.generation.top_k or 40,
            "min_p": self.config.generation.min_p or 0.05,
            "present_penalty": self.config.generation.presence_penalty or 0.0,
            "repeat_penalty": self.config.generation.repeat_penalty or 1.0,
            "reset": False,  # Preserve static state loaded by acquire_instance
        }
        return kwargs

    def generate_sync(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> str:
        """Synchronous text generation (delegates to streaming impl)."""
        parts: List[str] = []
        for chunk in self.generate_stream_sync(
            instance, prompt_tokens, max_tokens, temperature, **kwargs
        ):
            parts.append(chunk)
        return "".join(parts)

    def generate_stream_sync(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> Iterator[str]:
        """Synchronous streaming text generation.

        Uses the low-level ``Llama.generate()`` method with
        ``reset=False`` so the static-context state loaded by
        ``acquire_instance()`` is preserved.  Only the *dynamic*
        portion of the prompt (user message, chat template) is
        evaluated — the static knowledge tokens are already in the
        KV cache from ``load_state()``.

        For short responses (max_tokens <= 16, e.g. grammar-constrained
        menu picks), the entire response is buffered before yielding.
        This prevents partial chat template delimiters (e.g. ``<|im``)
        from leaking into the response due to a race between token
        generation and stop-sequence detection.

        Integrates with the GenerationTracker for:
        - Health polling (token count progress + phase tracking)
        - Thinking stream capture (pre-delimiter content)
        - Diagnostics (eval duration, generation speed)
        """
        import llama_cpp
        from core.generation_tracker import get_tracker

        tracker = get_tracker()

        # Split prompt into static (already in KV cache) and dynamic parts
        n_static = self._static_state.n_tokens if self._static_state else 0
        if n_static > len(prompt_tokens):
            n_static = 0  # safety fallback
        dynamic_tokens = list(prompt_tokens[n_static:])

        # Context-window guard
        total_ctx_used = n_static + len(dynamic_tokens)
        if total_ctx_used >= instance._n_ctx:
            tracker.finish()
            raise ValueError(
                f"Prompt ({total_ctx_used} tokens) exceeds context "
                f"window ({instance._n_ctx})"
            )
        remaining_ctx = instance._n_ctx - total_ctx_used
        effective_max = min(max_tokens, remaining_ctx)

        # For short responses (grammar-constrained menus, single-letter
        # picks), buffer the entire output to prevent partial delimiter
        # tokens from leaking.  The stop-sequence holdback mechanism
        # works per-yield, but when max_tokens is tiny the model often
        # produces the answer token immediately followed by a chat
        # template closer (e.g. <|im_end|>).  Buffering ensures we
        # only yield after generation is fully complete and all stop
        # sequences are cleanly stripped.
        buffer_mode = effective_max <= 16

        # Start tracker with full diagnostic context
        tracker.start(
            request_id=kwargs.get("request_id", ""),
            prompt_tokens=len(prompt_tokens),
        )
        log.info(
            "🔧 generate_stream_sync: dynamic=%d tok, static=%d tok, "
            "total_ctx=%d/%d, max_gen=%d%s",
            len(dynamic_tokens),
            n_static,
            total_ctx_used,
            instance._n_ctx,
            effective_max,
            " (buffered)" if buffer_mode else "",
        )

        gen_kwargs = self._build_generate_kwargs(temperature)
        from formats.registry import get_renderer
        stop_texts = get_renderer(self.config.model.family).stop_tokens()
        stop_bytes = [s.encode("utf-8") for s in stop_texts]

        completion_tokens: List[int] = []
        returned_bytes = 0
        is_first_token = True

        try:
            for token in instance.generate(dynamic_tokens, **gen_kwargs):
                # End-of-generation token check
                if llama_cpp.llama_token_is_eog(instance._model.vocab, token):
                    log.debug(
                        "🔧 EOG token received after %d tokens", len(completion_tokens)
                    )
                    break

                # Track first token (eval → generating transition)
                if is_first_token:
                    tracker.mark_first_token()
                    is_first_token = False

                completion_tokens.append(token)
                tracker.record_token()

                # Detokenize the full completion so far (context-aware)
                all_text: bytes = instance.detokenize(
                    completion_tokens, prev_tokens=prompt_tokens
                )

                # Stop-sequence detection — break the generation loop
                # when the model emits a stop sequence as text tokens
                # (rather than a native EOG token). The stop text is
                # NOT stripped from the output — it remains in the
                # yielded text so that downstream consumers (CRF,
                # training corpus) see the complete model output.
                should_stop = any(sb in all_text for sb in stop_bytes)

                # Yield only the *new* bytes that form valid UTF-8.
                # In buffer_mode, skip per-token yields entirely — we
                # flush everything after the generation loop completes.
                if not buffer_mode:
                    if len(all_text) > returned_bytes:
                        new_bytes = all_text[returned_bytes:]
                        try:
                            yield new_bytes.decode("utf-8")
                            returned_bytes = len(all_text)
                        except UnicodeDecodeError:
                            pass  # incomplete multi-byte char — wait for next token

                if should_stop or len(completion_tokens) >= effective_max:
                    break

            # Flush any remaining bytes (e.g. final multi-byte character
            # or buffered-mode content).
            if completion_tokens:
                final_text: bytes = instance.detokenize(
                    completion_tokens, prev_tokens=prompt_tokens
                )
                if len(final_text) > returned_bytes:
                    yield final_text[returned_bytes:].decode("utf-8", errors="replace")
        finally:
            tracker.finish()

    async def generate_async(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> str:
        """Asynchronous text generation (runs sync in thread pool)."""
        return await run_in_threadpool(
            self.generate_sync,
            instance,
            prompt_tokens,
            max_tokens,
            temperature,
            **kwargs,
        )

    async def generate_stream_async(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """Asynchronous streaming text generation."""
        async for chunk in iterate_in_threadpool(
            self.generate_stream_sync(
                instance, prompt_tokens, max_tokens, temperature, **kwargs
            )
        ):
            yield chunk

    # ------------------------------------------------------------------
    # Health & tokenization
    # ------------------------------------------------------------------

    def get_health_status(self) -> dict:
        """Get backend health status.

        Reports ``"initializing"`` before startup completes,
        ``"scaling"`` during JIT scale-up/down, and ``"ok"``
        when fully operational.
        """
        ready = self._ready_event.is_set()
        scaling = not self._scaling_gate.is_set()
        available = self._pool_queue.qsize() if self._pool_queue else 0
        active = len(self._all_instances)

        if not ready:
            status = "initializing"
        elif scaling:
            status = "scaling"
        else:
            status = "ok"

        info = {
            "status": status,
            "pool_size": self._pool_size,
            "available_instances": available,
            "active_instances": active,
            "in_flight": self._in_flight,
            "shared_model": True,
            "hybrid_model": self._is_hybrid,
            "jit_enabled": self._jit_enabled,
        }
        if self._jit_enabled:
            info["jit_limit"] = self._jit_limit
        if self._static_state is not None:
            info["static_state_tokens"] = self._static_state.n_tokens
            info["static_state_bytes"] = self._static_state.llama_state_size
        return info

    def tokenize(self, text: str) -> List[int]:
        """Tokenize text using the model's tokenizer."""
        if self._primary_instance is not None:
            return self._primary_instance.tokenize(text.encode("utf-8"), add_bos=False)

        # Fallback: create temporary instance just for tokenization
        temp_inst = self._create_primary_instance()
        result = temp_inst.tokenize(text.encode("utf-8"), add_bos=False)
        del temp_inst
        return result

    def detokenize(self, tokens: List[int]) -> str:
        """Convert tokens back to text."""
        if self._primary_instance is not None:
            return self._primary_instance.detokenize(tokens)

        # Fallback: create temporary instance just for detokenization
        temp_inst = self._create_primary_instance()
        result = temp_inst.detokenize(tokens)
        del temp_inst
        return result
