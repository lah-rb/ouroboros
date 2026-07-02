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
from collections import OrderedDict
from typing import Any, AsyncGenerator, Iterator, List, Optional

import numpy as np

from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .base import BaseBackend, BackendCapabilities
from inference.repetition import DegenerateGenerationError, RepetitionGuard

log = logging.getLogger("llm-mvp")

# Reserved seq ids for the resident in-context cache (config.model.resident_seq_cache).
# Each pool context handles ONE working stream, so generation stays on seq 0 (where the
# high-level Llama.generate()/eval() operate) — we reuse the existing generation machinery
# verbatim. SEQ_STATIC holds the pristine static prefix, forked onto SEQ_WORKING per request.
SEQ_WORKING = 0  # the live generation / session seq
SEQ_STATIC = 1   # pristine static-prefix template (fork source); never generated on
SEQ_FLOW_BASE = 2  # Phase 2: per-flow resident prefixes occupy seqs [2, 2+flow_hot_set)
# Generation headroom a snapshot capture must leave free in the shared
# n_ctx cell pool (capacity check in snapshot_working_seq).
SNAP_GEN_RESERVE = 8192
# Session snapshots occupy the band ABOVE the flow band:
# [SEQ_FLOW_BASE + flow_hot_set, + session_snapshot_max). Own allocator —
# snapshots are explicitly purged and capacity-rejected, never LRU-evicted,
# so entangling them with the flow band's LRU would silently shrink it.


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
        # Per-flow static-prefix KV cache (opt-in: config.model.flow_kv_cache).
        # flow_key -> saved LlamaState of [global static + that flow's static
        # head], so later visits restore it and prefill only the dynamic tail.
        # LRU-bounded; only populated when the flag is on. See config.py.
        self._flow_states: "OrderedDict[str, Any]" = OrderedDict()
        # Resident in-context sequence cache (opt-in: config.model.resident_seq_cache).
        # When active, the pristine static prefix lives on SEQ_STATIC and is forked
        # (memory_seq_cp) onto SEQ_WORKING=0 per request instead of load_state'd from a
        # blob. _resident_active is set in initialize() after the can_shift gate (forced
        # off for pure-recurrent models, which lack memory_can_shift()).
        self._resident_requested = bool(
            getattr(getattr(config, "model", None), "resident_seq_cache", False)
        )
        self._resident_active = False
        self._resident_static_len = 0
        self._resident_static_tokens: List[int] = []
        # Phase 2: resident flow-prefix hot-set. When resident + flow_kv_cache, each
        # flow's [global static + flow head] is pinned on a dedicated per-instance
        # seq (band [SEQ_FLOW_BASE, SEQ_FLOW_BASE+flow_hot_set)) and forked onto seq 0
        # per request — no save_state. Per-instance (KV is context-local).
        self._flow_hot_set = int(
            getattr(getattr(config, "model", None), "flow_kv_cache_max", 8) or 8
        )
        self._flow_resident = self._resident_requested and bool(
            getattr(getattr(config, "model", None), "flow_kv_cache", False)
        )
        # Health/degradation counters (cumulative for the server's lifetime),
        # surfaced via get_health_status for long-run monitoring. Flow-cache
        # churn + fallbacks indicate KV-cache pressure/instability; runaway
        # captures indicate generation pathologies. A rising fallback rate on a
        # box under memory pressure is the signature of the Apple-Silicon
        # unified-memory KV/weight eviction failure mode.
        self._h_flow_builds = 0
        self._h_flow_hits = 0
        self._h_flow_evicts = 0
        self._h_flow_fallbacks = 0
        self._h_runaway_captures = 0
        # In-process context-refresh accounting. The vanilla-compare verdict proved
        # the long-run output rot ("souring": the model emits short JSON action-stubs
        # instead of full files) is LLMVP-PROCESS-level — it clears with a fresh
        # llama.cpp context but NOT with a process/machine reboot, and it accumulates
        # by inference VOLUME (~200 forks in observation). _requests_since_refresh
        # drives the periodic refresh; _context_refreshes is the cumulative count.
        self._h_context_refreshes = 0
        self._h_requests_since_refresh = 0
        # Session flow-fork (Phase 2b): a memoryful session forks a pinned flow head
        # onto its live seq at turn 0. Reuses the same flow band as the stateless
        # cache; needs the band allocated even when flow_kv_cache itself is off.
        self._session_flow_fork = self._resident_requested and bool(
            getattr(getattr(config, "model", None), "resident_session_flow_fork", False)
        )
        # The flow seq band (n_seq_max widening + per-instance _flow_seqs allocator)
        # is allocated if EITHER flow consumer is on.
        self._flow_band = self._flow_resident or self._session_flow_fork
        # Semi-permanent session snapshots (config.model.session_snapshot_max).
        # Registry is backend-level (survives session end; refresh demotes hot →
        # cold in ONE place): key -> {dyn_tokens, static_len, turn_count,
        # created_at, resident}. Hot presence is per-instance (inst._snap_seqs).
        self._snapshot_max = int(
            getattr(getattr(config, "model", None), "session_snapshot_max", 2) or 0
        )
        self._snap_registry: "OrderedDict[str, dict]" = OrderedDict()
        self._h_snapshot_rebuilds = 0
        self._h_refresh_deferred = 0
        # JIT pool scaling
        jit_limit = config.resources.jit_concurrency_limit
        self._jit_enabled: bool = jit_limit is not None
        self._jit_limit: int = jit_limit or self._pool_size
        self._spawn_lock: asyncio.Lock = asyncio.Lock()
        self._scaler_task: Optional[asyncio.Task] = None
        # Proactive in-process context refresh (see _refresh_loop). Pre-empts the
        # LLMVP-process rot — BOTH stub-emission AND no-task-confusion thrashing —
        # before it spoils a run; a premature ~0.85s refresh beats a spoiled mission.
        self._refresh_loop_task: Optional[asyncio.Task] = None
        self._refresh_interval = int(
            getattr(getattr(config, "model", None), "context_refresh_interval", 75) or 75
        )
        # Time-based backstop for the refresh loop. The request-count + idle gate alone
        # can starve under CONTINUOUS load: a long mission never goes idle, so the
        # opportunistic refresh waited ~665 requests / 1h40m for a gap and a run soured
        # in the meantime. This wall-clock cap fires the refresh even while busy (the
        # graceful drain just lets the in-flight generation finish — real work, not
        # overhead). Default 30 min.
        self._refresh_seconds = int(
            getattr(getattr(config, "model", None), "context_refresh_seconds", 1800) or 1800
        )
        self._last_refresh_monotonic: Optional[float] = None
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

    @property
    def static_state(self) -> Any:
        """The pristine post-static-tokens snapshot, for whole-state
        restores by the session layer's full-replay policy (the only
        rollback operation recurrent/hybrid models support)."""
        return self._static_state

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

    # Map the human-readable flash_attn_type config to the llama.cpp enum.
    _FLASH_ATTN = {"auto": -1, "off": 0, "on": 1}

    def _make_draft(self) -> Any:
        """A FRESH per-instance n-gram-map speculative draft, or None if disabled.

        The draft (``LlamaNGramMapDecoding``) is STATEFUL — it holds its own n-gram
        index + token history — so it must NOT be shared across pool instances; each
        instance gets its own. It self-resets when the prompt isn't an incremental
        continuation, so the resident-fork and context-refresh paths need NO special
        handling: a stale draft simply rebuilds on the next request, and any wrong
        draft token is rejected by the target during verification (lossless)."""
        if not getattr(self.config.model, "speculative", False):
            return None
        from llama_cpp.llama_speculative import LlamaNGramMapDecoding

        return LlamaNGramMapDecoding(
            ngram_size=int(getattr(self.config.model, "speculative_ngram_size", 3) or 3),
            num_pred_tokens=int(getattr(self.config.model, "speculative_num_pred", 10) or 10),
        )

    def _create_primary_instance(self) -> Any:
        """Create the primary Llama instance that owns the model weights."""
        Llama = self._get_llama_class()

        return Llama(
            model_path=str(self.config.model.path),
            n_ctx=self.config.model.n_ctx,
            n_gpu_layers=self.config.model.n_gpu_layers,
            # flash_attn (bool) was a silent no-op; flash_attn_type is the real param
            # (-1 AUTO / 0 OFF / 1 ON). Default "auto" == the prior effective behavior.
            flash_attn_type=self._FLASH_ATTN.get(
                str(getattr(self.config.model, "flash_attn_type", "auto")).lower(), -1
            ),
            # Retain full KV for SWA layers so save_state/flow_kv_cache is sound on
            # sliding-window models (gpt-oss); kv_unified bounds the memory cost.
            swa_full=bool(getattr(self.config.model, "swa_full", False)),
            kv_unified=bool(getattr(self.config.model, "kv_unified", False)),
            # Resident cache needs SEQ_STATIC alongside the working seq; the flow
            # hot-set adds one resident seq per cached flow prefix; the snapshot
            # band adds one per pinnable session snapshot. Harmless if the
            # can_shift gate later disables the resident path.
            n_seq_max=(
                (
                    2
                    + (self._flow_hot_set if self._flow_band else 0)
                    + self._snapshot_max
                )
                if self._resident_requested
                else 1
            ),
            seed=self.config.model.seed,
            verbose=self.config.model.verbose,
            n_threads=self.config.resources.cpu_threads,
            # batch_size was a silent no-op; n_batch is the real param (the prior
            # effective value was the 2048 default). draft_model wires the binding's
            # native n-gram speculative decoding (gated by config.model.speculative).
            n_batch=int(getattr(self.config.model, "n_batch", 2048) or 2048),
            draft_model=self._make_draft(),
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

        # The shallow copy aliased the primary's speculative draft, which is stateful
        # (per-instance n-gram index/history) and must not be shared — give this
        # instance its own (or None when speculative is off). _logits_all is already
        # copied from the primary and stays consistent (True iff a draft is present).
        inst.draft_model = self._make_draft()

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
            n_tokens = len(static_tokens)

            started = time.perf_counter()

            if self._static_state is not None:
                # State already computed by the first instance — just load it.
                llm_inst.load_state(self._static_state)
                log.info(
                    f"✅ Loaded pre-computed static state for pool slot #{idx} "
                    f"in {time.perf_counter() - started:.2f}s"
                )
            elif n_tokens == 0:
                # No static tokens (--skip-knowledge) — save a clean
                # initial state without any system prompt prefix.
                llm_inst.reset()
                self._static_state = llm_inst.save_state()
                log.info(
                    f"📝 No static tokens — saved clean initial state "
                    f"(pool slot #{idx})"
                )
            else:
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

            # Resident path: seq 0 now holds the pristine static prefix — pin a copy
            # onto SEQ_STATIC so acquire_instance can fork it back onto seq 0 per
            # request (an intra-context copy, never a save_state blob).
            if self._resident_active:
                self._resident_static_len = n_tokens
                self._resident_static_tokens = list(static_tokens)
                if n_tokens > 0:
                    llm_inst._ctx.memory_seq_rm(SEQ_STATIC, 0, -1)
                    llm_inst._ctx.memory_seq_cp(SEQ_WORKING, SEQ_STATIC, -1, -1)
                # Per-instance flow-prefix allocator (Phase 2): flow_key -> seq_id.
                llm_inst._flow_seqs = OrderedDict()
                # Per-instance hot-snapshot presence: key -> seq_id. A fresh/
                # refreshed context starts cold; the registry's token lists
                # rebuild on demand (fork_snapshot_seq miss -> cold rebuild).
                llm_inst._snap_seqs = OrderedDict()
                log.debug(
                    "🧩 Pinned %d static tokens to SEQ_STATIC (pool slot #%d)",
                    n_tokens, idx,
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

    def _refresh_context_sync(self, inst: Any) -> None:
        """Tier-4 in-process context refresh: drop + rebuild the ``llama_context``
        (KEEP the loaded weights), then re-warm.

        The vanilla-compare control proved the long-run output rot is
        LLMVP-PROCESS-level: a fresh llama.cpp context is clean on the identical
        hard prompt while the aged context emits stubs, and a process/machine reboot
        is NOT required. This rebuilds the context struct (fresh KV cells, fresh cell
        metadata, fresh position counters) from the SAME in-memory model, so it is a
        process-restart equivalent minus the multi-GB weight reload (~50-200ms).

        Re-warm reuses ``_warm_up_instance``: it reloads the pristine ``_static_state``
        snapshot (a one-time, CPU-resident blob immune to the runtime KV churn that
        rots the live context) into the fresh context and re-pins SEQ_STATIC, the
        resident fork source. MUST run with the scaling gate closed and generations
        drained — see ``refresh_context`` — so nothing is mid-decode on this context.
        """
        from llama_cpp import internals

        started = time.perf_counter()
        # Drop the old context + batch — frees the accumulated/rotted KV state.
        with contextlib.suppress(Exception):
            if inst._ctx is not None:
                inst._ctx.close()
        with contextlib.suppress(Exception):
            if inst._batch is not None:
                inst._batch.close()
        # Fresh context + batch from the SAME model (weights stay resident, no reload).
        inst._ctx = internals.LlamaContext(
            model=inst._model, params=inst.context_params, verbose=False
        )
        inst._batch = internals.LlamaBatch(
            n_tokens=inst.n_batch,
            embd=0,
            n_seq_max=inst.context_params.n_seq_max,
            verbose=False,
        )
        # Reset per-instance mutable arrays + sampler (mirrors _create_shared_instance).
        inst.input_ids = np.ndarray((inst._n_ctx,), dtype=np.intc)
        logits_rows = inst._n_ctx if inst._logits_all else inst.n_batch
        inst.scores = np.ndarray((logits_rows, inst._n_vocab), dtype=np.single)
        inst._candidates = internals.LlamaTokenDataArray(n_vocab=inst._n_vocab)
        inst.n_tokens = 0
        inst._mirostat_mu = ctypes.c_float(2.0 * 5.0)
        inst._sampler = None
        inst._sampling_ctx = None
        inst.cache = None
        if getattr(inst, "_hybrid_cache_mgr", None) is not None:
            from llama_cpp.llama import HybridCheckpointCache

            inst._hybrid_cache_mgr = HybridCheckpointCache(
                inst._ctx.ctx,
                max_checkpoints=getattr(inst, "ctx_checkpoints", 16),
                verbose=False,
            )
        # Session snapshots: the fresh context has no pinned seqs — demote every
        # hot snapshot to cold, LOUDLY. The registry (CPU-side token lists) is
        # untouched, so from_snapshot still works; the next fork re-prefills.
        demoted = len(getattr(inst, "_snap_seqs", {}) or {})
        if demoted:
            log.warning(
                "🧊 context refresh demoted %d session snapshot(s) to cold — "
                "next fork re-prefills",
                demoted,
            )
        # Re-warm into the fresh context: reload the pristine static snapshot + re-pin
        # SEQ_STATIC. _static_state is already computed, so this is a load, not a re-eval.
        self._warm_up_instance(inst, 0)
        log.info(
            "🧼 In-process context refresh complete in %.2fs "
            "(rebuild + re-warm, weights kept)",
            time.perf_counter() - started,
        )

    async def refresh_context(self, reason: str = "manual") -> dict:
        """Drop + rebuild every instance's ``llama_context`` in-process to clear the
        LLMVP-process-level output rot WITHOUT a process restart or reboot.

        Runs inside ``_scaling_operation`` (closes the scaling gate so no new GPU work
        or checkout starts) and after ``_drain_in_flight`` (waits for in-flight
        generations to finish) — the same exclusive-access envelope JIT teardown uses,
        so we never free a context that is mid-decode. Resets the refresh counter.
        """
        if self._primary_instance is None:
            return {"refreshed": 0, "reason": reason, "status": "not_initialized"}
        started = time.perf_counter()
        async with self._scaling_operation():
            if not await self._drain_in_flight("context refresh"):
                log.warning("⚠️ Context refresh aborted — generation drain timed out")
                return {"refreshed": 0, "reason": reason, "status": "drain_timeout"}
            targets = list(self._all_instances)
            for inst in targets:
                await run_in_threadpool(self._refresh_context_sync, inst)
            self._h_context_refreshes += 1
            self._h_requests_since_refresh = 0
            self._last_refresh_monotonic = time.monotonic()  # resets the time-based cap
        elapsed = time.perf_counter() - started
        log.info(
            "✅ In-process context refresh #%d: %d context(s) in %.2fs (reason=%s)",
            self._h_context_refreshes, len(targets), elapsed, reason,
        )
        return {
            "refreshed": len(targets),
            "reason": reason,
            "status": "ok",
            "elapsed_s": round(elapsed, 3),
            "total_refreshes": self._h_context_refreshes,
        }

    async def _refresh_loop(self) -> None:
        """Proactively refresh the context to pre-empt the LLMVP-process rot (both
        stub-emission souring and the no-task-confusion thrashing that drove missions
        to 0 goals) BEFORE it spoils a run — a premature ~0.85s refresh beats a spoiled
        mission. Two triggers:

          (a) OPPORTUNISTIC — idle (no pinned session, no generation in flight) AND
              `requests_since_refresh >= interval`. Cheap (~0.85s, no drain) and lands
              at a natural session/mission gap. Preferred when it can fire.
          (b) TIME-BASED CAP — `>= refresh_seconds` since the last refresh, fired even
              while BUSY. This is the fix for continuous load: a long mission never goes
              idle, so (a) alone starved (~665 requests / 1h40m before a gap) and a run
              soured. refresh_context drains in-flight first, so the only "cost" is the
              current generation finishing normally (real work) + the ~0.85s rebuild.

        refresh_context closes the scaling gate + drains, so nothing sneaks in
        mid-rebuild either way.
        """
        try:
            self._last_refresh_monotonic = time.monotonic()
            while True:
                await asyncio.sleep(15)
                if self._primary_instance is None:
                    continue
                reason = self._refresh_decision()
                if reason:
                    log.info(
                        "🧼 proactive refresh (%s): %d req since last "
                        "(interval %d), cap %ds — rebuilding context",
                        reason, self._h_requests_since_refresh,
                        self._refresh_interval, self._refresh_seconds,
                    )
                    await self.refresh_context(reason=reason)
        except asyncio.CancelledError:
            return

    def _refresh_decision(self) -> Optional[str]:
        """Should the proactive refresh fire now? None = no.

        BOTH triggers require no instance checked out: a pinned session
        between turns would have its context rebuilt to static-only under
        it, silently vanishing every prior turn (refresh_context drains
        generations, not sessions — the original time-capped branch fired
        through pinned sessions). Deferral is bounded in practice: sessions
        end within minutes and the loop re-checks every 15s; the deferred
        counter makes starvation visible in health.
        """
        idle = self._checked_out == 0 and self._active_generations == 0
        since = self._h_requests_since_refresh
        elapsed = time.monotonic() - (self._last_refresh_monotonic or 0.0)
        if idle and since >= self._refresh_interval:
            return "proactive-interval"
        if since > 0 and elapsed >= self._refresh_seconds:
            if self._checked_out > 0:
                self._h_refresh_deferred += 1
                return None
            if self._active_generations == 0:
                return "proactive-timed"
        return None

    def _resident_restore_static(self, inst: Any) -> None:
        """Restore seq 0 to the pristine static prefix by forking SEQ_STATIC onto it
        (clearing seq 0 first) — the resident replacement for load_state(_static_state),
        a cheap intra-context copy rather than a multi-GB blob restore. Syncs the
        high-level n_tokens / input_ids counters so the existing eval/generate path
        (which operates on seq 0) places the dynamic prompt at the correct position.
        SEQ_STATIC is left intact for the next fork."""
        ctx = inst._ctx
        ctx.memory_seq_rm(SEQ_WORKING, 0, -1)  # clear the working seq
        if self._resident_static_len > 0:
            ctx.memory_seq_cp(SEQ_STATIC, SEQ_WORKING, -1, -1)
            inst.input_ids[: self._resident_static_len] = np.array(
                self._resident_static_tokens, dtype=np.intc
            )
        inst.n_tokens = self._resident_static_len

    def _window_resident_seq(self, inst: Any, n_keep: int) -> int:
        """Slide the resident session window: drop the oldest ~half of the live
        conversation — positions [n_keep, n_keep+n_discard) — and shift the recent
        tail down by n_discard, keeping the static head [0, n_keep) intact. Lets a
        deep session run PAST n_ctx (the oldest turns fall out of context) instead
        of the backend raising at the context-window guard. Only the tail BEYOND
        the static head is shifted, so the static-prefix positions (pos_min=0) are
        never touched (the seq_add corruption the reasoning strip warned about hit
        spans that included the head). Returns the new n_tokens."""
        ctx = inst._ctx
        n_tokens = int(inst.n_tokens)
        n_discard = (n_tokens - n_keep) // 2
        if n_discard <= 0:
            return n_tokens
        ctx.memory_seq_rm(SEQ_WORKING, n_keep, n_keep + n_discard)
        ctx.memory_seq_add(SEQ_WORKING, n_keep + n_discard, n_tokens, -n_discard)
        inst.input_ids[n_keep: n_tokens - n_discard] = inst.input_ids[
            n_keep + n_discard: n_tokens
        ]
        inst.n_tokens = n_tokens - n_discard
        log.warning(
            "🪟 windowed resident seq: dropped %d oldest tokens "
            "(kept %d static head + %d recent)",
            n_discard, n_keep, inst.n_tokens - n_keep,
        )
        return inst.n_tokens

    def _alloc_flow_seq(self, inst: Any, flow_key: str) -> int:
        """Allocate a resident seq id for flow_key in the per-instance flow band
        [SEQ_FLOW_BASE, SEQ_FLOW_BASE+flow_hot_set), LRU-evicting when full."""
        flow_seqs = inst._flow_seqs
        used = set(flow_seqs.values())
        for s in range(SEQ_FLOW_BASE, SEQ_FLOW_BASE + self._flow_hot_set):
            if s not in used:
                flow_seqs[flow_key] = s
                return s
        old_key, old_seq = flow_seqs.popitem(last=False)  # evict LRU, reuse its seq
        inst._ctx.memory_seq_rm(old_seq, 0, -1)
        flow_seqs[flow_key] = old_seq
        self._h_flow_evicts += 1
        log.info("resident flow LRU evict %r → reuse seq %d", old_key, old_seq)
        return old_seq

    def _resident_flow(
        self, inst: Any, flow_key: str, flow_prefix_len: int, prompt_tokens: List[int]
    ) -> Optional[int]:
        """Resident flow-prefix cache (Phase 2): pin [global static + flow head]
        (flow_prefix_len tokens) on a dedicated per-instance seq and fork it onto
        seq 0 per request, so only the dynamic tail prefills — NO save_state, just
        seq_cp. acquire_instance() has already forked SEQ_STATIC → seq 0 (global
        static). Returns flow_prefix_len on success, or None to fall back to base."""
        try:
            # Telemetry: did THIS request reuse a pinned flow seq (HIT) or build
            # it fresh (BUILD)? Read back by generate_stream_sync for cache_hit.
            inst._resident_flow_hit = False
            flow_seqs = inst._flow_seqs
            ctx = inst._ctx
            ids = np.array(prompt_tokens[:flow_prefix_len], dtype=np.intc)
            if flow_key in flow_seqs:
                # HIT: replace seq 0 (global static) with the pinned [global+flow].
                flow_seqs.move_to_end(flow_key)
                seq = flow_seqs[flow_key]
                ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
                ctx.memory_seq_cp(seq, SEQ_WORKING, -1, -1)
                inst.n_tokens = flow_prefix_len
                inst.input_ids[:flow_prefix_len] = ids
                inst._resident_flow_hit = True
                self._h_flow_hits += 1
                log.info(
                    "🔁 resident flow HIT %r (seq %d, %d tok)",
                    flow_key, seq, flow_prefix_len,
                )
            else:
                # BUILD: seq 0 holds the global static (acquire fork); eval the flow
                # head on top, then pin a copy on a dedicated flow seq.
                inst.eval(list(prompt_tokens[self._resident_static_len: flow_prefix_len]))
                seq = self._alloc_flow_seq(inst, flow_key)
                ctx.memory_seq_rm(seq, 0, -1)
                ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
                inst.input_ids[:flow_prefix_len] = ids
                self._h_flow_builds += 1
                log.info(
                    "🆕 resident flow BUILD %r (seq %d, %d tok)",
                    flow_key, seq, flow_prefix_len,
                )
            return flow_prefix_len
        except Exception as exc:  # noqa: BLE001 — fall back to the static base
            self._h_flow_fallbacks += 1
            log.warning("resident flow failed for %r (%s) — static base", flow_key, exc)
            self._resident_restore_static(inst)
            return None

    # ------------------------------------------------------------------
    # Semi-permanent session snapshots (hot seq band + cold token list)
    # ------------------------------------------------------------------

    def _snap_seq_base(self) -> int:
        return SEQ_FLOW_BASE + (self._flow_hot_set if self._flow_band else 0)

    def _alloc_snap_seq(self, inst: Any, key: str) -> int:
        """Reserve a snapshot seq id. NO eviction: capacity errors are the
        caller's signal to purge — a silently evicted snapshot would turn a
        guaranteed ~zero-prefill fork into a surprise full re-prefill."""
        used = set(inst._snap_seqs.values())
        base = self._snap_seq_base()
        for s in range(base, base + self._snapshot_max):
            if s not in used:
                inst._snap_seqs[key] = s
                return s
        raise RuntimeError(
            f"snapshot capacity ({self._snapshot_max}) reached — purge one first"
        )

    def snapshot_working_seq(self, inst: Any, key: str) -> dict:
        """Pin the live working seq's KV under ``key`` (hot) and record the
        dynamic token stream (cold). The seq_cp shares cells (no copy); the
        snapshot owns them alone once the working seq moves on. Registry entry
        survives session end; only purge_snapshot frees it."""
        if not self._resident_active:
            raise RuntimeError("resident cache inactive — use the replay fallback")
        if key in self._snap_registry:
            raise RuntimeError(f"snapshot key {key!r} already exists — purge first")
        n_tokens = int(inst.n_tokens)
        static_len = self._resident_static_len
        # Capacity: static + existing snapshots + this candidate must leave
        # generation headroom in the shared n_ctx cell pool.
        live = sum(
            len(e["dyn_tokens"]) + static_len
            for e in self._snap_registry.values()
            if e.get("resident")
        )
        n_ctx = int(getattr(inst, "_n_ctx", 0) or 0)
        if n_ctx and live + n_tokens + SNAP_GEN_RESERVE > n_ctx:
            raise RuntimeError(
                f"snapshot would exceed context budget "
                f"({live} pinned + {n_tokens} candidate + {SNAP_GEN_RESERVE} "
                f"reserve > {n_ctx})"
            )
        seq = self._alloc_snap_seq(inst, key)
        try:
            inst._ctx.memory_seq_rm(seq, 0, -1)
            inst._ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
        except Exception:
            inst._snap_seqs.pop(key, None)
            raise
        entry = {
            "dyn_tokens": [int(t) for t in inst.input_ids[static_len:n_tokens]],
            "static_len": static_len,
            "turn_count": 0,  # caller (session manager) overwrites
            "created_at": time.time(),
            "resident": True,
        }
        self._snap_registry[key] = entry
        log.info(
            "📸 snapshot %r pinned: seq %d, %d tokens (%d dynamic)",
            key, seq, n_tokens, n_tokens - static_len,
        )
        return {"tokens": n_tokens, "resident": True}

    def fork_snapshot_seq(self, inst: Any, key: str) -> Optional[int]:
        """Fork snapshot ``key`` onto the working seq. HOT (pinned on this
        instance): pure seq_cp, ~zero cost. Cold miss: returns None — caller
        runs rebuild_snapshot_cold. Unknown key: KeyError."""
        entry = self._snap_registry[key]
        seq = inst._snap_seqs.get(key)
        if seq is None or not entry.get("resident"):
            return None
        ctx = inst._ctx
        static_len = int(entry["static_len"])
        n_total = static_len + len(entry["dyn_tokens"])
        ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
        ctx.memory_seq_cp(seq, SEQ_WORKING, -1, -1)
        inst.input_ids[:static_len] = np.array(
            self._resident_static_tokens, dtype=np.intc
        )
        inst.input_ids[static_len:n_total] = np.array(
            entry["dyn_tokens"], dtype=np.intc
        )
        inst.n_tokens = n_total
        log.info("🔁 snapshot fork HIT %r (seq %d, %d tok)", key, seq, n_total)
        return n_total

    def rebuild_snapshot_cold(self, inst: Any, key: str) -> int:
        """Re-prefill snapshot ``key`` from its token list (after a context
        refresh or on a different instance), then re-pin it hot. The cold path
        costs one prefill — loudly counted, never silent."""
        entry = self._snap_registry[key]
        self._resident_restore_static(inst)
        dyn = list(entry["dyn_tokens"])
        if dyn:
            inst.eval(dyn)
        n_total = int(inst.n_tokens)
        if entry.get("resident") and self._resident_active:
            try:
                seq = inst._snap_seqs.get(key) or self._alloc_snap_seq(inst, key)
                inst._ctx.memory_seq_rm(seq, 0, -1)
                inst._ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
            except Exception as exc:  # noqa: BLE001 — fork source is optional
                inst._snap_seqs.pop(key, None)
                log.warning("snapshot %r re-pin failed (%s) — stays cold", key, exc)
        self._h_snapshot_rebuilds += 1
        log.info(
            "🧊 snapshot %r cold rebuild: %d tokens re-prefixed", key, n_total
        )
        return n_total

    def purge_snapshot(self, key: str) -> bool:
        """Free ``key`` everywhere: hot seqs on every instance + registry."""
        found = key in self._snap_registry
        self._snap_registry.pop(key, None)
        for inst in self._all_instances:
            seq = getattr(inst, "_snap_seqs", {}).pop(key, None)
            if seq is not None:
                with contextlib.suppress(Exception):
                    inst._ctx.memory_seq_rm(seq, 0, -1)
        if found:
            log.info("🗑️ snapshot %r purged", key)
        return found

    def register_replay_snapshot(self, key: str, dyn_tokens: List[int]) -> dict:
        """Cold-only snapshot for the non-resident/recurrent fallback: no seq
        ops, just the token history — from_snapshot seeds a full re-prefill.
        Same registry, same purge, same API surface; telemetry marks the mode."""
        if key in self._snap_registry:
            raise RuntimeError(f"snapshot key {key!r} already exists — purge first")
        self._snap_registry[key] = {
            "dyn_tokens": [int(t) for t in dyn_tokens],
            "static_len": 0,
            "turn_count": 0,
            "created_at": time.time(),
            "resident": False,
        }
        log.info("📸 snapshot %r registered (replay mode, %d tok)", key, len(dyn_tokens))
        return {"tokens": len(dyn_tokens), "resident": False}

    def list_snapshots(self) -> List[dict]:
        return [
            {
                "key": k,
                "tokens": e["static_len"] + len(e["dyn_tokens"]),
                "resident": bool(e.get("resident")),
                "turn_count": int(e.get("turn_count") or 0),
                "created_at": float(e.get("created_at") or 0.0),
            }
            for k, e in self._snap_registry.items()
        ]

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

        # Resident-seq gate: the seq ops (memory_seq_cp/rm) require memory_can_shift()
        # — true on SWA models with swa_full and on can-shift hybrids (Qwen3-Next),
        # FALSE on pure-recurrent state. When false, force the resident path off and
        # fall back to the legacy save_state/full_replay path.
        if self._resident_requested:
            can_shift = bool(self._primary_instance._ctx.memory_can_shift())
            self._resident_active = can_shift
            if can_shift:
                log.info("🧩 Resident-seq cache ACTIVE (memory_can_shift=True)")
            else:
                log.warning(
                    "🧩 resident_seq_cache requested but memory_can_shift=False "
                    "(pure-recurrent) — falling back to legacy save_state path"
                )

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

        # Start the proactive context-refresh loop (interval-gated, idle-only).
        self._refresh_loop_task = asyncio.create_task(self._refresh_loop())

    async def shutdown(self) -> None:
        """Clean up all instances and saved state.

        Shared instances' contexts are freed first, then the primary
        instance (which owns the model weights) is closed last to
        avoid use-after-free.
        """
        await self._cancel_scaler_task()
        if self._refresh_loop_task is not None:
            self._refresh_loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_loop_task
            self._refresh_loop_task = None

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

        # Restore seq 0 to the pristine post-static-tokens state. Guard it like
        # any other GPU work. Resident: fork SEQ_STATIC → seq 0 (intra-context
        # copy). Legacy: load_state the (multi-GB) snapshot blob.
        if self._resident_active:
            async with self.generation_guard():
                await run_in_threadpool(self._resident_restore_static, inst)
            log.debug("🧩 Forked SEQ_STATIC → seq 0 before request (resident)")
        elif self._static_state is not None:
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

        # ── Per-flow static-prefix KV cache (opt-in) ──────────────────────
        # Pin [global static + this flow's static head] (flow_prefix_len tokens)
        # so later visits restore it and prefill only the dynamic tail. BUILD
        # uses reset()+eval() (the global-buffer pattern); SERVE uses
        # load_state() — NEVER load_state()+eval() (SWA-fragile). Any save_state
        # failure (recurrent model, blob overflow) falls back to the static base.
        flow_key = kwargs.pop("flow_key", None)
        flow_prefix_len = int(kwargs.pop("flow_prefix_len", 0) or 0)
        flow_n_static = None
        flow_hit_telemetry = False  # did this request reuse a pinned flow head?
        flow_eligible = bool(
            flow_key
            and getattr(self.config.model, "flow_kv_cache", False)
            and static_in_prompt
            and 0 < flow_prefix_len <= len(prompt_tokens)
        )
        if flow_eligible and self._resident_active:
            # Phase 2: resident flow hot-set — pin [global static + flow head] on a
            # dedicated seq, fork onto seq 0 per request. No save_state (seq_cp only).
            flow_n_static = self._resident_flow(
                instance, flow_key, flow_prefix_len, prompt_tokens
            )
            flow_hit_telemetry = flow_n_static is not None and bool(
                getattr(instance, "_resident_flow_hit", False)
            )
        elif flow_eligible:
            try:
                if flow_key in self._flow_states:
                    self._flow_states.move_to_end(flow_key)
                    instance.load_state(self._flow_states[flow_key])
                    flow_hit_telemetry = True
                    self._h_flow_hits += 1
                    log.info(
                        "🔁 flow_kv_cache HIT %r (%d tok pinned)",
                        flow_key, flow_prefix_len,
                    )
                else:
                    # Build ON TOP of the global static that acquire_instance
                    # already loaded — eval ONLY the flow-static span and
                    # snapshot [global + flow_static]. This reproduces the
                    # uncached path's global base EXACTLY (same warmup snapshot),
                    # so output is bit-identical; reset()+eval(whole prefix)
                    # recomputes the global KV and diverges. (eval-on-top of a
                    # loaded state is what generate() does every request.)
                    n_global = (
                        self._static_state.n_tokens if self._static_state else 0
                    )
                    instance.eval(list(prompt_tokens[n_global:flow_prefix_len]))
                    self._flow_states[flow_key] = instance.save_state()
                    cap = max(
                        1,
                        int(getattr(self.config.model, "flow_kv_cache_max", 8) or 8),
                    )
                    while len(self._flow_states) > cap:
                        self._flow_states.popitem(last=False)
                        self._h_flow_evicts += 1
                    self._h_flow_builds += 1
                    log.info(
                        "🆕 flow_kv_cache BUILD %r (%d tok)",
                        flow_key, flow_prefix_len,
                    )
                flow_n_static = flow_prefix_len
            except Exception as exc:  # noqa: BLE001 — save_state fragility net
                self._h_flow_fallbacks += 1
                log.warning(
                    "flow_kv_cache failed for %r (%s) — using static base",
                    flow_key, exc,
                )
                if self._static_state is not None:
                    instance.load_state(self._static_state)
                flow_n_static = None

        n_static = (
            flow_n_static
            if flow_n_static is not None
            else (
                self._static_state.n_tokens
                if (self._static_state and static_in_prompt)
                else 0
            )
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

        def _capture_meta() -> dict:
            # The prompt is the half of the specimen the live failure never
            # preserved (43 cancelled menu runaways, prompts unrecoverable —
            # the archived survivor reproduced nothing at any temperature).
            # Detok only on capture (rare), tail only (the dynamic context
            # that varies between a clean run and a runaway sits at the end).
            meta = {
                "request_id": kwargs.get("request_id", ""),
                "temperature": temperature,
                "prompt_tokens": len(prompt_tokens),
            }
            try:
                meta["prompt_tail"] = instance.detokenize(
                    list(prompt_tokens[-768:])
                ).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - forensics must not break capture
                meta["prompt_tail"] = "(detokenization failed)"
            return meta

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
                        self._h_runaway_captures += 1
                        runaway_capture.dump_capture(
                            capture_dir or "./logs",
                            gen_end_reason,
                            acc_bytes,
                            len(completion_tokens),
                            meta=_capture_meta(),
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
                        self._h_runaway_captures += 1
                        runaway_capture.dump_capture(
                            capture_dir or "./logs",
                            lc_reason,
                            acc_bytes,
                            len(completion_tokens),
                            meta=_capture_meta(),
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
            # Cache-aware token telemetry (read-only — what the run already
            # computed). kv_base = KV the model SKIPPED prefilling (global static
            # + flow head for stateless; full restored occupancy for sessions);
            # dynamic_tokens = tokens actually prefilled this request.
            instance._last_kv_base = int(kv_base)
            instance._last_dynamic_len = int(len(dynamic_tokens))
            instance._last_flow_hit = bool(flow_hit_telemetry)
            instance._last_flow_key = flow_key or ""
            # Honest cache_hit: did this request skip prefilling MORE than the
            # always-on global static fork? That captures resident-session reuse
            # (the ~89% of real work) and flow-pin hits alike. The old signal read
            # _last_flow_hit only — the flow-pin, which fires on the ~1/N stateless
            # completions — so it reported ~0% while the resident cache was doing
            # the heavy lifting. (_last_flow_hit is kept for flow-pin-specific stats.)
            instance._last_cache_hit = bool(flow_hit_telemetry) or int(kv_base) > self._resident_static_len
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
                    meta=_capture_meta(),
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
            self._h_requests_since_refresh += 1  # drives the periodic context refresh
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
            self._h_requests_since_refresh += 1  # drives the periodic context refresh
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

        # ── Deep-health: long-run degradation signals ──────────────────
        # Memory residency/pressure — the Apple-Silicon unified-memory KV/weight
        # eviction failure mode shows here as falling RSS / rising system %
        # while a long run is active (cf. flow-cache fallbacks below).
        try:
            import psutil

            vm = psutil.virtual_memory()
            info["mem_process_rss_mb"] = round(psutil.Process().memory_info().rss / 1e6, 1)
            info["mem_system_used_percent"] = vm.percent
            info["mem_system_available_mb"] = round(vm.available / 1e6, 1)
            # Wired (unevictable) bytes — on macOS a drop here while the model is
            # loaded means weights/KV got evicted to the compressor.
            wired = getattr(vm, "wired", None)
            if wired is not None:
                info["mem_system_wired_mb"] = round(wired / 1e6, 1)
        except Exception:
            pass
        # Flow/resident KV-cache health: live size + cumulative churn. A rising
        # fallback rate is the canary for KV-cache instability under pressure.
        info["flow_cache_entries"] = len(self._flow_states)
        info["resident_active"] = self._resident_active
        info["flow_builds"] = self._h_flow_builds
        info["flow_hits"] = self._h_flow_hits
        info["flow_evicts"] = self._h_flow_evicts
        info["flow_fallbacks"] = self._h_flow_fallbacks
        info["runaway_captures"] = self._h_runaway_captures
        info["context_refreshes"] = self._h_context_refreshes
        info["requests_since_refresh"] = self._h_requests_since_refresh
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
