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
import dataclasses
import logging
import time
from typing import Any, AsyncGenerator, Iterator, List, Optional

import numpy as np

from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .base import BaseBackend, BackendCapabilities
from inference.repetition import DegenerateGenerationError, RepetitionGuard

log = logging.getLogger("llm-mvp")


@dataclasses.dataclass
class InstanceMeta:
    """Per-instance lifecycle metadata for LRU reaping and health output."""

    created_at: float
    last_released_at: float
    warmup_seconds: float = 0.0
    is_primary: bool = False


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
    # 60s (was 300): with one-per-tick LRU reaping gated on
    # instance_idle_ttl, decay is gradual — one instance per minute,
    # starting only after the idle TTL — instead of all-at-once.
    _JIT_SCALER_INTERVAL: float = 60.0

    # Minimum seconds after a scale-up before the scaler may tear down.
    _JIT_COOLDOWN: float = 300.0

    # After a scale-up aborts on drain timeout, suppress re-triggering
    # for this long so acquirers fall back to the slow path instead of
    # hammering the unsafe spawn.
    _SCALE_UP_RETRY_BACKOFF: float = 30.0

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
        # Generation accounting: _active_generations counts GPU work
        # units (generate/eval/load_state/save_state spans inside
        # generation_guard). Scaling drains wait on THIS, so a pinned
        # but idle session never blocks a scale-up. _checked_out counts
        # pool membership only (health/logging) — a session holds a
        # checkout for its whole life, which must not look like GPU work.
        self._active_generations: int = 0
        self._checked_out: int = 0
        self._drain_event: asyncio.Event = asyncio.Event()
        self._drain_event.set()  # Initially "drained" (no generations)
        # Cooldown tracking — prevents scaler from destroying freshly
        # created instances.
        self._last_scale_up_time: float = 0.0
        # Retry backoff after a scale-up aborted on drain timeout.
        self._scale_up_backoff_until: float = 0.0
        # Per-instance lifecycle metadata, keyed by id(instance).
        self._instance_meta: dict[int, InstanceMeta] = {}
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

        from llama_cpp import internals

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

            started = time.perf_counter()

            if self._static_state is not None:
                # State already computed by the first instance — just load it.
                llm_inst.load_state(self._static_state)
                log.info(
                    f"✅ Loaded pre-computed static state for pool slot #{idx} "
                    f"in {time.perf_counter() - started:.2f}s"
                )
                return

            n_tokens = len(static_tokens)

            if n_tokens == 0:
                # No static tokens (--skip-knowledge) — save a clean
                # initial state without any system prompt prefix.
                llm_inst.reset()
                self._static_state = llm_inst.save_state()
                log.info(
                    f"📝 No static tokens — saved clean initial state "
                    f"(pool slot #{idx})"
                )
                return

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
                f"{state_mb:,.1f} MiB C-level state, static eval took "
                f"{time.perf_counter() - started:.2f}s (pool slot #{idx})"
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
        started = time.perf_counter()
        self._primary_instance.reset()
        self._primary_instance.load_state(self._static_state)
        log.info(
            "🔄 Re-warmed primary instance after scale-down in %.2fs",
            time.perf_counter() - started,
        )

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
        """Wait for all in-flight GENERATIONS (GPU work) to complete.

        Pinned-but-idle checkouts (sessions between turns) do not count
        — only active generation_guard spans hold the drain. Returns
        ``True`` if all generations completed within the configured
        ``backend_timeout``, ``False`` on timeout.
        """
        if self._active_generations <= 0:
            return True

        timeout = self.config.app.backend_timeout
        log.info(
            f"⏳ Waiting for {self._active_generations} in-flight "
            f"generation(s) to complete before {operation}…"
        )
        started = time.perf_counter()
        try:
            await asyncio.wait_for(self._drain_event.wait(), timeout=timeout)
            log.info(
                "✅ Drained generations in %.2fs before %s",
                time.perf_counter() - started,
                operation,
            )
            return True
        except asyncio.TimeoutError:
            log.warning(
                "⚠️ Drain timed out after %ds during %s "
                "(active_generations=%d, checked_out=%d)",
                timeout,
                operation,
                self._active_generations,
                self._checked_out,
            )
            return False

    def _requeue_instances(self, instances: List[Any]) -> None:
        """Put instances back into the pool queue."""
        for inst in instances:
            self._pool_queue.put_nowait(inst)

    def _register_instance(
        self, inst: Any, is_primary: bool = False, warmup_seconds: float = 0.0
    ) -> None:
        """Record lifecycle metadata for a pool instance (LRU + health)."""
        now = time.monotonic()
        self._instance_meta[id(inst)] = InstanceMeta(
            created_at=now,
            last_released_at=now,
            warmup_seconds=warmup_seconds,
            is_primary=is_primary,
        )

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

        Prevents ``acquire_instance()`` from handing out instances and
        ``generation_guard()`` from starting new GPU work while a JIT
        scale-up or scale-down is modifying the pool.
        A permanently-closed gate deadlocks the server, so the
        ``finally`` block guarantees reopening even on cancellation.
        """
        self._scaling_gate.clear()
        try:
            yield
        finally:
            self._scaling_gate.set()

    async def _gate_generation(self) -> None:
        """Wait for the readiness + scaling gates before starting GPU work.

        Uses ``scale_wait_timeout`` (not ``backend_timeout``): a waiter
        must outlive a full scale-up (drain + N warm-ups), so its budget
        is deliberately larger than the drain budget inside the scaling
        operation it is waiting on.
        """
        timeout = getattr(self.config.resources, "scale_wait_timeout", 600)
        if not self._ready_event.is_set():
            try:
                await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Backend did not become ready within {timeout}s "
                    "(scale_wait_timeout) — initialize() may have failed"
                )
        if not self._scaling_gate.is_set():
            try:
                await asyncio.wait_for(self._scaling_gate.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"Scaling operation did not complete within {timeout}s "
                    "(scale_wait_timeout)"
                )

    @contextlib.asynccontextmanager
    async def generation_guard(self, *, nested: bool = False):
        """Mark a span of GPU work (generate, eval, load_state, save_state).

        The single mechanism enforcing the pool's core invariant: no GPU
        work concurrent with a scaling operation's spawn/warm-up/teardown.

        Outer entry (``nested=False``) waits the gates, then increments
        ``_active_generations`` with a post-increment gate re-check — if a
        scaling op closed the gate between our wait and the increment, we
        back out and re-wait. This is race-free because
        ``_scaling_operation`` closes the gate BEFORE its drain reads the
        counter, and both sides run on the event loop.

        Nested entry (``nested=True`` — the generate wrappers when called
        from inside ``session_turn``'s own guard, signalled via the
        ``_nested_guard`` kwarg) increments WITHOUT waiting on the scaling
        gate: the enclosing guard already holds the drain hostage, so
        waiting would deadlock against the scaler. Nested entries only
        balance the counter.

        Nesting is an explicit flag rather than contextvar depth-tracking
        because async-generator steps execute in the CALLER's context — a
        stream iterated from more than one task (slow clients, handler
        handoffs) would corrupt contextvar tokens.
        """
        if not nested:
            while True:
                await self._gate_generation()
                self._active_generations += 1
                self._drain_event.clear()
                if self._scaling_gate.is_set():
                    break  # Safe: any drain will now wait for us.
                # Lost the race — a scaling op closed the gate after
                # our wait. Back out and re-wait.
                self._active_generations -= 1
                if self._active_generations == 0:
                    self._drain_event.set()
        else:
            self._active_generations += 1
            self._drain_event.clear()
        try:
            yield
        finally:
            self._active_generations = max(0, self._active_generations - 1)
            if self._active_generations == 0:
                self._drain_event.set()

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
        self._register_instance(self._primary_instance, is_primary=True)

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
                ctx_started = time.perf_counter()
                inst = self._create_shared_instance(self._primary_instance)
                log.info(
                    f"🔗 Created shared context for pool slot #{i} "
                    f"in {time.perf_counter() - ctx_started:.2f}s "
                    f"(shared model weights with primary)"
                )
                await run_in_threadpool(self._warm_up_instance, inst, i)
                self._all_instances.append(inst)
                self._register_instance(inst)

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
        self._instance_meta.clear()
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

        # ── Readiness + scaling gates ───────────────────────────────
        # Gate waits use scale_wait_timeout: a waiter must outlive a
        # full scale-up (drain + N warm-ups), so its budget is larger
        # than the backend_timeout that bounds the drain inside the
        # scaling operation it is waiting on.
        await self._gate_generation()

        if self._pool_queue is None:
            raise RuntimeError("Backend not initialized")

        inst: Any = None

        # --- Fast path: grab an idle instance if available ----------
        try:
            inst = self._pool_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

        # --- JIT batch scale-up path --------------------------------
        # Skipped during the retry backoff after an aborted scale-up
        # (drain timeout) — callers fall through to the slow path and
        # wait for a release instead of re-triggering the unsafe spawn.
        if inst is None and self._jit_enabled:
            if (
                len(self._all_instances) < self._jit_limit
                and time.monotonic() >= self._scale_up_backoff_until
            ):
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

        # Pool-membership accounting only. GPU-busy tracking lives in
        # generation_guard — a checkout (e.g. a session pinned between
        # turns) is not GPU work and must not block scaling drains.
        self._checked_out += 1

        # Restore the post-static-tokens state snapshot. This is a
        # multi-GB GPU memcpy — guard it like any other GPU work.
        if self._static_state is not None:
            async with self.generation_guard():
                await run_in_threadpool(inst.load_state, self._static_state)
            log.debug("🔄 Restored static state snapshot before request")

        log.debug(
            "🔧 Acquired instance (idle=%d, total=%d, checked_out=%d)",
            self._pool_queue.qsize(),
            len(self._all_instances),
            self._checked_out,
        )
        return inst

    async def release_instance(self, inst: Any) -> None:
        """Return a used instance back to the pool (async-safe).

        Decrements the checkout counter and stamps the instance's
        last-released time for LRU reaping. Drain synchronisation is
        handled by generation_guard, not here.
        """
        if self._pool_queue is not None:
            await self._pool_queue.put(inst)

        self._checked_out = max(0, self._checked_out - 1)
        meta = self._instance_meta.get(id(inst))
        if meta is not None:
            meta.last_released_at = time.monotonic()

        log.debug(
            "🔧 Released instance (idle=%d, total=%d, checked_out=%d)",
            self._pool_queue.qsize() if self._pool_queue else 0,
            len(self._all_instances),
            self._checked_out,
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

            scale_started = time.perf_counter()
            async with self._scaling_operation():
                # 1. Drain in-flight generations for exclusive GPU access.
                #    On timeout, ABORT: spawning contexts and running
                #    warm-up evals concurrently with an active generation
                #    on the same Metal device crashes ggml (assert/segv,
                #    the "non-OOM crash" class). The drain runs BEFORE
                #    _drain_queue, so there is nothing to requeue — just
                #    set the retry backoff and let acquirers fall through
                #    to the slow path until it expires.
                if not await self._drain_in_flight("scale-up"):
                    self._scale_up_backoff_until = (
                        time.monotonic() + self._SCALE_UP_RETRY_BACKOFF
                    )
                    log.warning(
                        "⚠️ JIT scale-up ABORTED — drain timeout; retry "
                        "suppressed for %.0fs",
                        self._SCALE_UP_RETRY_BACKOFF,
                    )
                    return  # Gate reopens via _scaling_operation's finally.

                # 2. Collect all idle instances from the queue.
                returned = self._drain_queue()

                # 3. Spawn and warm up new instances.
                spawned: List[Any] = []
                for i in range(current, target):
                    try:
                        ctx_started = time.perf_counter()
                        new_inst = self._create_shared_instance(self._primary_instance)
                        ctx_seconds = time.perf_counter() - ctx_started
                        warm_started = time.perf_counter()
                        await run_in_threadpool(self._warm_up_instance, new_inst, i)
                        warm_seconds = time.perf_counter() - warm_started
                        self._all_instances.append(new_inst)
                        self._register_instance(new_inst, warmup_seconds=warm_seconds)
                        spawned.append(new_inst)
                        log.info(
                            f"📈 JIT batch: instance #{i} context "
                            f"{ctx_seconds:.2f}s + warm-up {warm_seconds:.2f}s "
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
                    f"✅ JIT batch scale-up complete in "
                    f"{time.perf_counter() - scale_started:.2f}s "
                    f"({len(self._all_instances)}/{target} instances, "
                    f"{self._pool_queue.qsize()} idle)"
                )

            # 6. Restart the scaler with a fresh sleep timer so it
            #    counts from NOW, not from when it last woke up.
            await self._restart_scaler_task()

    def _pick_lru_victim(self, idle: List[Any]) -> Optional[Any]:
        """Pick the least-recently-used reapable instance, or None.

        Reapable = non-primary, idle longer than ``instance_idle_ttl``.
        Pure and synchronous for direct unit testing.
        """
        idle_ttl = getattr(self.config.resources, "instance_idle_ttl", 600)
        now = time.monotonic()
        victim: Any = None
        victim_released = float("inf")
        for inst in idle:
            if inst is self._primary_instance:
                continue
            meta = self._instance_meta.get(id(inst))
            released = meta.last_released_at if meta else 0.0
            if now - released < idle_ttl:
                continue
            if released < victim_released:
                victim = inst
                victim_released = released
        return victim

    async def _jit_reap_lru_one(self) -> bool:
        """Reap the single least-recently-used idle non-primary instance.

        Replaces the old all-at-once scale-down (which caused spawn-all →
        reap-all thrash under bursty load): at most ONE instance per
        scaler tick, and only if it has been idle longer than
        ``instance_idle_ttl``. The primary re-warm (Metal allocator
        layout recovery) runs only when the pool returns to a single
        instance. Returns True if an instance was reaped.
        """
        async with self._spawn_lock:
            async with self._scaling_operation():
                # Drain in-flight generations (safety net for the TOCTOU
                # race between _scaler_tick pre-checks and lock acquisition).
                if not await self._drain_in_flight("scale-down"):
                    return False  # Skip teardown — gate reopens via finally

                idle = self._drain_queue()
                victim = self._pick_lru_victim(idle)
                if victim is None:
                    self._requeue_instances(idle)
                    return False

                idle.remove(victim)
                self._close_shared_context(victim)
                self._all_instances.remove(victim)
                self._instance_meta.pop(id(victim), None)

                if len(self._all_instances) == 1 and self._static_state is not None:
                    await run_in_threadpool(self._rewarm_after_teardown)

                self._requeue_instances(idle)

                log.info(
                    f"📉 JIT reap: destroyed 1 LRU instance "
                    f"(active={len(self._all_instances)})"
                )
                return True

    async def _scaler_tick(self) -> None:
        """One scaler decision cycle — extracted so tests can drive
        ticks directly without sleeping through the interval."""
        if self._pool_queue is None:
            return

        # Only scale down if we have more than 1 instance.
        if len(self._all_instances) <= 1:
            return

        # Skip if every instance is checked out — nothing is reapable
        # (checked-out instances are not in the queue).
        if self._checked_out >= len(self._all_instances):
            return

        # Cooldown: skip if a batch scale-up completed recently.
        cooldown = getattr(
            self.config.resources, "scale_down_cooldown", self._JIT_COOLDOWN
        )
        elapsed = time.monotonic() - self._last_scale_up_time
        if elapsed < cooldown:
            log.debug(
                f"⏭️ JIT scale-down skipped — cooldown active "
                f"({elapsed:.0f}s / {cooldown:.0f}s)"
            )
            return

        # Skip if any generations are in-flight — better to check
        # again next cycle than stall active requests.
        if self._active_generations > 0:
            return

        await self._jit_reap_lru_one()

    async def _scaler_loop(self) -> None:
        """Background task that periodically reaps idle JIT instances.

        Runs every ``_JIT_SCALER_INTERVAL`` seconds; each cycle reaps at
        most one LRU instance idle beyond ``instance_idle_ttl`` (see
        ``_scaler_tick`` / ``_jit_reap_lru_one``).
        """
        try:
            while True:
                await asyncio.sleep(self._JIT_SCALER_INTERVAL)
                await self._scaler_tick()
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
        stop_texts: List[str] | None = None,
        **kwargs,
    ) -> Iterator[str]:
        """Synchronous streaming text generation.

        Args:
            instance: Pre-acquired backend instance.
            prompt_tokens: Tokenized prompt (static + dynamic).
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.
            stop_texts: Optional override for stop-sequence detection.
                When None (default), uses ``renderer.stop_tokens()`` in
                completion mode. Session callers should pass
                ``renderer.stop_tokens(mode="session")`` to add the
                second-assistant-turn opener as a stop, which prevents
                multi-turn rambling.

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

        # Split prompt into static (already in KV cache) and dynamic parts.
        #
        # COMPLETION path: prompt_tokens = [static prefix] + [dynamic], so we
        # skip the prefix (already evaluated via load_state).
        # SESSION path: prompt_tokens are PURELY incremental (the restored KV
        # already holds static prefix + prior turns) — callers pass
        # static_in_prompt=False so nothing is skipped. Before this flag, any
        # session turn LONGER than the static prefix had its first n_static
        # tokens silently amputated (turns shorter than the prefix were saved
        # by the n_static > len fallback, which hid the bug): the model saw
        # only the prompt tail and produced blind rewrites/refusals
        # (45f031ac run, cycle-25 process_command placeholder splice).
        static_in_prompt = kwargs.pop("static_in_prompt", True)
        n_static = (
            self._static_state.n_tokens
            if (self._static_state and static_in_prompt)
            else 0
        )
        if n_static > len(prompt_tokens):
            n_static = 0  # safety fallback
        dynamic_tokens = list(prompt_tokens[n_static:])

        # Context-window guard. For session turns (static_in_prompt=False)
        # the KV base is the instance's current occupancy — static prefix
        # plus all prior turns — not just the static prefix.
        kv_base = n_static
        if not static_in_prompt:
            kv_base = int(getattr(instance, "n_tokens", 0) or 0)
        total_ctx_used = kv_base + len(dynamic_tokens)
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

        # Use caller-provided stops when given (session mode) — otherwise
        # default to completion-mode stops from the renderer.
        if stop_texts is None:
            stop_texts = get_renderer(self.config.model.family).stop_tokens()
        stop_bytes = [s.encode("utf-8") for s in stop_texts]
        # Scan only a bounded tail for stop sequences (incremental detok keeps a
        # cumulative byte accumulator; a freshly-emitted stop is always near the
        # tail). Slack covers a stop split across the last couple of tokens.
        max_stop_len = max((len(sb) for sb in stop_bytes), default=0)
        stop_tail = max_stop_len + 8

        completion_tokens: List[int] = []
        returned_bytes = 0
        is_first_token = True

        # Incremental detokenization (replaces O(n²) full-list detok per token):
        # accumulate bytes and detokenize only the new token, with all preceding
        # tokens as context so llama.cpp emits the correct piece boundary.
        acc_bytes = b""
        prior_tokens: List[int] = list(prompt_tokens)

        # Degenerate-repetition guard (off only if explicitly disabled per-config).
        gen_cfg = self.config.generation
        guard: Optional[RepetitionGuard] = None
        if gen_cfg.repetition_guard_enabled is not False:
            from inference.repetition import (
                DEFAULT_MAX_CYCLE_PERIOD,
                DEFAULT_MAX_RUN,
                DEFAULT_MIN_CYCLE_REPS,
            )

            guard = RepetitionGuard(
                max_run=gen_cfg.repetition_max_run or DEFAULT_MAX_RUN,
                max_cycle_period=(
                    gen_cfg.repetition_max_cycle_period or DEFAULT_MAX_CYCLE_PERIOD
                ),
                min_cycle_reps=gen_cfg.repetition_min_cycle_reps
                or DEFAULT_MIN_CYCLE_REPS,
            )

        # Long-cycle guard + abnormal-exit capture (runaway_capture.py).
        # The RepetitionGuard sees 8-token cycles; paragraph-scale loops
        # sail under it (live: 130k-token menu runaways, text discarded).
        from inference import runaway_capture

        long_cycle_on = gen_cfg.long_cycle_guard_enabled is not False
        capture_dir = getattr(getattr(self.config, "logging", None), "directory", None)
        # Set when this generation ends for a known reason (normal stop,
        # degeneracy, long-cycle). Left None across an abnormal exit —
        # GeneratorExit from an abandoned consumer, i.e. the agent-side
        # watchdog cancelling a runaway — where the finally captures the
        # partial text instead of discarding the evidence.
        gen_end_reason: Optional[str] = None

        # KV position where generation begins: generate() evals dynamic_tokens
        # (reset=False) at [n_tokens, n_tokens+len), then samples from there.
        gen_start_pos = instance.n_tokens + len(dynamic_tokens)

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

                # Degenerate-repetition guard — abort a turn that collapses into
                # token-level repetition before it fills max_tokens (~1h hang).
                if guard is not None:
                    reason = guard.observe(token)
                    if reason:
                        gen_end_reason = f"degenerate: {reason}"
                        runaway_capture.dump_capture(
                            capture_dir or "./logs",
                            gen_end_reason,
                            acc_bytes,
                            len(completion_tokens),
                            meta={
                                "request_id": kwargs.get("request_id", ""),
                                "temperature": temperature,
                            },
                        )
                        raise DegenerateGenerationError(
                            reason, tokens_generated=len(completion_tokens)
                        )

                # Long-cycle guard — paragraph-scale loops the token guard
                # can't see. Structural check on the text tail every
                # CHECK_INTERVAL tokens; abort through the same clean path.
                if (
                    long_cycle_on
                    and len(completion_tokens) % runaway_capture.CHECK_INTERVAL == 0
                ):
                    lc_reason = runaway_capture.detect_long_cycle(acc_bytes)
                    if lc_reason:
                        gen_end_reason = f"long-cycle: {lc_reason}"
                        runaway_capture.dump_capture(
                            capture_dir or "./logs",
                            lc_reason,
                            acc_bytes,
                            len(completion_tokens),
                            meta={
                                "request_id": kwargs.get("request_id", ""),
                                "temperature": temperature,
                            },
                        )
                        raise DegenerateGenerationError(
                            lc_reason, tokens_generated=len(completion_tokens)
                        )

                # Incremental detokenize: only the NEW token, with all prior
                # tokens as context. A detok failure (e.g. llama_cpp "Negative
                # size passed to PyBytes_FromStringAndSize" on a degenerate
                # token) is converted to the same clean abort path.
                try:
                    piece: bytes = instance.detokenize(
                        [token], prev_tokens=prior_tokens
                    )
                except Exception as e:  # noqa: BLE001 — convert to controlled abort
                    raise DegenerateGenerationError(
                        f"detokenization failed: {e}",
                        tokens_generated=len(completion_tokens),
                    ) from e
                prior_tokens.append(token)
                acc_bytes += piece

                # Stop-sequence detection — break the generation loop when the
                # model emits a stop sequence as text tokens (rather than a
                # native EOG token). Scanned over a bounded tail; the stop text
                # is NOT stripped — it stays in the output so downstream
                # consumers (FSM labeller, capture log) see the full output.
                should_stop = any(sb in acc_bytes[-stop_tail:] for sb in stop_bytes)

                # Yield only the *new* bytes that form valid UTF-8. Decode the
                # cumulative tail (never `piece` alone — a token can be a UTF-8
                # continuation fragment). In buffer_mode, skip per-token yields.
                if not buffer_mode:
                    if len(acc_bytes) > returned_bytes:
                        try:
                            yield acc_bytes[returned_bytes:].decode("utf-8")
                            returned_bytes = len(acc_bytes)
                        except UnicodeDecodeError:
                            pass  # incomplete multi-byte char — wait for next token

                if should_stop or len(completion_tokens) >= effective_max:
                    break

            # Flush any remaining bytes (final multi-byte char or buffered-mode
            # content). The accumulator already holds everything.
            gen_end_reason = "completed"
            if acc_bytes and len(acc_bytes) > returned_bytes:
                yield acc_bytes[returned_bytes:].decode("utf-8", errors="replace")

            # Expose the generated token ids + the KV position where generation
            # began, so the session layer can locate the reasoning span for
            # in-place KV compaction (Factor 4 / strip_reasoning).
            instance._last_completion_tokens = list(completion_tokens)
            instance._last_gen_start_pos = gen_start_pos
        finally:
            # Abnormal exit with substantial output and no recorded reason:
            # the consumer abandoned the stream — in practice the agent-side
            # health watchdog cancelling a runaway. Capture the partial text
            # the cancellation used to discard (live: 43 cancellations at up
            # to 130k tokens with zero forensic evidence).
            if (
                gen_end_reason is None
                and len(completion_tokens) >= runaway_capture.CHECK_INTERVAL
            ):
                runaway_capture.dump_capture(
                    capture_dir or "./logs",
                    "abandoned by consumer (watchdog cancel or disconnect)",
                    acc_bytes,
                    len(completion_tokens),
                    meta={
                        "request_id": kwargs.get("request_id", ""),
                        "temperature": temperature,
                    },
                )
            tracker.finish()

    async def generate_async(
        self,
        instance: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        **kwargs,
    ) -> str:
        """Asynchronous text generation (runs sync in thread pool).

        The async wrappers are the only sanctioned serving entry points:
        they hold generation_guard, which enforces the pool invariant
        (no GPU work concurrent with JIT spawn/warm-up/teardown).
        Calling generate_sync/generate_stream_sync directly bypasses it.

        ``_nested_guard=True`` (popped, never forwarded) marks a call made
        from inside an enclosing generation_guard (session_turn) — the
        guard then only balances the counter instead of re-waiting the
        scaling gate, which would deadlock against a draining scaler.
        """
        nested = kwargs.pop("_nested_guard", False)
        async with self.generation_guard(nested=nested):
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
        """Asynchronous streaming text generation.

        Holds generation_guard for the stream's lifetime. If a consumer
        abandons the stream, ``aclose()``/asyncgen finalization resumes
        the generator and the guard's finally releases the counter —
        bounded in the worst case by the drain timeout, which now aborts
        scaling cleanly instead of crashing.

        ``_nested_guard``: see generate_async.
        """
        nested = kwargs.pop("_nested_guard", False)
        async with self.generation_guard(nested=nested):
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

        now = time.monotonic()
        info = {
            "status": status,
            "pool_size": self._pool_size,
            "available_instances": available,
            "active_instances": active,
            # Active GENERATIONS (GPU-busy), not checkouts — a session
            # pinned between turns shows in_flight 0 / checked_out 1.
            "in_flight": self._active_generations,
            "checked_out": self._checked_out,
            "shared_model": True,
            "hybrid_model": self._is_hybrid,
            "jit_enabled": self._jit_enabled,
            "instances": [
                {
                    "age_seconds": round(now - meta.created_at, 1),
                    "idle_seconds": round(now - meta.last_released_at, 1),
                    "warmup_seconds": round(meta.warmup_seconds, 2),
                    "is_primary": meta.is_primary,
                }
                for meta in self._instance_meta.values()
            ],
        }
        if self._jit_enabled:
            info["jit_limit"] = self._jit_limit
        if self._static_state is not None:
            info["static_state_tokens"] = self._static_state.n_tokens
            info["static_state_bytes"] = self._static_state.llama_state_size
        return info

    def strip_reasoning_replay(
        self, instance: Any, t0: int, replay_tokens: List[int]
    ) -> bool:
        """Truncate-and-replay reasoning strip (Factor 4): drop everything in the
        KV from ``t0`` to the end (the turn's reasoning + raw answer), then
        re-eval the canonical answer (``replay_tokens``) at ``t0``.

        Uses only two primitives validated to behave correctly here:
          • ``memory_seq_rm(0, t0, -1)`` — tail removal (leaves [0, t0) intact,
            positions contiguous, pos_min unchanged).
          • ``eval`` — re-adds the clean answer, keeping ``input_ids``/``n_tokens``
            consistent automatically.

        We deliberately avoid ``seq_add``: its large negative position shift
        corrupted the static-prefix positions for big spans (e.g. harmony's
        analysis channel — pos_min jumped by the shift amount). The recompute is
        tiny (just the short answer, not history).

        No-op for hybrid/recurrent models (``memory_can_shift()`` is False, e.g.
        Qwen3.5/Qwen3-Next): their memory is a fixed recurrent state, not a
        removable KV span, so reasoning doesn't accumulate unboundedly and
        per-turn excision doesn't apply (and would corrupt the recurrent state).
        """
        ctx = instance._ctx
        if not ctx.memory_can_shift():
            return False
        nt = instance.n_tokens
        if t0 < 0 or t0 >= nt:
            return False
        ctx.memory_seq_rm(0, t0, -1)  # drop the tail [t0, end)
        instance.n_tokens = t0
        if replay_tokens:
            instance.eval(replay_tokens)  # re-add canonical answer at t0
        return True

    def tokenize(self, text: str, special: bool = False) -> List[int]:
        """Tokenize text using the model's tokenizer.

        ``special`` parses special-token strings as canonical single tokens —
        pass True only for trusted structural framing (see tokenize_segments).
        """
        if self._primary_instance is not None:
            return self._primary_instance.tokenize(
                text.encode("utf-8"), add_bos=False, special=special
            )

        # Fallback: create temporary instance just for tokenization
        temp_inst = self._create_primary_instance()
        result = temp_inst.tokenize(
            text.encode("utf-8"), add_bos=False, special=special
        )
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
