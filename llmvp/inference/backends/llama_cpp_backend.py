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
import os
import time
from collections import OrderedDict
from typing import Any, AsyncGenerator, Dict, Iterator, List, Optional

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


# ── ggml/llama.cpp native log forwarding ──────────────────────────────
# verbose=False installs a NULL llama.cpp log callback, which SWALLOWS the
# Metal backend's own error lines — including `command buffer failed with
# status 5 (Insufficient Memory)` and the sticky-latch notice `backend is in
# error state from a previous command buffer failure` that explain every
# llama_decode -3. Forward native logs into our logger instead so production
# is never blind to the first failure of an episode. Module-level refs keep
# the ctypes callback alive (GC'd callbacks segfault the C side).
_GGML_LOG_LEVELS = {2: logging.INFO, 3: logging.WARNING, 4: logging.ERROR}
_ggml_log_cb = None  # ctypes CFUNCTYPE ref — MUST outlive the process
_ggml_line_buf: dict = {"buf": ""}


def _install_ggml_log_forwarding() -> None:
    global _ggml_log_cb
    if _ggml_log_cb is not None:
        return  # once per process
    import ctypes

    import llama_cpp

    def _cb(level: int, text: bytes, _user_data) -> None:
        try:
            frag = (text or b"").decode("utf-8", errors="replace")
            _ggml_line_buf["buf"] += frag
            while "\n" in _ggml_line_buf["buf"]:
                line, _ggml_line_buf["buf"] = _ggml_line_buf["buf"].split("\n", 1)
                if line.strip():
                    # CONT fragments arrive as level 5 — the buffered line's
                    # severity rides on the level of its final fragment; treat
                    # unknown levels as DEBUG so noise stays out of prod logs.
                    log.log(_GGML_LOG_LEVELS.get(level, logging.DEBUG),
                            "ggml: %s", line.strip())
        except Exception:  # noqa: BLE001 — a log hook must never throw into C
            pass

    _ggml_log_cb = llama_cpp.ggml_log_callback(_cb)
    llama_cpp.llama_log_set(_ggml_log_cb, ctypes.c_void_p(0))
    log.info("🔎 ggml native log forwarding installed (llama_log_set)")


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
        # Concurrency architecture: "pool" (N contexts) vs "batched" (ONE
        # context, N working seqs, one llama_decode per step — the
        # llama-server slot pattern). Config-gated; the pool path is not
        # modified when "batched" is off. See inference/batched_engine.py.
        self._decode_mode: str = getattr(
            config.resources, "decode_mode", "pool"
        )
        self._engine: Any = None  # BatchedEngine when _decode_mode=="batched"
        self._engine_seats: List[Any] = []  # SeqSlot seats (batched mode)
        self._batched_map: Any = None  # memoized SeqMap (batched mode)
        self._all_instances: List[Any] = []  # For shutdown cleanup
        self._llama_module = None
        self._tokenizer = None
        # Hybrid/recurrent model support
        self._is_hybrid = False
        # Saved LlamaState per PERSONA after processing that persona's static
        # tokens (multi-persona pooling: each pool slot may carry a different
        # "SOUL"; states are only reused across same-persona slots).
        self._static_states: Dict[str, Any] = {}
        # Persona name per pool slot (resolved from config in initialize();
        # ["default"] * pool_size when no slot_personas configured).
        self._slot_personas: List[str] = ["default"] * self._pool_size
        # Idle-instance queues per persona. "default" aliases _pool_queue so
        # every legacy path keeps working; named personas get their own queue
        # and acquire_instance(persona=...) routes to it.
        self._persona_queues: Dict[str, asyncio.Queue] = {}
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
        # Fatal decode failures (llama_decode -3/-2 — the Metal error latch)
        # and the targeted context-refresh heals that recover the slot.
        self._h_decode_failures = 0
        self._h_latch_heals = 0
        # Harmony final-channel dynamic stop (see inference/final_channel_stop):
        # how many session turns ended at a non-empty final close rather than the
        # (history-form-shadowed) <|return|>. Non-zero confirms the stop is doing
        # real work; a spike alongside short turns would flag premature firing.
        self._h_final_channel_stops = 0
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
        # Per-request reasoning HEAD-SWAP (config.model.reasoning_head_swap). Pin a
        # system head per reasoning level on a band ABOVE the snapshot band; a
        # session turn carrying reasoning=<level> forks that head onto the live seq
        # at turn 0. gpt-oss/harmony only (reasoning steer lives in the cached
        # system slot). The default level (thinking_mode) reuses SEQ_STATIC, so only
        # the OTHER levels get a pinned head. Per-instance presence (_reasoning_seqs).
        _model_cfg = getattr(config, "model", None)
        self._reasoning_levels = ["low", "medium", "high"]
        self._reasoning_default_level = getattr(_model_cfg, "thinking_mode", None)
        self._reasoning_pin_levels = [
            lv for lv in self._reasoning_levels if lv != self._reasoning_default_level
        ]
        self._reasoning_head_swap = (
            self._resident_requested
            and bool(getattr(_model_cfg, "reasoning_head_swap", False))
            and getattr(_model_cfg, "family", "") == "harmony"
        )
        self._h_reasoning_swaps = 0
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

    @property
    def _static_state(self) -> Any:
        """DEFAULT persona's static state — compatibility view over the
        per-persona dict so pre-persona code paths keep reading/writing the
        single-SOUL state they always did."""
        return self._static_states.get("default")

    @_static_state.setter
    def _static_state(self, value: Any) -> None:
        if value is None:
            self._static_states.pop("default", None)
        else:
            self._static_states["default"] = value

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
            # can_shift gate later disables the resident path. Batched mode
            # uses its own layout: W working seats + persona heads +
            # reasoning heads (see inference/batched_engine.plan_seq_map).
            n_seq_max=(
                self._batched_seq_map().n_seq_max
                if self._decode_mode == "batched"
                else (
                    2
                    + (self._flow_hot_set if self._flow_band else 0)
                    + self._snapshot_max
                    + (len(self._reasoning_pin_levels) if self._reasoning_head_swap else 0)
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

    def _create_shared_instance(self, primary: Any, n_ctx_override: Optional[int] = None) -> Any:
        """Create a pool instance that shares model weights with the primary.

        The returned object is a full ``Llama`` instance (same class,
        same public API) but its ``_model`` attribute points to the
        *primary's* model — no duplicate weight load.  It gets its own
        ``_ctx`` (KV-cache), ``_batch``, ``input_ids``, ``scores``, and
        sampler state so that inference is fully independent.

        ``n_ctx_override`` shrinks this slot's context (the multi-persona
        memory knob — e.g. a 32k user-sim slot beside the 131k primary).
        """
        import copy

        from llama_cpp import internals

        # Shallow-copy the primary to inherit all config / metadata.
        inst = copy.copy(primary)

        # --- Replace mutable, per-instance objects ---

        # New ExitStack (we manage cleanup ourselves in shutdown)
        inst._stack = contextlib.ExitStack()

        # New context from the *shared* model. For an n_ctx override, clone
        # the params struct (ctypes by-value copy) rather than mutating the
        # primary's shared struct — the context may retain a reference, and a
        # mutate-and-restore dance would leave slot 1 built against params
        # that later revert (Metal graph failure at first decode).
        params = primary.context_params
        if n_ctx_override:
            params = type(params).from_buffer_copy(params)
            params.n_ctx = int(n_ctx_override)
            inst.context_params = params  # this slot's own params (refresh reuses it)
        ctx = internals.LlamaContext(
            model=primary._model,
            params=params,
            verbose=False,
        )
        inst._ctx = ctx
        inst._n_ctx = int(n_ctx_override) if n_ctx_override else primary._n_ctx
        inst._persona_n_ctx = int(n_ctx_override) if n_ctx_override else None

        # New batch
        batch = internals.LlamaBatch(
            n_tokens=primary.n_batch,
            embd=0,
            n_seq_max=primary.context_params.n_seq_max,
            verbose=False,
        )
        inst._batch = batch

        # New mutable arrays (sized to THIS slot's n_ctx, which may be
        # persona-overridden)
        inst.input_ids = np.ndarray((inst._n_ctx,), dtype=np.intc)
        # Match upstream Llama.__init__: ONE logits row when logits_all is
        # false (llama.py:704). n_batch rows here silently inflated every
        # save_state blob ~2048x — for gpt-oss's 201k vocab that is a
        # ~1.6 GB scores copy PER BLOB (memory audit; the hidden weight
        # behind the historical flow_kv_cache pain). The decode path only
        # ever reads row 0 when logits_all is false.
        logits_rows = inst._n_ctx if primary._logits_all else 1
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

    def _warm_up_instance(
        self, llm_inst: Any, idx: int = 0, persona: Optional[str] = None
    ) -> None:
        """Warm up by evaluating the slot's PERSONA static tokens and
        snapshotting model state.

        This strategy is used for every model architecture:

        1. **First pool slot of a persona**: evaluates that persona's static
           tokens through the model (populating KV cache and, for hybrid
           models, the recurrent state), then saves a full state snapshot
           under the persona key.
        2. **Subsequent same-persona slots**: load the pre-computed snapshot
           instantly — no re-evaluation needed.
        3. **Before every request** (in ``acquire_instance``): the
           snapshot is restored so the model always begins from a
           pristine post-static-context state, guaranteeing correct
           prefix matching and perfect request isolation.

        ``persona`` resolution order: explicit arg → the instance's recorded
        persona (context refresh re-warm) → the slot assignment → "default".
        """
        try:
            from preprocessing.static_tokens import get_static_tokens

            if persona is None:
                persona = getattr(llm_inst, "_persona", None) or (
                    self._slot_personas[idx]
                    if idx < len(self._slot_personas)
                    else "default"
                )
            llm_inst._persona = persona
            static_tokens = get_static_tokens(persona)
            n_tokens = len(static_tokens)

            started = time.perf_counter()

            state = self._static_states.get(persona)
            if state is not None:
                # State already computed by this persona's first instance —
                # just load it.
                llm_inst.load_state(state)
                log.info(
                    f"✅ Loaded pre-computed static state [{persona}] for pool "
                    f"slot #{idx} in {time.perf_counter() - started:.2f}s"
                )
            elif n_tokens == 0:
                # No static tokens (--skip-knowledge) — save a clean
                # initial state without any system prompt prefix.
                llm_inst.reset()
                self._static_states[persona] = llm_inst.save_state()
                log.info(
                    f"📝 No static tokens — saved clean initial state "
                    f"(pool slot #{idx})"
                )
            else:
                log.info(
                    f"🔄 Processing {n_tokens:,} static tokens [{persona}] for "
                    f"state snapshot (pool slot #{idx})…"
                )
                llm_inst.reset()
                llm_inst.eval(list(static_tokens))
                # save_state ONLY on the primary. Live finding (duo spike):
                # save_state() on a copy.copy'd shared instance corrupts its
                # context — the next decode dies with llama_decode -3 ("Graph
                # computation failed internally"). The blob is only needed for
                # same-persona fast-warm of LATER slots and the legacy
                # non-resident restore; a resident shared slot pins SEQ_STATIC
                # from its own eval below and never touches the blob. Later
                # same-persona slots simply re-eval (~0.6s per 488 tok).
                if llm_inst is self._primary_instance or not self._resident_active:
                    self._static_states[persona] = llm_inst.save_state()
                    state_mb = (
                        self._static_states[persona].llama_state_size / (1024 * 1024)
                    )
                    log.info(
                        f"✅ State snapshot saved [{persona}] — {n_tokens:,} tokens, "
                        f"{state_mb:,.1f} MiB C-level state, static eval took "
                        f"{time.perf_counter() - started:.2f}s (pool slot #{idx})"
                    )
                else:
                    log.info(
                        f"✅ Static eval done [{persona}] — {n_tokens:,} tokens, "
                        f"no state blob (shared slot, resident pin only) in "
                        f"{time.perf_counter() - started:.2f}s (pool slot #{idx})"
                    )

            # Resident path: seq 0 now holds the pristine static prefix — pin a copy
            # onto SEQ_STATIC so acquire_instance can fork it back onto seq 0 per
            # request (an intra-context copy, never a save_state blob).
            if self._resident_active:
                # Per-instance static identity (multi-persona: each slot's
                # restore/fork must use ITS OWN tokens, never a global's).
                llm_inst._static_len = n_tokens
                llm_inst._static_tokens = list(static_tokens)
                if persona == "default":
                    # Keep the legacy backend-level fields in sync for any
                    # pre-persona reader (windowing, session paths).
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
                    "🧩 Pinned %d static tokens [%s] to SEQ_STATIC (pool slot #%d)",
                    n_tokens, persona, idx,
                )
                # Reasoning HEAD-SWAP: pin a head per non-default level (re-pinned
                # here on every refresh/rewarm since this is the single warm path).
                self._pin_reasoning_heads(llm_inst)
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
        primary_persona = getattr(self._primary_instance, "_persona", "default")
        self._primary_instance.load_state(self._static_states[primary_persona])
        log.info(
            "🔄 Re-warmed primary instance after scale-down in %.2fs",
            time.perf_counter() - started,
        )

    # ------------------------------------------------------------------
    # Batched decode mode (decode_mode: "batched")
    # ------------------------------------------------------------------

    def _batched_seq_map(self) -> Any:
        """The single batched context's seq layout (memoized — n_seq_max is
        baked into the context at creation)."""
        if self._batched_map is None:
            from inference.batched_engine import plan_seq_map

            persona_names = sorted(getattr(self.config, "personas", {}) or {})
            self._batched_map = plan_seq_map(
                self._pool_size,
                persona_names,
                self._reasoning_pin_levels if self._reasoning_head_swap else [],
            )
        return self._batched_map

    def _pin_batched_heads(self) -> tuple:
        """Eval + pin every persona static head (and reasoning heads) on the
        batched seq map's bands. Runs on whichever thread OWNS the context:
        the init threadpool at warmup, the decode thread at latch rebuild.
        Returns (persona_heads, reasoning_heads)."""
        from inference.batched_engine import PersonaHead
        from preprocessing.static_tokens import get_static_tokens

        primary = self._primary_instance
        ctx = primary._ctx
        seq_map = self._batched_seq_map()

        heads: Dict[str, PersonaHead] = {}
        for persona, head_seq in seq_map.persona_seqs.items():
            tokens = list(get_static_tokens(persona))
            started = time.perf_counter()
            ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
            primary.reset()
            if tokens:
                primary.eval(tokens)
                ctx.memory_seq_rm(head_seq, 0, -1)
                ctx.memory_seq_cp(SEQ_WORKING, head_seq, -1, -1)
            heads[persona] = PersonaHead(
                name=persona, seq=head_seq, tokens=tokens
            )
            log.info(
                "🧩 Pinned persona head [%s] — %d tokens on seq %d in %.2fs",
                persona, len(tokens), head_seq, time.perf_counter() - started,
            )
            if persona == "default":
                # Legacy backend-level readers (windowing, session paths).
                self._resident_static_len = len(tokens)
                self._resident_static_tokens = list(tokens)
                primary._static_len = len(tokens)
                primary._static_tokens = list(tokens)
        # Reasoning heads (head-swap band): built for the DEFAULT persona,
        # same eval-then-pin pattern. The default level itself reuses the
        # default persona head at install time.
        reasoning_heads: Dict[str, Any] = {}
        if self._reasoning_head_swap and seq_map.reasoning_seqs:
            from preprocessing.builder import build_static_tokens

            try:
                for level, seq in seq_map.reasoning_seqs.items():
                    toks = list(
                        build_static_tokens(
                            self.config, reasoning=level, persona="default"
                        )
                    )
                    ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
                    primary.reset()
                    if toks:
                        primary.eval(toks)
                        ctx.memory_seq_rm(seq, 0, -1)
                        ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
                    reasoning_heads[level] = PersonaHead(
                        name=level, seq=seq, tokens=toks
                    )
                log.info(
                    "🧠 pinned %d reasoning heads %s (batched band)",
                    len(reasoning_heads),
                    {lv: h.n_tokens for lv, h in reasoning_heads.items()},
                )
            except Exception as exc:  # noqa: BLE001 — head-swap off, stay safe
                log.warning(
                    "reasoning head pin failed (%s) — head-swap off (batched)",
                    exc,
                )
                reasoning_heads = {}
        ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
        primary.reset()
        return heads, reasoning_heads

    def _rebuild_batched_context(self) -> None:
        """Latch recovery for the batched engine: drop + rebuild the shared
        llama_context (fresh Metal backend clears the sticky error latch),
        then re-pin every head. Runs ON the decode thread (via the engine's
        rebuild hook) or with the engine paused (refresh) — never while a
        step is in flight."""
        from llama_cpp import internals

        primary = self._primary_instance
        started = time.perf_counter()
        try:
            if primary._ctx is not None:
                primary._ctx.close()
        except Exception:
            log.exception("⚠️ context close FAILED during batched rebuild")
        try:
            if primary._batch is not None:
                primary._batch.close()
        except Exception:
            log.exception("⚠️ batch close FAILED during batched rebuild")
        primary._ctx = internals.LlamaContext(
            model=primary._model, params=primary.context_params, verbose=False
        )
        primary._batch = internals.LlamaBatch(
            n_tokens=primary.n_batch,
            embd=0,
            n_seq_max=primary.context_params.n_seq_max,
            verbose=False,
        )
        primary.input_ids = np.ndarray((primary._n_ctx,), dtype=np.intc)
        logits_rows = primary._n_ctx if primary._logits_all else 1
        primary.scores = np.ndarray(
            (logits_rows, primary._n_vocab), dtype=np.single
        )
        primary._candidates = internals.LlamaTokenDataArray(
            n_vocab=primary._n_vocab
        )
        primary.n_tokens = 0
        primary._sampler = None
        primary._sampling_ctx = None
        heads, reasoning_heads = self._pin_batched_heads()
        if self._engine is not None:
            self._engine._persona_heads = heads
            self._engine._reasoning_heads = reasoning_heads
        self._h_context_refreshes += 1
        log.info(
            "🧼 batched context rebuilt + heads re-pinned in %.2fs",
            time.perf_counter() - started,
        )

    def _warm_batched(self) -> None:
        """Warm the batched context: pin every persona's static head on its
        band seq, create the working seats, and start the decode engine.

        Runs in a threadpool during initialize(). Mirrors the per-persona
        eval of _warm_up_instance but pins each head on the batched seq
        map's persona band instead of SEQ_STATIC (which is a WORKING seat
        id in this layout)."""
        from inference.batched_engine import BatchedEngine, SeqSlot

        primary = self._primary_instance
        seq_map = self._batched_seq_map()
        heads, reasoning_heads = self._pin_batched_heads()

        # Flow-prefix pinning is pool-only in v1: the persona head already
        # delivers the dominant prefill saving, and the flow LRU is a whole
        # extra seq-allocator surface. Documented deferral.
        if self._session_flow_fork or self._flow_band:
            log.info(
                "flow band / session flow-fork are pool-only — disabled in "
                "batched mode (persona heads cover the static prefix)"
            )
        self._session_flow_fork = False

        self._engine_seats = [
            SeqSlot(seq=i, _n_ctx=primary._n_ctx)
            for i in range(self._pool_size)
        ]

        gen_cfg = self.config.generation
        capture_dir = getattr(
            getattr(self.config, "logging", None), "directory", None
        )
        engine = BatchedEngine(
            primary,
            seq_map,
            n_batch=int(getattr(self.config.model, "n_batch", 2048) or 2048),
            prefill_chunk=getattr(
                self.config.resources, "batched_prefill_chunk", None
            ),
            persona_heads=heads,
            capture_dir=str(capture_dir or "./logs"),
            seats=self._engine_seats,
            rebuild_fn=self._rebuild_batched_context,
        )
        engine.family = self.config.model.family
        engine._long_cycle_enabled = gen_cfg.long_cycle_guard_enabled is not False
        if gen_cfg.repetition_guard_enabled is not False:
            from inference.repetition import (
                DEFAULT_MAX_CYCLE_PERIOD,
                DEFAULT_MAX_RUN,
                DEFAULT_MIN_CYCLE_REPS,
            )

            def _guard_factory() -> RepetitionGuard:
                return RepetitionGuard(
                    max_run=gen_cfg.repetition_max_run or DEFAULT_MAX_RUN,
                    max_cycle_period=(
                        gen_cfg.repetition_max_cycle_period
                        or DEFAULT_MAX_CYCLE_PERIOD
                    ),
                    min_cycle_reps=gen_cfg.repetition_min_cycle_reps
                    or DEFAULT_MIN_CYCLE_REPS,
                )

            engine._repetition_guard_factory = _guard_factory
        else:
            engine._repetition_guard_factory = lambda: None
        engine._reasoning_heads = reasoning_heads
        for seat in self._engine_seats:
            seat._engine_ref = engine
        engine.start()
        self._engine = engine

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
        # A failed close here silently leaks a multi-GB Metal KV allocation
        # once per refresh (48/day at the time cap) — log it loudly; the
        # rebuild itself can still proceed.
        try:
            if inst._ctx is not None:
                inst._ctx.close()
        except Exception:
            log.exception("⚠️ context close FAILED during refresh — KV may leak")
        try:
            if inst._batch is not None:
                inst._batch.close()
        except Exception:
            log.exception("⚠️ batch close FAILED during refresh — buffer may leak")
        # Fresh context + batch from the SAME model (weights stay resident, no reload).
        # Re-apply this slot's persona n_ctx override (context_params is the shared
        # struct — same override-and-restore dance as _create_shared_instance).
        _params = inst.context_params
        _prev_n_ctx = _params.n_ctx
        _override = getattr(inst, "_persona_n_ctx", None)
        if _override:
            _params.n_ctx = int(_override)
        try:
            inst._ctx = internals.LlamaContext(
                model=inst._model, params=_params, verbose=False
            )
        finally:
            _params.n_ctx = _prev_n_ctx
        inst._batch = internals.LlamaBatch(
            n_tokens=inst.n_batch,
            embd=0,
            n_seq_max=inst.context_params.n_seq_max,
            verbose=False,
        )
        # Reset per-instance mutable arrays + sampler (mirrors _create_shared_instance).
        inst.input_ids = np.ndarray((inst._n_ctx,), dtype=np.intc)
        # ONE row when logits_all is false — see _create_shared_instance.
        logits_rows = inst._n_ctx if inst._logits_all else 1
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
            for key in getattr(inst, "_snap_seqs", {}):
                entry = self._snap_registry.get(key)
                if entry is not None:
                    entry["resident"] = False  # cold until the next rebuild re-pins
            log.warning(
                "🧊 context refresh demoted %d session snapshot(s) to cold — "
                "next fork re-prefills",
                demoted,
            )
        # Re-warm into the fresh context: reload the pristine static snapshot + re-pin
        # SEQ_STATIC. The persona's static state is already computed, so this is a
        # load, not a re-eval. MUST re-warm as the instance's OWN persona — a
        # refresh must never quietly turn a user_sim slot back into the default SOUL.
        self._warm_up_instance(inst, 0, persona=getattr(inst, "_persona", "default"))
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
        if self._decode_mode == "batched":
            # Batched refresh: park the decode thread at a step boundary,
            # rebuild the shared context + re-pin heads, resume. Only sound
            # with no pinned seats and no live streams — _refresh_decision
            # gates on exactly that; a manual call mid-session is refused.
            if self._checked_out > 0 or self._active_generations > 0:
                self._h_refresh_deferred += 1
                return {
                    "refreshed": 0, "reason": reason,
                    "status": "deferred_busy",
                }
            engine = self._engine
            await run_in_threadpool(engine.pause)
            try:
                await run_in_threadpool(self._rebuild_batched_context)
                engine._batch = None  # re-allocate against the fresh context
                self._h_requests_since_refresh = 0
                self._last_refresh_monotonic = time.monotonic()
            finally:
                engine.resume()
            elapsed = time.perf_counter() - started
            log.info(
                "✅ Batched context refresh #%d in %.2fs (reason=%s)",
                self._h_context_refreshes, elapsed, reason,
            )
            return {
                "refreshed": 1, "reason": reason, "status": "ok",
                "elapsed_s": round(elapsed, 3),
                "total_refreshes": self._h_context_refreshes,
            }
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

    def _mark_decode_failure(self, inst: Any, exc: Exception) -> None:
        """Record a fatal decode failure and flag the instance for a context
        refresh. The Metal backend's sticky error latch means THIS context
        will fail every subsequent decode — the flag routes it through
        _heal_instance before it serves again. The failing turn still errors
        (caller sees it); the SLOT self-heals."""
        self._h_decode_failures += 1
        inst._needs_context_refresh = True
        log.error(
            "💥 fatal decode failure #%d on instance [%s]: %s — context "
            "flagged for refresh (Metal error latch: this context cannot "
            "decode again until rebuilt; check preceding ggml lines for the "
            "root command-buffer failure, e.g. Insufficient Memory)",
            self._h_decode_failures,
            getattr(inst, "_persona", "default"),
            exc,
        )

    async def _heal_instance(self, inst: Any) -> None:
        """Targeted context refresh for a decode-poisoned instance: rebuild
        its llama_context (fresh Metal backend → error latch cleared) inside
        the same exclusive envelope refresh_context uses, then re-warm as its
        own persona. Best-effort — a failed heal leaves the flag set so the
        next release/acquire tries again."""
        try:
            async with self._scaling_operation():
                if not await self._drain_in_flight("latch heal"):
                    log.warning("⚠️ latch heal deferred — drain timed out")
                    return
                await run_in_threadpool(self._refresh_context_sync, inst)
            inst._needs_context_refresh = False
            self._h_latch_heals += 1
            log.info(
                "🩹 latch heal #%d complete [%s] — context rebuilt, slot healthy",
                self._h_latch_heals, getattr(inst, "_persona", "default"),
            )
        except Exception:  # noqa: BLE001 — healing must never crash the caller
            log.exception("latch heal failed — instance stays flagged")

    def _resident_restore_static(self, inst: Any) -> None:
        """Restore seq 0 to the pristine static prefix by forking SEQ_STATIC onto it
        (clearing seq 0 first) — the resident replacement for load_state(_static_state),
        a cheap intra-context copy rather than a multi-GB blob restore. Syncs the
        high-level n_tokens / input_ids counters so the existing eval/generate path
        (which operates on seq 0) places the dynamic prompt at the correct position.
        SEQ_STATIC is left intact for the next fork.

        Uses the INSTANCE's own static identity (multi-persona: each slot's
        SEQ_STATIC holds its persona's head); falls back to the backend-level
        default-persona fields for pre-persona instances."""
        ctx = inst._ctx
        static_len = int(getattr(inst, "_static_len", self._resident_static_len) or 0)
        static_tokens = getattr(inst, "_static_tokens", None)
        if static_tokens is None:
            static_tokens = self._resident_static_tokens
        ctx.memory_seq_rm(SEQ_WORKING, 0, -1)  # clear the working seq
        if static_len > 0:
            ctx.memory_seq_cp(SEQ_STATIC, SEQ_WORKING, -1, -1)
            inst.input_ids[:static_len] = np.array(static_tokens, dtype=np.intc)
        inst.n_tokens = static_len
        # The restored head IS the default level's — reset the swap tracker so a
        # later session's splice compares against reality.
        inst._reasoning_current = self._reasoning_default_level

    def _reasoning_seq_base(self) -> int:
        """Reasoning-head band base — ABOVE the snapshot band (highest ids), so it
        never collides with the flow LRU or the snapshot allocator."""
        return self._snap_seq_base() + self._snapshot_max

    def _pin_reasoning_heads(self, inst: Any) -> None:
        """Build + pin a system head per non-default reasoning level on the reasoning
        band, forked onto the live seq at turn 0 by _install_reasoning_head. Called
        from _warm_up_instance (so it covers initial warmup, context refresh, and
        every pool slot). No-op unless reasoning_head_swap is enabled. Leaves seq 0
        restored to the pristine static (the warmup post-condition acquire expects)."""
        if not self._reasoning_head_swap:
            return
        inst._reasoning_seqs = {}
        inst._reasoning_head_tokens = {}
        inst._reasoning_head_len = {}
        persona = getattr(inst, "_persona", "default")
        inst_static_tokens = getattr(inst, "_static_tokens", None)
        if inst_static_tokens is None:
            inst_static_tokens = self._resident_static_tokens
        inst_static_len = int(getattr(inst, "_static_len", self._resident_static_len) or 0)
        # The default level (thinking_mode) reuses the already-pinned SEQ_STATIC
        # (which holds THIS instance's persona head).
        if self._reasoning_default_level in self._reasoning_levels:
            inst._reasoning_seqs[self._reasoning_default_level] = SEQ_STATIC
            inst._reasoning_head_tokens[self._reasoning_default_level] = list(
                inst_static_tokens
            )
            inst._reasoning_head_len[self._reasoning_default_level] = inst_static_len
        base = self._reasoning_seq_base()
        try:
            from preprocessing.builder import build_static_tokens

            for i, level in enumerate(self._reasoning_pin_levels):
                toks = build_static_tokens(self.config, reasoning=level, persona=persona)
                seq = base + i
                inst._ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
                inst.n_tokens = 0
                inst.eval(list(toks))
                inst._ctx.memory_seq_rm(seq, 0, -1)
                inst._ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
                inst._reasoning_seqs[level] = seq
                inst._reasoning_head_tokens[level] = list(toks)
                inst._reasoning_head_len[level] = len(toks)
            log.info(
                "🧠 pinned %d reasoning heads %s (band base seq %d)",
                len(self._reasoning_pin_levels),
                {lv: inst._reasoning_head_len[lv] for lv in self._reasoning_pin_levels},
                base,
            )
        except Exception as exc:  # noqa: BLE001 — disable head-swap for this inst, stay safe
            log.warning("reasoning head pin failed (%s) — head-swap off for this slot", exc)
            inst._reasoning_seqs = {}
        finally:
            # Restore seq 0 to the pristine static so acquire_instance's fork is sound.
            self._resident_restore_static(inst)

    def _install_reasoning_head(self, inst: Any, level: str) -> bool:
        """Fork the pinned head for ``level`` onto the live seq — the reasoning
        HEAD-SWAP. Whole-seq replace (like _resident_restore_static), so it is sound
        only at request/turn START (nothing above the head yet); the session
        manager calls it at turn 0. Returns True if a head was installed."""
        if self._decode_mode == "batched":
            # Per-seq head install via the decode thread (control op). The
            # default level reuses the seat's persona head.
            if level == self._reasoning_default_level:
                head = self._engine._persona_heads.get(
                    getattr(inst, "persona", "default")
                )
            else:
                head = self._engine._reasoning_heads.get(level)
            if head is None:
                return False
            self._engine.install_head_sync(inst, head)
            inst._reasoning_current = level
            self._h_reasoning_swaps += 1
            log.info(
                "🧠 reasoning head-swap → %s (seat seq %d, %d tok, batched)",
                level, inst.seq, head.n_tokens,
            )
            return True
        seqs = getattr(inst, "_reasoning_seqs", None) or {}
        if level not in seqs:
            return False
        seq = seqs[level]
        toks = inst._reasoning_head_tokens.get(level) or []
        hlen = int(inst._reasoning_head_len.get(level, 0) or 0)
        ctx = inst._ctx
        ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
        if hlen > 0:
            ctx.memory_seq_cp(seq, SEQ_WORKING, -1, -1)
            inst.input_ids[:hlen] = np.array(toks, dtype=np.intc)
        inst.n_tokens = hlen
        inst._reasoning_current = level
        self._h_reasoning_swaps += 1
        log.info("🧠 reasoning head-swap → %s (seq %d, %d tok)", level, seq, hlen)
        return True

    def _reasoning_head_source(self, inst: Any, level: str):
        """(src_seq, tokens, head_len) for ``level``'s pinned head, or None.

        The default level's head IS the pristine static on SEQ_STATIC (that's
        why only the non-default levels get hold seqs); other levels come from
        the instance's reasoning band."""
        if level == self._reasoning_default_level:
            toks = getattr(inst, "_static_tokens", None)
            if toks is None:
                toks = self._resident_static_tokens
            hlen = int(getattr(inst, "_static_len", self._resident_static_len) or 0)
            return (SEQ_STATIC, toks, hlen) if hlen > 0 else None
        seqs = getattr(inst, "_reasoning_seqs", None) or {}
        if level not in seqs:
            return None
        hlen = int(inst._reasoning_head_len.get(level, 0) or 0)
        toks = inst._reasoning_head_tokens.get(level) or []
        return (seqs[level], toks, hlen) if hlen > 0 else None

    def _splice_reasoning_head(self, inst: Any, level: str) -> bool:
        """Mid-session reasoning HEAD-SWAP SPLICE — replace ONLY the head span
        [0, head_len) of the working seq with ``level``'s pinned head, leaving
        the session body above it intact. The per-turn counterpart of
        _install_reasoning_head (whole-seq replace, turn-0-only). Mechanism
        validated 2026-06 (alternating spike + 54-min corewars full-agent run:
        237 swaps, 0 corruption, 0.62 ms/swap), then reverted; re-applied here
        as the adaptive router's actuator (Phase F).

        Sound iff the current and target heads have the SAME token length (the
        level word tokenizes identically), so the body positions stay aligned —
        verified per call; a mismatch refuses the splice (turn proceeds on the
        current level). Returns True if the splice happened."""
        if self._decode_mode == "batched":
            cur = getattr(inst, "_reasoning_current", None) or self._reasoning_default_level
            if level == cur:
                return False
            if level == self._reasoning_default_level:
                head = self._engine._persona_heads.get(
                    getattr(inst, "persona", "default")
                )
            else:
                head = self._engine._reasoning_heads.get(level)
            if head is None:
                return False
            if not self._engine.splice_head_sync(inst, head):
                return False
            inst._reasoning_current = level
            self._h_reasoning_swaps += 1
            log.info(
                "🧠 reasoning head-splice → %s (seat seq %d, batched, body intact)",
                level, inst.seq,
            )
            return True
        cur = getattr(inst, "_reasoning_current", None) or self._reasoning_default_level
        if level == cur:
            return False
        target = self._reasoning_head_source(inst, level)
        current = self._reasoning_head_source(inst, cur)
        if target is None or current is None:
            return False
        src_seq, toks, hlen = target
        cur_hlen = current[2]
        n_tokens = int(getattr(inst, "n_tokens", 0) or 0)
        if hlen != cur_hlen or hlen > n_tokens:
            log.warning(
                "🧠 reasoning splice refused: head-len mismatch (%s:%d vs %s:%d, n=%d)",
                cur, cur_hlen, level, hlen, n_tokens,
            )
            return False
        ctx = inst._ctx
        ctx.memory_seq_rm(SEQ_WORKING, 0, hlen)
        ctx.memory_seq_cp(src_seq, SEQ_WORKING, -1, -1)
        inst.input_ids[:hlen] = np.array(toks, dtype=np.intc)
        inst._reasoning_current = level
        self._h_reasoning_swaps += 1
        log.info(
            "🧠 reasoning head-splice → %s (per-turn, %d tok head, %d tok body intact)",
            level, hlen, n_tokens - hlen,
        )
        return True

    def _completion_reasoning_head(
        self, inst: Any, prompt_tokens: List[int], level: str
    ) -> List[int]:
        """Per-request reasoning level for STATELESS completions.

        Sessions get the head via turn-0 install / mid-session splice; a
        completion re-forks the pristine static every request, so the same
        whole-seq install is sound here — at request start nothing sits above
        the head. Two coordinated moves keep the continuation prefix-match
        exact (and thus keep the zero-re-prefill property):

        1. ``_install_reasoning_head`` — the working seq's cache now holds the
           level's pinned head (and ``input_ids[:hlen]`` its tokens);
        2. the SAME head tokens replace ``prompt_tokens[:static_len]`` — so the
           eval path sees cache == prompt head and evals only the dynamic tail.

        Returns the (possibly head-swapped) prompt tokens. Refuses (returning
        the input unchanged) whenever any precondition fails: default level,
        swap disabled, non-resident, flow-prefix caching in play (its pinned
        KV assumes the default head), or length mismatches.
        """
        if not level or level == self._reasoning_default_level:
            return prompt_tokens
        if not (self._reasoning_head_swap and getattr(self, "_resident_active", False)):
            log.debug("completion reasoning=%s ignored (swap off or non-resident)", level)
            return prompt_tokens
        source = self._reasoning_head_source(inst, level)
        if source is None:
            log.warning("completion reasoning=%s: no pinned head — ignored", level)
            return prompt_tokens
        _seq, head_toks, hlen = source
        static_len = int(getattr(inst, "_static_len", self._resident_static_len) or 0)
        if hlen != static_len or len(prompt_tokens) < static_len or static_len <= 0:
            log.warning(
                "completion reasoning=%s refused: head len %d vs static %d (prompt %d)",
                level, hlen, static_len, len(prompt_tokens),
            )
            return prompt_tokens
        if not self._install_reasoning_head(inst, level):
            return prompt_tokens
        return list(head_toks) + list(prompt_tokens[static_len:])

    def _window_resident_seq(self, inst: Any, n_keep: int) -> int:
        """Slide the resident session window: drop the oldest ~half of the live
        conversation — positions [n_keep, n_keep+n_discard) — and shift the recent
        tail down by n_discard, keeping the static head [0, n_keep) intact. Lets a
        deep session run PAST n_ctx (the oldest turns fall out of context) instead
        of the backend raising at the context-window guard. Only the tail BEYOND
        the static head is shifted, so the static-prefix positions (pos_min=0) are
        never touched (the seq_add corruption the reasoning strip warned about hit
        spans that included the head). Returns the new n_tokens."""
        if self._decode_mode == "batched":
            return self._engine.window_seat_sync(inst, n_keep)
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

    @staticmethod
    def _guard_snapshot_persona(inst: Any, key: str, entry: dict) -> None:
        """Refuse a cross-persona snapshot restore: splicing persona-B's static
        head under persona-A's dynamic tokens silently corrupts the context."""
        snap_persona = entry.get("persona", "default")
        inst_persona = getattr(inst, "_persona", "default")
        if snap_persona != inst_persona:
            raise RuntimeError(
                f"snapshot {key!r} belongs to persona '{snap_persona}' but this "
                f"instance carries '{inst_persona}' — cross-persona restore refused"
            )

    def snapshot_working_seq(self, inst: Any, key: str) -> dict:
        """Pin the live working seq's KV under ``key`` (hot) and record the
        dynamic token stream (cold). The seq_cp shares cells (no copy); the
        snapshot owns them alone once the working seq moves on. Registry entry
        survives session end; only purge_snapshot frees it."""
        if self._decode_mode == "batched":
            raise RuntimeError(
                "session snapshots are pool-only in batched mode v1 "
                "(decode_mode: pool for snapshot workloads)"
            )
        if not self._resident_active:
            raise RuntimeError("resident cache inactive — use the replay fallback")
        if key in self._snap_registry:
            raise RuntimeError(f"snapshot key {key!r} already exists — purge first")
        n_tokens = int(inst.n_tokens)
        static_len = int(getattr(inst, "_static_len", self._resident_static_len) or 0)
        # Capacity: static + snapshots LIVE ON THIS INSTANCE + this
        # candidate must leave generation headroom in the shared n_ctx
        # cell pool. Live pins (inst._snap_seqs), not registry flags —
        # a refresh demotes pins but leaves registry entries, and stale
        # flags would brick snapshot creation until restart.
        live = sum(
            len(self._snap_registry[k]["dyn_tokens"])
            + self._snap_registry[k]["static_len"]
            for k in getattr(inst, "_snap_seqs", {})
            if k in self._snap_registry
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
            # A snapshot's dyn_tokens are meaningful ONLY atop the static head
            # they were captured over — bind the persona so fork/rebuild can
            # refuse a cross-persona splice (multi-persona pooling).
            "persona": getattr(inst, "_persona", "default"),
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
        if self._decode_mode == "batched":
            raise RuntimeError(
                "session snapshots are pool-only in batched mode v1"
            )
        entry = self._snap_registry[key]
        self._guard_snapshot_persona(inst, key, entry)
        seq = inst._snap_seqs.get(key)
        if seq is None or not entry.get("resident"):
            return None
        ctx = inst._ctx
        static_len = int(entry["static_len"])
        n_total = static_len + len(entry["dyn_tokens"])
        static_tokens = getattr(inst, "_static_tokens", None)
        if static_tokens is None:
            static_tokens = self._resident_static_tokens
        ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
        ctx.memory_seq_cp(seq, SEQ_WORKING, -1, -1)
        inst.input_ids[:static_len] = np.array(static_tokens, dtype=np.intc)
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
        self._guard_snapshot_persona(inst, key, entry)
        self._resident_restore_static(inst)
        dyn = list(entry["dyn_tokens"])
        if dyn:
            inst.eval(dyn)
        n_total = int(inst.n_tokens)
        if self._resident_active:
            # Re-pin on backend CAPABILITY, not the entry's flag — the
            # refresh demotion just cleared that flag, and gating on it
            # would leave every snapshot permanently cold after the
            # first refresh (caught by test). Pinning a replay-born
            # entry on a resident backend is a pure win too.
            try:
                seq = inst._snap_seqs.get(key) or self._alloc_snap_seq(inst, key)
                inst._ctx.memory_seq_rm(seq, 0, -1)
                inst._ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
                entry["resident"] = True  # hot again after a refresh demotion
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
        cap = max(8, self._snapshot_max * 4)
        if len(self._snap_registry) >= cap:
            raise RuntimeError(
                f"snapshot registry at capacity ({cap}) — purge stale keys "
                f"(each replay entry holds its full token history in RAM)"
            )
        self._snap_registry[key] = {
            "dyn_tokens": [int(t) for t in dyn_tokens],
            "static_len": 0,
            "turn_count": 0,
            "created_at": time.time(),
            "resident": False,
        }
        log.info("📸 snapshot %r registered (replay mode, %d tok)", key, len(dyn_tokens))
        return {"tokens": len(dyn_tokens), "resident": False}

    def sweep_stale_snapshots(self, max_age_s: float) -> int:
        """Purge snapshots older than max_age_s — crash insurance.

        Snapshots survive session end/TTL/refresh BY DESIGN; the only
        normal free path is an explicit purge. A mission that dies
        between snapshot and purge would otherwise hold the entry (and
        its capacity slot) for the server's lifetime. Called by the
        session manager's orphan reaper. 0 disables.
        """
        if not max_age_s:
            return 0
        cutoff = time.time() - max_age_s
        stale = [
            k
            for k, e in self._snap_registry.items()
            if float(e.get("created_at") or 0) < cutoff
        ]
        for key in stale:
            log.warning(
                "🧹 sweeping stale snapshot %r (older than %.0fs — "
                "crash-orphaned? normal paths purge explicitly)",
                key,
                max_age_s,
            )
            self.purge_snapshot(key)
        return len(stale)

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

        # MULTI-INSTANCE METAL FIX (found 2026-07-12, duo n_ctx sweep): with
        # per-buffer MTLResidencySets (llama.cpp PR#11427, on by default),
        # TWO live contexts on one Metal device fail command buffers with a
        # FALSE "Insufficient Memory" (status 5) regardless of size — 2×8k
        # (288 MiB KV, ~50G headroom) died in 5 rounds; the identical config
        # with GGML_METAL_NO_RESIDENCY=1 soaked clean. Residency sets were
        # never designed for multi-context (upstream PR discusses none). Cost
        # of disabling: buffers become OS-evictable after ~1s idle (~250ms
        # re-wire on resume) — negligible next to a dead slot. Respect an
        # explicit operator setting; only auto-set for multi-slot pools.
        # Batched mode is exempt: ONE context regardless of slot count, so
        # residency sets are safe and the weights stay wired (a perf win).
        if (
            self._decode_mode == "pool"
            and self._pool_size > 1
            and "GGML_METAL_NO_RESIDENCY" not in os.environ
        ):
            os.environ["GGML_METAL_NO_RESIDENCY"] = "1"
            log.warning(
                "🔧 multi-instance pool: GGML_METAL_NO_RESIDENCY=1 auto-set "
                "(Metal residency sets break multi-context — false OOM latch)"
            )

        # Forward llama.cpp/ggml native logs (Metal command-buffer failures,
        # the has_error latch notice, buffer sizes) into our logger — the
        # null callback verbose=False installs would swallow them.
        _install_ggml_log_forwarding()

        mode = "JIT" if self._jit_enabled else "eager"
        limit = self._jit_limit if self._jit_enabled else self._pool_size
        log.info(f"🔥 Initializing Llama pool ({mode} mode, " f"limit={limit})")

        # Create the primary instance (loads model weights once).
        self._primary_instance = self._create_primary_instance()
        self._is_hybrid = self._check_is_hybrid(self._primary_instance)

        if self._is_hybrid:
            log.info("🧬 Hybrid/recurrent model detected")

        # Batched mode preconditions that need the loaded model: the whole
        # engine is built on resident seq forking + per-seq positions, which
        # recurrent/hybrid state does not support (per-turn state surgery
        # corrupts it — see dev/CACHE_STATE.md architecture matrix).
        if self._decode_mode == "batched":
            if self._is_hybrid:
                raise RuntimeError(
                    "decode_mode 'batched' requires a plain-transformer model "
                    "— hybrid/recurrent architectures cannot host N working "
                    "sequences (use decode_mode: pool)"
                )
            if self._jit_enabled:
                raise RuntimeError(
                    "decode_mode 'batched' requires eager slots "
                    "(jit_concurrency_limit: null) — seats are seq ids, "
                    "there is nothing to JIT-spawn"
                )

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

        # Resolve the persona-per-slot assignment (multi-persona pooling).
        # Falls back to all-default for configs without slot_personas and for
        # test doubles whose config lacks the helper.
        try:
            self._slot_personas = self.config.slot_persona_names()
        except AttributeError:
            self._slot_personas = ["default"] * self._pool_size
        if self._jit_enabled and any(p != "default" for p in self._slot_personas):
            raise RuntimeError(
                "slot_personas requires eager pooling (jit_concurrency_limit: null) "
                "— JIT spawn/reap is not persona-aware"
            )
        if self._slot_personas[0] != "default":
            log.warning(
                "slot 0 persona is '%s' (not 'default') — the primary context is "
                "built at the model n_ctx; a persona n_ctx override on slot 0 is "
                "ignored", self._slot_personas[0],
            )

        if self._decode_mode == "batched":
            # ONE context, N working seqs, a decode thread — the pool
            # queue holds SeqSlot seats instead of Llama instances, so
            # acquire/release and every caller keep their shape.
            if not self._resident_active:
                raise RuntimeError(
                    "decode_mode 'batched' requires the resident-seq cache "
                    "to be ACTIVE (memory_can_shift) — this model cannot "
                    "host it (use decode_mode: pool)"
                )
            await run_in_threadpool(self._warm_batched)
            self._all_instances.append(self._primary_instance)
            self._register_instance(self._primary_instance, is_primary=True)
            self._pool_queue = asyncio.Queue(maxsize=self._pool_size)
            self._persona_queues = {"default": self._pool_queue}
            for seat in self._engine_seats:
                self._pool_queue.put_nowait(seat)
            log.info(
                "✅ Batched decode engine ready (%d seats, one context, "
                "personas=%s, n_seq_max=%d)",
                self._pool_size,
                sorted(self._engine._persona_heads),
                self._batched_seq_map().n_seq_max,
            )
            self._ready_event.set()
            # Proactive refresh loop: refresh_context's batched branch does
            # pause -> rebuild + re-pin -> resume, idle-gated by the same
            # _refresh_decision counters the pool uses.
            self._refresh_loop_task = asyncio.create_task(self._refresh_loop())
            return

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
            self._persona_queues = {"default": self._pool_queue}

            # Start the background scaler that tears down idle instances.
            self._scaler_task = asyncio.create_task(self._scaler_loop())

            log.info(
                f"✅ Llama pool ready (JIT mode, 1/{self._jit_limit} "
                f"instances, shared_model=True, hybrid={self._is_hybrid})"
            )
        else:
            # Eager mode: pre-allocate all instances at startup, each with its
            # slot's persona (and that persona's optional n_ctx override).
            for i in range(1, self._pool_size):
                persona = self._slot_personas[i]
                n_ctx_override = None
                try:
                    n_ctx_override = self.config.resolve_persona(persona).n_ctx
                except AttributeError:
                    pass
                ctx_started = time.perf_counter()
                inst = self._create_shared_instance(
                    self._primary_instance, n_ctx_override=n_ctx_override
                )
                log.info(
                    f"🔗 Created shared context for pool slot #{i} [{persona}"
                    f"{f', n_ctx={n_ctx_override}' if n_ctx_override else ''}] "
                    f"in {time.perf_counter() - ctx_started:.2f}s "
                    f"(shared model weights with primary)"
                )
                await run_in_threadpool(self._warm_up_instance, inst, i, persona)
                self._all_instances.append(inst)
                self._register_instance(inst)

            self._pool_queue = asyncio.Queue(maxsize=self._pool_size)
            # Per-persona idle queues: "default" aliases _pool_queue (legacy
            # paths untouched); named personas get their own queue.
            self._persona_queues = {"default": self._pool_queue}
            for persona in set(self._slot_personas):
                if persona != "default":
                    self._persona_queues[persona] = asyncio.Queue(maxsize=self._pool_size)
            for inst in self._all_instances:
                self._persona_queues[getattr(inst, "_persona", "default")].put_nowait(inst)

            log.info(
                f"✅ Llama pool ready ({len(self._all_instances)} instances, "
                f"personas={self._slot_personas}, "
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

        if self._engine is not None:
            # Stop the decode thread first: it owns all context operations,
            # and closing the context underneath a live step is a crash.
            await run_in_threadpool(self._engine.shutdown)
            self._engine = None
            self._engine_seats = []

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

    async def acquire_instance(self, persona: Optional[str] = None) -> Any:
        """Acquire a Llama instance from the pool (async-safe).

        ``persona`` routes the checkout to that persona's idle queue
        (multi-persona pooling — e.g. "user_sim" leases the simulated-user
        slot). None/"default" preserves the pre-persona semantics exactly.

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

        persona_key = persona or "default"
        if self._decode_mode == "batched":
            # Any seat serves any persona — the fork at handout stamps the
            # persona head onto the seat's seq (no per-persona queues, no
            # slot starvation by construction).
            if persona_key not in self._engine._persona_heads:
                raise RuntimeError(
                    f"unknown persona '{persona_key}' — warmed personas: "
                    f"{sorted(self._engine._persona_heads)}"
                )
            queue = self._pool_queue
        else:
            queue = self._persona_queues.get(persona_key)
            if queue is None:
                raise RuntimeError(
                    f"unknown persona '{persona_key}' — pool personas: "
                    f"{sorted(self._persona_queues)}"
                )

        inst: Any = None

        # --- Fast path: grab an idle instance if available ----------
        try:
            inst = queue.get_nowait()
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
                    inst = queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass  # Fall through to slow path

        # --- Slow path: wait for a release ---------------------------
        if inst is None:
            try:
                inst = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                active = len(self._all_instances)
                raise RuntimeError(
                    f"All inference instances are busy [{persona_key}] — try "
                    f"again later (active={active}, limit={self._jit_limit})"
                )

        # Belt over the release-side heal: a flagged instance must never
        # serve (its Metal latch fails every decode) — heal before handout.
        # (Batched seats carry the flag too, but their heal is the engine
        # rebuild — Stage 3 — not the per-context refresh.)
        if self._decode_mode != "batched" and getattr(
            inst, "_needs_context_refresh", False
        ):
            await self._heal_instance(inst)

        # Pool-membership accounting only. GPU-busy tracking lives in
        # generation_guard — a checkout (e.g. a session pinned between
        # turns) is not GPU work and must not block scaling drains.
        self._checked_out += 1

        # Restore seq 0 to the pristine post-static-tokens state. Guard it like
        # any other GPU work. Resident: fork SEQ_STATIC → seq 0 (intra-context
        # copy). Legacy: load_state the (multi-GB) snapshot blob. Batched:
        # fork the persona head onto the seat's seq via a control op (the
        # decode thread owns all KV surgery, applied between steps).
        if self._decode_mode == "batched":
            seat, engine = inst, self._engine
            async with self.generation_guard():
                await asyncio.wrap_future(
                    engine.control(lambda: engine.prepare_seat(seat, persona_key))
                )
            log.debug(
                "🧩 Forked persona head [%s] → seat seq %d (batched)",
                persona_key, seat.seq,
            )
        elif self._resident_active:
            async with self.generation_guard():
                await run_in_threadpool(self._resident_restore_static, inst)
            log.debug("🧩 Forked SEQ_STATIC → seq 0 before request (resident)")
        else:
            state = self._static_states.get(getattr(inst, "_persona", "default"))
            if state is not None:
                async with self.generation_guard():
                    await run_in_threadpool(inst.load_state, state)
                log.debug("🔄 Restored static state snapshot before request")

        log.debug(
            "🔧 Acquired instance [%s] (idle=%d, total=%d, checked_out=%d)",
            persona_key,
            queue.qsize(),
            len(self._all_instances),
            self._checked_out,
        )
        return inst

    async def release_instance(self, inst: Any) -> None:
        """Return a used instance back to the pool (async-safe).

        Decrements the checkout counter and stamps the instance's
        last-released time for LRU reaping. Drain synchronisation is
        handled by generation_guard, not here. The instance returns to
        ITS persona's queue (multi-persona routing).

        A decode-poisoned instance (Metal error latch) is HEALED here —
        context rebuilt + persona re-warm — before rejoining the pool, so a
        fatal decode costs one turn, not the slot.
        """
        if self._decode_mode == "batched":
            # Seat return: clear its seq (control op) and requeue. A dead
            # seat (engine fatal) still requeues — the next prepare_seat
            # re-forks onto a rebuilt context (Stage 3) or errors loudly.
            seat, engine = inst, self._engine
            try:
                await asyncio.wrap_future(
                    engine.control(lambda: engine.clear_seat(seat))
                )
            except Exception as exc:  # noqa: BLE001 — return the seat regardless
                log.warning("⚠️ seat clear failed on release: %s", exc)
            if self._pool_queue is not None:
                await self._pool_queue.put(seat)
            self._checked_out = max(0, self._checked_out - 1)
            log.debug(
                "🔧 Released seat seq %d (idle=%d, checked_out=%d)",
                seat.seq,
                self._pool_queue.qsize() if self._pool_queue else 0,
                self._checked_out,
            )
            return
        if getattr(inst, "_needs_context_refresh", False):
            await self._heal_instance(inst)
        if self._pool_queue is not None:
            queue = self._persona_queues.get(
                getattr(inst, "_persona", "default"), self._pool_queue
            )
            await queue.put(inst)

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

        # Per-request reasoning level for STATELESS completions. Only the
        # completion path ever puts "reasoning" in generate kwargs (the session
        # layer consumes it before generate — see session_manager gen_kwargs),
        # so this cannot fire mid-session. Installs the level's pinned head AND
        # swaps the prompt's head tokens together, keeping the continuation
        # prefix-match exact (zero extra prefill).
        _reasoning = kwargs.pop("reasoning", None)
        if _reasoning:
            prompt_tokens = self._completion_reasoning_head(
                instance, prompt_tokens, str(_reasoning)
            )

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
        session_mode = stop_texts is not None
        if stop_texts is None:
            stop_texts = get_renderer(self.config.model.family).stop_tokens()
        stop_bytes = [s.encode("utf-8") for s in stop_texts]

        # Final-channel completion detector (Harmony session mode only). Harmony
        # converts the <|return|> terminator to the history-form <|end|> once a
        # turn ages into the KV, so a deep session never sees <|return|> and the
        # model closes its answer with <|end|> and keeps generating (the
        # astropy-2 runaway). A stateless stop can't distinguish that close from
        # the legal analysis→final reopen; this stateful detector terminates the
        # turn when a NON-EMPTY final message closes. Off for completions (a
        # single final is the whole answer, and <|return|> fires normally) and
        # for non-Harmony families (their gen_stop has no history-form collision).
        final_stop = None
        if session_mode and self.config.model.family == "harmony":
            from inference.final_channel_stop import FinalChannelStop

            final_stop = FinalChannelStop()
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

                # Harmony session terminator: stop when a non-empty final-channel
                # message closes (the answer is complete; anything further is
                # self-play). Stateful — see FinalChannelStop. Runs on the full
                # accumulator (channel state spans the turn, not just the tail).
                if final_stop is not None and final_stop.update(acc_bytes):
                    should_stop = True
                    gen_end_reason = "final_channel_close"
                    self._h_final_channel_stops += 1
                    log.debug(
                        "🛑 final-channel stop: turn ended at non-empty final "
                        "close (%d tokens)",
                        len(completion_tokens),
                    )

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
            # content). The accumulator already holds everything. Preserve a
            # specific in-loop reason (e.g. final_channel_close) — only the
            # plain budget/EOG exits fall through to "completed".
            if gen_end_reason is None:
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
        except RuntimeError as e:
            # llama_decode -3 (GGML_STATUS_FAILED) latches the Metal backend:
            # ggml-metal sets a sticky has_error on any failed command buffer
            # and every later decode on this CONTEXT fails until it is
            # recreated. Mark the instance so release/acquire heals it via a
            # targeted context refresh; -2 (alloc failed) gets the same cure.
            if "code -3" in str(e) or "code -2" in str(e):
                self._mark_decode_failure(instance, e)
            raise
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
            if self._decode_mode == "batched":
                parts: List[str] = []
                async for chunk in self._batched_stream(
                    instance, prompt_tokens, max_tokens, temperature, **kwargs
                ):
                    parts.append(chunk)
                return "".join(parts)
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
            if self._decode_mode == "batched":
                async for chunk in self._batched_stream(
                    instance, prompt_tokens, max_tokens, temperature, **kwargs
                ):
                    yield chunk
                return
            async for chunk in iterate_in_threadpool(
                self.generate_stream_sync(
                    instance, prompt_tokens, max_tokens, temperature, **kwargs
                )
            ):
                yield chunk

    async def _batched_stream(
        self,
        seat: Any,
        prompt_tokens: List[int],
        max_tokens: int,
        temperature: float,
        stop_texts: List[str] | None = None,
        **kwargs,
    ) -> AsyncGenerator[str, None]:
        """Batched-mode generation: submit a StreamRequest to the decode
        engine and relay its OutputBridge. Mirrors generate_stream_sync's
        request shaping (static/dynamic split, stop defaults, telemetry
        contract); the per-token loop itself lives in the engine.
        """
        from core.generation_tracker import get_tracker
        from formats.registry import get_renderer
        from inference.batched_engine import OutputBridge, StreamRequest

        tracker = get_tracker()

        static_in_prompt = kwargs.pop("static_in_prompt", True)
        # Flow-prefix pinning is pool-only in v1 (documented deferral): the
        # persona head already delivers the dominant prefill saving.
        flow_key = kwargs.pop("flow_key", None)
        kwargs.pop("flow_prefix_len", None)
        if flow_key:
            log.debug("flow_kv_cache is pool-only — ignoring flow_key %r", flow_key)

        # Per-request reasoning level for STATELESS completions (batched
        # parity with generate_stream_sync). Session turns never carry the
        # kwarg. Preconditions: fresh seat (only its head in KV — the same
        # contract the static split below already assumes), pinned level head
        # of equal length. Install head + swap prompt head tokens together so
        # the split stays exact; restore the persona head afterward so the
        # next request on this seat sees its contract intact.
        _reasoning = kwargs.pop("reasoning", None)
        _restore_head = None
        if _reasoning and str(_reasoning) != self._reasoning_default_level:
            _level = str(_reasoning)
            _persona_head = self._engine._persona_heads.get(
                getattr(seat, "persona", "default")
            )
            _level_head = self._engine._reasoning_heads.get(_level)
            if (
                self._reasoning_head_swap
                and static_in_prompt
                and _level_head is not None
                and _persona_head is not None
                and _level_head.n_tokens == seat.static_len
                and int(seat.n_tokens or 0) == int(seat.static_len)
                and len(prompt_tokens) >= seat.static_len
            ):
                self._engine.install_head_sync(seat, _level_head)
                prompt_tokens = list(_level_head.tokens) + list(
                    prompt_tokens[_level_head.n_tokens:]
                )
                _restore_head = _persona_head
                self._h_reasoning_swaps += 1
                log.info(
                    "🧠 completion head-swap → %s (seat seq %d, batched)",
                    _level, seat.seq,
                )
            else:
                log.debug("completion reasoning=%s refused (batched preconditions)", _level)

        n_static = seat.static_len if static_in_prompt else 0
        if n_static > len(prompt_tokens):
            n_static = 0  # safety fallback (pool parity)
        dynamic_tokens = list(prompt_tokens[n_static:])
        kv_base = n_static if static_in_prompt else int(seat.n_tokens or 0)

        session_mode = stop_texts is not None
        if stop_texts is None:
            stop_texts = get_renderer(self.config.model.family).stop_tokens()

        sampling_kwargs = self._build_generate_kwargs(temperature)
        sampling_kwargs.pop("reset", None)
        grammar = kwargs.pop("grammar", None)
        request_id = kwargs.get("request_id", "")

        tracker.start(request_id=request_id, prompt_tokens=len(prompt_tokens))
        bridge = OutputBridge(asyncio.get_running_loop())
        req = StreamRequest(
            prompt_tokens=dynamic_tokens,
            max_tokens=max_tokens,
            sampling_kwargs=sampling_kwargs,
            out=bridge,
            persona=getattr(seat, "persona", "default"),
            slot=seat,
            stop_texts=list(stop_texts),
            session_mode=session_mode,
            grammar=grammar,
            request_id=request_id,
            temperature=temperature,
            kv_base=kv_base,
        )
        stream_id = self._engine.submit(req)
        log.info(
            "🔧 batched stream %s: dynamic=%d tok, kv_base=%d, max_gen=%d "
            "[seq %d, %s]",
            stream_id, len(dynamic_tokens), kv_base, max_tokens,
            seat.seq, req.persona,
        )
        completed = False
        try:
            async for chunk in bridge:
                yield chunk
            completed = True
        finally:
            if not completed:
                # Consumer abandoned (watchdog cancel / disconnect) — the
                # engine retires the stream and captures the partial text.
                bridge.closed = True
                self._engine.cancel(stream_id)
            if _restore_head is not None:
                # Put the persona head back so the seat honors its fresh-seat
                # contract for the next request.
                try:
                    self._engine.install_head_sync(seat, _restore_head)
                except Exception:  # noqa: BLE001 — restore is best-effort
                    log.warning("persona head restore failed (seat seq %d)", seat.seq)
            tracker.finish()

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
        # Sum across persona queues (single-persona: just _pool_queue).
        available = sum(q.qsize() for q in self._persona_queues.values()) if (
            self._persona_queues
        ) else (self._pool_queue.qsize() if self._pool_queue else 0)
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
            # Per-persona idle counts (multi-persona pooling readiness).
            "personas": {
                name: q.qsize() for name, q in sorted(self._persona_queues.items())
            },
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
        info["decode_mode"] = self._decode_mode
        if self._engine is not None:
            # Batched engine internals; a fatal latch flips overall status
            # so dashboards/soaks see the outage without new fields.
            engine_health = self._engine.health()
            info["batched_engine"] = engine_health
            if engine_health.get("engine_fatal"):
                info["status"] = "error"

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
        info["decode_failures"] = self._h_decode_failures
        info["latch_heals"] = self._h_latch_heals
        info["final_channel_stops"] = self._h_final_channel_stops
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
        if self._decode_mode == "batched":
            # Deferred in batched v1: the strip needs a tail seq_rm + a
            # mini-prefill ON the seat's seq, which means an eval path the
            # engine doesn't expose yet (control op + prefill-only stream).
            # Off in production; sessions simply keep raw turns.
            log.debug("resident_strip_reasoning is pool-only — skipped (batched)")
            return False
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
