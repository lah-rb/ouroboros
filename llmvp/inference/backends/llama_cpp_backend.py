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
from core.config import resolve_working_seats
import os
import time
from collections import OrderedDict
from typing import Any, AsyncGenerator, Dict, Iterator, List, Optional

import anyio
import numpy as np

from starlette.concurrency import iterate_in_threadpool, run_in_threadpool

from .base import BaseBackend, BackendCapabilities
from inference.repetition import DegenerateGenerationError, RepetitionGuard
from inference.decode_constants import BUFFER_MODE_MAX_TOKENS
from inference.token_pipeline import TokenPipeline, build_capture_meta

log = logging.getLogger("llm-mvp")

# Reserved seq ids for the resident in-context cache (config.model.resident_seq_cache).
# Each pool context handles ONE working stream, so generation stays on seq 0 (where the
# high-level Llama.generate()/eval() operate) — we reuse the existing generation machinery
# verbatim. SEQ_STATIC holds the pristine static prefix, forked onto SEQ_WORKING per request.
from inference.seq_layout import (  # noqa: E402 — single source of the pool band layout; sits below the llama_cpp import guard by design
    SEQ_FLOW_BASE,
    SEQ_STATIC,
    SEQ_WORKING,
    plan_pool_seq_map,
)

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

# Fraction of physical memory a KV+weights allocation may claim.
#
# MEASURED, on four models across three architectures and four quants
# (2026-07-29, `--probe-context`). Each was walked to the largest n_ctx that
# would load AND decode, then its weights + live KV expressed as a fraction of
# the 137.4GB physical:
#
#     gemma-4-31b       119.7GB   0.871      dense, Q4_K_XL
#     laguna-poolside   120.6GB   0.878      MoE, Q4_K_M
#     step-3.7-flash    120.8GB   0.879      MoE, IQ4_XS
#
# Three architectures landing inside 0.008 of each other is a real constant,
# not a coincidence, and 0.87 sits at or just below the lowest of them.
#
# The first value here was 0.90, inferred from a single crash (a computed
# 143.7GB against 137.4 physical hard rebooted the machine). That was too
# generous by exactly the margin the probes then found: every opening rung
# computed at 0.90 FAILED, on all four models, by 6-10%.
_PHYSICAL_SAFETY_FRACTION = float(os.environ.get("OURO_PHYSICAL_SAFETY", "0.87"))


def _physical_memory_gb() -> float:
    """Physical RAM in DECIMAL GB — the same units _kv_preflight compares in.

    Sizing a budget from the binary figure lands ~7% short, which on this
    machine is larger than the entire margin between a clean load and a reboot.
    """
    try:
        import psutil

        return psutil.virtual_memory().total / 1e9
    except Exception:  # noqa: BLE001 — fall back rather than skip the guard
        try:
            import subprocess

            out = subprocess.run(
                ["sysctl", "-n", "hw.memsize"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return int(out.stdout.strip()) / 1e9
        except Exception:  # noqa: BLE001
            # Unknown physical memory must not silently disable the ceiling.
            # 128GB is this fleet's floor; a wrong-but-present bound beats none.
            log.warning(
                "⚠️ could not read physical memory — KV preflight "
                "ceiling assuming 128GB"
            )
            return 128.0


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
                    log.log(
                        _GGML_LOG_LEVELS.get(level, logging.DEBUG),
                        "ggml: %s",
                        line.strip(),
                    )
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
        self._pool_size = resolve_working_seats(config.resources)
        # Concurrency architecture: "pool" (N contexts) vs "batched" (ONE
        # context, N working seqs, one llama_decode per step — the
        # llama-server slot pattern). Config-gated; the pool path is not
        # modified when "batched" is off. See inference/batched_engine.py.
        self._decode_mode: str = getattr(config.resources, "decode_mode", "pool")
        self._engine: Any = None  # BatchedEngine when _decode_mode=="batched"
        self._engine_seats: List[Any] = []  # SeqSlot seats (batched mode)
        self._batched_map: Any = None  # memoized SeqMap (batched mode)
        self._all_instances: List[Any] = []  # For shutdown cleanup
        # The mtmd vision instance, built on first vision request. NOT in
        # _all_instances and NOT in any queue — it must never be handed out as
        # a pool slot (see _create_vision_instance for why that would silently
        # corrupt the text path's KV bands). Shutdown closes it explicitly.
        self._vision_instance: Optional[Any] = None
        # The vision POOL: N private single-seq contexts, checked out one
        # owner at a time. None until the first vision request builds it.
        self._vision_instances: list = []
        self._vision_pool: Optional[asyncio.Queue] = None
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
        # (The M8 save_state-blob flow cache — self._flow_states — was deleted
        # 2026-07-30; the seq-ops mechanisms below are the only flow caches.)
        # Resident in-context sequence cache (opt-in: config.model.resident_seq_cache).
        # When active, the pristine static prefix lives on SEQ_STATIC and is forked
        # (memory_seq_cp) onto SEQ_WORKING=0 per request instead of load_state'd from a
        # blob. _resident_active is set in initialize() after the can_shift gate (forced
        # off for pure-recurrent models, which lack memory_can_shift()).
        self._resident_requested = bool(
            getattr(getattr(config, "model", None), "resident_seq_cache", False)
        )
        self._resident_active = False
        # Whether the ARCH can host the resident cache, asked at load whether or
        # not we requested it. None = not yet asked / unaskable.
        #
        # WHY UNCONDITIONALLY: the can_shift gate below used to run only when
        # resident was REQUESTED, so a config with resident_seq_cache: false
        # produced no evidence either way and the answer stayed unknown. hy3 sat
        # in that state through a full tier arm, paying O(n^2) session re-prefill
        # (60.8% of run wall at 58min) while nobody could say whether the flat
        # path was even available to it. An unasked question is not a measurement.
        self._session_can_shift: Optional[bool] = None
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
        # CONSECUTIVE fatal decodes that a context rebuild did NOT fix. This is
        # the number that distinguishes a healable fault from an unservable
        # configuration, and it is the crash predictor.
        #
        # -3 has several causes and most of them heal: SWA-boundary corruption
        # on a pinned prefix, save_state churn, a one-off command-buffer
        # failure. _heal_instance rebuilds the context, the latch clears, work
        # resumes. Killing on the FIRST -3 would take the server down for
        # conditions it currently recovers from.
        #
        # What does NOT heal is a configuration whose decode cannot fit. On
        # 2026-07-28 the Hy3 ladder saw exactly that at n_ctx 49152 and 65536 —
        # both loaded, both reported healthy, both failed EVERY decode with -3 —
        # and the next rung up rebooted the machine while holding 108 GB of
        # weights. A heal-then-fail-again loop is that state, and it is a
        # LEADING indicator: it appeared two rungs before the crash, while the
        # machine was still entirely healthy.
        self._consecutive_unhealed_decode_failures = 0
        self._unservable = False
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
        # Batched snapshot band: key -> snapshot seq id on the single context.
        # (Pool mode uses per-instance inst._snap_seqs instead.)
        self._batched_snap_seqs: Dict[str, int] = {}
        self._h_snapshot_rebuilds = 0
        # Per-request reasoning HEAD-SWAP (config.model.reasoning_head_swap). Pin a
        # system head per reasoning level on a band ABOVE the snapshot band; a
        # session turn carrying reasoning=<level> forks that head onto the live seq
        # at turn 0. Family-general since 2026-08-16: harmony qualifies via its
        # inline steer, any other family by declaring a reasoning.levels map
        # (muse-glimmer was the first port — its Reasoning-strength line lives
        # in the cached system head, the same slot harmony swaps). The default level (thinking_mode) reuses SEQ_STATIC, so only
        # the OTHER levels get a pinned head. Per-instance presence (_reasoning_seqs).
        _model_cfg = getattr(config, "model", None)
        # Canonical level order. The per-family subset is derived from the
        # format's reasoning.levels map below — a family that declares xhigh
        # (muse-glimmer, per its card) gets an xhigh head; harmony (no map,
        # inline steer) keeps the trio. An unmapped requested level degrades
        # to no-swap via _reasoning_heads.get() -> None.
        self._reasoning_levels = ["low", "medium", "high", "xhigh"]
        self._reasoning_default_level = getattr(_model_cfg, "thinking_mode", None)
        # Distinct pinned heads are keyed by RENDERED TEXT, not level name: a
        # BIMODAL family collapses two canonical levels onto one text (Gemma-4:
        # low+medium -> the padding, high -> <|think|>), so it costs ONE extra
        # head, not two.
        _fam = getattr(_model_cfg, "family", "") or ""
        _level_map: dict = {}
        try:
            from formats.registry import get_renderer as _get_renderer

            _level_map = dict(_get_renderer(_fam).s.reasoning.levels or {})
        except Exception:  # noqa: BLE001 — a missing/invalid spec just means no map
            _level_map = {}
        if _level_map:
            self._reasoning_levels = [
                lv for lv in self._reasoning_levels if lv in _level_map
            ] or ["low", "medium", "high"]
        else:
            # No map (harmony's inline steer): xhigh is not a trained value
            # there — keep the trio.
            self._reasoning_levels = ["low", "medium", "high"]
        _default_text = _level_map.get(
            self._reasoning_default_level, self._reasoning_default_level
        )
        _seen: set = set()
        self._reasoning_pin_levels = []
        for lv in self._reasoning_levels:
            if lv == self._reasoning_default_level:
                continue
            text = _level_map.get(lv, lv)
            if text == _default_text or text in _seen:
                continue  # renders identically to the default / an earlier head
            _seen.add(text)
            self._reasoning_pin_levels.append(lv)
        # Head-swap needs the reasoning steer to live in the CACHED system head.
        # harmony expresses it inline ("Reasoning: <level>"); any other family
        # qualifies by declaring a reasoning.levels map (formats/*.yaml), which
        # is what makes its levels renderable into that head. The mid-session
        # splice independently verifies equal head length and refuses a
        # mismatched swap, so a badly-padded map degrades to "no swap".
        self._reasoning_head_swap = (
            self._resident_requested
            and bool(getattr(_model_cfg, "reasoning_head_swap", False))
            and (_fam == "harmony" or bool(_level_map))
        )
        # GATE-LEVEL families (gemma, hy3, ...): thinking is switched by the
        # PER-TURN generation-prompt gate (gate_levels), not by which system
        # head a session baked — and for gemma the head carries the
        # activation token itself, so a session that bakes the low head at
        # turn 0 loses <|think|> for its whole life. Measured 2026-08-21
        # (gemma-4-31b gate arm): plan_interaction routes explicit low, it is
        # turn 0 of every session, and the arm produced 0 CoT emissions in
        # 117 calls where the 08-02 pre-head-swap arm thought 36 times. For
        # these families the session-level head machinery must stand down:
        # the default head keeps the activation and the gate forecloses the
        # low TURNS. Stateless completions keep per-request installs — a
        # whole-prompt build renders the official bytes for its own level.
        _gate_levels: list = []
        try:
            _gate_levels = list(_get_renderer(_fam).s.thinking.gate_levels or [])
        except Exception:  # noqa: BLE001 — no renderer/spec = not gated
            _gate_levels = []
        self._reasoning_gate_family = bool(_gate_levels)
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
        # Batched seat reaper (see _seat_reaper_loop): backstop that reclaims
        # seats whose consumer vanished without releasing. _reaper_reclaimed
        # holds seat ids reclaimed by the reaper so a late duplicate release
        # (GC-finalized consumer) is ignored instead of double-requeueing.
        self._seat_reaper_task: Optional[asyncio.Task] = None
        self._reaper_reclaimed: set = set()
        # Seats currently inside a batched-vision install window (acquire ->
        # stream submit). Mutated only on the event loop (core/inference);
        # read by the reaper sweep on the same loop.
        self._vision_installing: set = set()
        self._refresh_interval = int(
            getattr(getattr(config, "model", None), "context_refresh_interval", 75)
            or 75
        )
        # Time-based backstop for the refresh loop. The request-count + idle gate alone
        # can starve under CONTINUOUS load: a long mission never goes idle, so the
        # opportunistic refresh waited ~665 requests / 1h40m for a gap and a run soured
        # in the meantime. This wall-clock cap fires the refresh even while busy (the
        # graceful drain just lets the in-flight generation finish — real work, not
        # overhead). Default 30 min.
        self._refresh_seconds = int(
            getattr(getattr(config, "model", None), "context_refresh_seconds", 1800)
            or 1800
        )
        # Drain window for refreshing under load (0 = legacy defer-while-busy,
        # which starves forever under continuous multi-mission load). When set,
        # the admission gate below closes, in-flight work gets drain_s to
        # finish, stragglers are force-cleared (sessions expire via their
        # normal listener path; streams retire retriable), then the context
        # rebuilds. See core/config.ModelConfig.context_refresh_drain_s.
        self._refresh_drain_s = float(
            getattr(getattr(config, "model", None), "context_refresh_drain_s", 0.0)
            or 0.0
        )
        # Admission gate — cleared while a drain-refresh is in progress so
        # acquire_instance() waiters queue instead of keeping the pool busy.
        self._refresh_admission_gate: asyncio.Event = asyncio.Event()
        self._refresh_admission_gate.set()
        # Set by SessionManager at construction (soft DI): force-expires all
        # live sessions through the normal expiry path at the drain deadline.
        self._session_expirer = None
        # Post-force-clear settle window (tests shrink it).
        self._refresh_settle_s = 30.0
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
    # llama.cpp LLAMA_SPLIT_MODE_*: 0 NONE, 1 LAYER, 2 ROW. Mapped by NAME
    # in config because a bare integer in YAML is unreviewable.
    _SPLIT_MODES = {"none": 0, "layer": 1, "row": 2}

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
            ngram_size=int(
                getattr(self.config.model, "speculative_ngram_size", 3) or 3
            ),
            num_pred_tokens=int(
                getattr(self.config.model, "speculative_num_pred", 10) or 10
            ),
        )

    def _create_primary_instance(self) -> Any:
        """Create the primary Llama instance that owns the model weights."""
        Llama = self._get_llama_class()

        # DEVICE PLACEMENT. Passed only when the config asks for it, so a
        # single-GPU host and every existing config keep llama.cpp's defaults
        # byte for byte. `main_gpu` names the device; `split_mode` must ALSO be
        # "none" or LAYER split still spreads the model over every visible card
        # and the pin does nothing.
        placement: dict = {}
        _main_gpu = getattr(self.config.model, "main_gpu", None)
        if _main_gpu is not None:
            placement["main_gpu"] = int(_main_gpu)
        _split = getattr(self.config.model, "split_mode", None)
        if _split is not None:
            placement["split_mode"] = self._SPLIT_MODES[str(_split).lower()]
        _tsplit = getattr(self.config.model, "tensor_split", None)
        if _tsplit:
            # Deterministic per-device proportions (see config.py): the
            # free-VRAM-proportional default moves with whatever else is
            # resident at load, which makes an OOM ladder unrepeatable.
            placement["tensor_split"] = [float(x) for x in _tsplit]

        return Llama(
            model_path=str(self.config.model.path),
            n_ctx=self.config.model.n_ctx,
            n_gpu_layers=self.config.model.n_gpu_layers,
            **placement,
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
            # band adds one per pinnable session snapshot. NOT harmless when the
            # can_shift gate later disables the resident path: without
            # kv_unified, llama.cpp splits n_ctx per sequence, so the band
            # fragments the window — the gate un-fragments by rebuilding the
            # context single-seq on fallback (see initialize()). Batched mode
            # uses its own layout: W working seats + persona heads +
            # reasoning heads (see inference/batched_engine.plan_seq_map).
            n_seq_max=(
                self._batched_seq_map().n_seq_max
                if self._decode_mode == "batched"
                else self._pool_seq_map().n_seq_max if self._resident_requested else 1
            ),
            seed=self.config.model.seed,
            verbose=self.config.model.verbose,
            n_threads=self.config.resources.cpu_threads,
            # batch_size was a silent no-op; n_batch is the real param (the prior
            # effective value was the 2048 default). draft_model wires the binding's
            # native n-gram speculative decoding (gated by config.model.speculative).
            n_batch=int(getattr(self.config.model, "n_batch", 2048) or 2048),
            draft_model=self._make_draft(),
            # RoPE/YaRN overrides — omitted entirely unless a config sets them,
            # so the default path is byte-identical to before this existed.
            **self._rope_kwargs(),
        )

    # RoPE/YaRN params llama.cpp accepts. Each maps a config field of the same
    # name to the Llama() kwarg; unset (None) means "don't pass it", which
    # leaves llama.cpp's own resolution — GGUF metadata plus its defaults —
    # exactly as it was.
    _ROPE_FIELDS = (
        "rope_scaling_type",
        "rope_freq_base",
        "rope_freq_scale",
        "yarn_ext_factor",
        "yarn_attn_factor",
        "yarn_beta_fast",
        "yarn_beta_slow",
        "yarn_orig_ctx",
    )

    def _rope_kwargs(self) -> dict:
        """Explicit RoPE/YaRN overrides, and a record of what is in force.

        WHY THIS EXISTS. Laguna's GGUF declares
        ``rope.scaling.yarn_attn_factor = 1.4852`` while poolside's own guidance
        is 1.0, and llama-cpp-python's default is also 1.0 — so which value the
        model actually ran under was not merely unset, it was UNOBSERVABLE: we
        never passed the parameter and nothing logged the resolved value. The
        same GGUF asks for a 128x YaRN stretch (8192 -> 1M) that we never use at
        our working contexts.

        Both halves are fixed here: the params become settable, and the ones the
        GGUF declares are logged at load whether or not we override them, so the
        question "what is attn_factor right now" always has an answer.
        """
        out: dict = {}
        for field in self._ROPE_FIELDS:
            val = getattr(self.config.model, field, None)
            if val is not None:
                out[field] = val
        if out:
            log.info(
                "🧭 RoPE/YaRN overrides: %s",
                ", ".join(f"{k}={v}" for k, v in sorted(out.items())),
            )
        else:
            log.info(
                "🧭 RoPE/YaRN: no overrides — llama.cpp resolves from GGUF "
                "metadata and its own defaults"
            )
        return out

    def _create_shared_instance(
        self, primary: Any, n_ctx_override: Optional[int] = None
    ) -> Any:
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
        # Per-STREAM ceiling, not the allocation: session windowing keys off
        # _n_ctx, and a pool larger than the trained range (the swarm/model
        # context split) must not let a session grow past n_ctx_train — AND,
        # since 2026-07-29, past the TRUE per-seq window (n_ctx/n_seq_max under
        # kv_unified:false). Without the seq clamp the guard fired at n_ctx
        # while llama.cpp held 1/12th of that per sequence.
        _base_ctx = int(n_ctx_override) if n_ctx_override else primary._n_ctx
        _lim = self._stream_ctx_limit()
        _seq_lim = self._seq_ctx_limit(ctx, params)
        # CACHE the per-seq window on the instance (2026-08-09). n_ctx_seq is
        # immutable for a built context, so the health register must read this
        # int rather than re-probing the LIVE context: a status read that
        # lands mid-rebuild dereferences a freed pointer and SIGSEGVs the
        # whole server (three identical crash reports, top frame
        # libllama!llama_n_ctx_seq, KERN_INVALID_ADDRESS 0xc). A try/except
        # cannot catch a native segfault.
        inst._n_ctx_seq = _seq_lim
        inst._n_ctx = min(c for c in (_base_ctx, _lim, _seq_lim) if c)
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
    # Vision (mtmd) — a private single-seq instance, never in the pool
    # ------------------------------------------------------------------

    def _create_vision_instance(self, primary: Any) -> Any:
        """A private instance carrying the mtmd chat handler.

        WHY THIS IS NOT A POOL SLOT, AND MUST NEVER BECOME ONE.
        ``MTMDChatHandler.__call__`` is hard-coded to ``seq_id=0`` and, on a
        prefix mismatch, calls ``llama._ctx.memory_clear(True)`` — which clears
        EVERY sequence, not just its own. On a pooled instance that single call
        destroys SEQ_STATIC, the flow band, the snapshot band and every pinned
        reasoning head, and the text path keeps running afterwards producing
        wrong output with no error at all. Nothing downstream would catch it.

        Giving vision its own context with ``n_seq_max=1`` makes the handler's
        assumption TRUE instead of dangerous: seq 0 is the only sequence it
        can reach, and clearing it destroys nothing else.

        Weights are shared with the primary (``model=primary._model``) — the
        projector binds the MODEL, not a context (mtmd_init_from_file), so this
        costs one small context plus the mmproj, never a second weight load.

        The handler is attached HERE and never to the primary: pool slots are
        built with ``copy.copy(primary)``, which does not reset ``chat_handler``
        — it would be aliased into every slot with a single-owner ``close()``.
        """
        from llama_cpp import internals

        from inference.vision_handlers import load_handler_class

        mcfg = self.config.model
        n_ctx = int(getattr(mcfg, "vision_n_ctx", 8192) or 8192)

        # SHALLOW CLONE, explicitly — NOT copy.copy. On builds where Llama
        # defines __setstate__, copy.copy routes through the pickle reduce
        # protocol and RELOADS THE MODEL FROM DISK before we ever overwrite
        # the context: a second 19.6 GB weight upload that failed instantly
        # on the 24 GB card (muse vision 500s, 2026-08-16) and silently
        # doubled paddle's footprint per vision context. Unified-memory
        # mmap made the same reload invisible on the M1. Everything this
        # instance must own is overwritten right below; everything else —
        # the model above all — is deliberately shared.
        inst = object.__new__(type(primary))
        inst.__dict__.update(primary.__dict__)
        inst._stack = contextlib.ExitStack()

        # Own params: our own window AND a single sequence.
        params = type(primary.context_params).from_buffer_copy(primary.context_params)
        params.n_ctx = n_ctx
        params.n_seq_max = 1
        inst.context_params = params

        inst._ctx = internals.LlamaContext(
            model=primary._model, params=params, verbose=False
        )
        inst._n_ctx = n_ctx
        inst._n_ctx_seq = n_ctx
        inst._persona_n_ctx = None
        inst._batch = internals.LlamaBatch(
            n_tokens=primary.n_batch, embd=0, n_seq_max=1, verbose=False
        )
        inst.input_ids = np.ndarray((n_ctx,), dtype=np.intc)
        logits_rows = n_ctx if primary._logits_all else 1
        inst.scores = np.ndarray((logits_rows, primary._n_vocab), dtype=np.single)
        inst._candidates = internals.LlamaTokenDataArray(n_vocab=primary._n_vocab)
        inst.n_tokens = 0
        inst._mirostat_mu = ctypes.c_float(2.0 * 5.0)
        inst._sampler = None
        inst._sampling_ctx = None
        inst.cache = None
        inst._hybrid_cache_mgr = None
        # Speculative decoding is off for vision: the draft model has no
        # projector and the handler drives its own decode loop.
        inst.draft_model = None

        handler_cls = load_handler_class(
            mcfg.family, getattr(mcfg, "vision_handler", None)
        )
        # ONLY Generic takes chat_format. The family handlers carry a fixed
        # CHAT_FORMAT class attribute and REJECT the kwarg with a TypeError —
        # their __init__ is (force_reasoning, add_vision_id, **kwargs) over a
        # base of (mmproj_path, verbose, use_gpu, image_min/max_tokens).
        # PLACEMENT. The handler's signature carries only use_gpu — a bool, no
        # device index — so by default mtmd allocates on device 0 whatever
        # main_gpu says. But clip.cpp consults MTMD_BACKEND_DEVICE via getenv
        # at every mtmd_init_from_file and resolves it with
        # ggml_backend_init_by_name, so exporting it around construction is
        # per-model projector placement: paddle's projector can sit on CUDA1
        # beside its weights while muse keeps the default. use_gpu false keeps
        # the projector in host RAM. resident_models.footprint_by_device
        # charges whichever device this selects.
        handler_kwargs: dict = {
            "mmproj_path": str(mcfg.mmproj_path),
            "verbose": False,
            "use_gpu": bool(getattr(mcfg, "vision_projector_gpu", True)),
        }
        if handler_cls.__name__ == "GenericMTMDChatHandler":
            handler_kwargs["chat_format"] = None
        proj_dev = getattr(mcfg, "vision_projector_device", None)
        if proj_dev and handler_kwargs["use_gpu"]:
            prior = os.environ.get("MTMD_BACKEND_DEVICE")
            os.environ["MTMD_BACKEND_DEVICE"] = str(proj_dev)
            try:
                inst.chat_handler = handler_cls(**handler_kwargs)
                # EAGER, AND INSIDE THE ENV SCOPE, because construction does
                # not touch mtmd at all: _init_mtmd_context runs on the FIRST
                # VISION REQUEST (it needs the Llama object), long after a
                # construction-scoped env var is restored. The first version
                # of this scoped only the constructor, and the projector
                # landed on device 0 anyway — caught by per-process nvidia-smi
                # attribution (+1.3 GB on CUDA0), not by any error. The init
                # is guarded (`if self.mtmd_ctx is not None: return`), so the
                # request-path call becomes a no-op. Eager also matches the
                # governor, which charges the projector at admission rather
                # than at first use.
                init = getattr(inst.chat_handler, "_init_mtmd_context", None)
                if callable(init):
                    init(inst)
            finally:
                # Restore, never leak: the env var is process-wide and the
                # NEXT model's projector must not inherit this one's device.
                if prior is None:
                    os.environ.pop("MTMD_BACKEND_DEVICE", None)
                else:
                    os.environ["MTMD_BACKEND_DEVICE"] = prior
        else:
            inst.chat_handler = handler_cls(**handler_kwargs)
        log.info(
            "👁  Vision instance ready — handler=%s n_ctx=%d mmproj=%s",
            handler_cls.__name__,
            n_ctx,
            os.path.basename(str(mcfg.mmproj_path)),
        )
        return inst

    async def _build_vision_pool(self) -> None:
        """Build the vision contexts on first use (idempotent).

        Lazy so a config that declares a projector pays nothing until a vision
        request actually arrives. Built under BOTH the spawn lock (one builder)
        and the generation guard — creating a context while a generation is
        live on the same Metal device crashes ggml, which is the same reason
        _jit_batch_scale_up drains first.
        """
        if self._vision_pool is not None:
            return
        if not getattr(self.config.model, "mmproj_path", None):
            raise RuntimeError(
                "vision is not configured: set model.mmproj_path for "
                f"{self.config.model.name!r}"
            )
        async with self._spawn_lock:
            if self._vision_pool is not None:  # re-check under the lock
                return
            width = max(1, int(getattr(self.config.model, "vision_pool_size", 1) or 1))
            pool: asyncio.Queue = asyncio.Queue()
            built: list = []
            async with self.generation_guard():
                for _ in range(width):
                    inst = self._create_vision_instance(self._primary_instance)
                    built.append(inst)
                    pool.put_nowait(inst)
            self._vision_instances = built
            # Back-compat: single-instance callers and the teardown path.
            self._vision_instance = built[0]
            self._vision_pool = pool
            log.info(
                "👁️  vision pool ready: %d context(s) @ n_ctx %d",
                width,
                int(getattr(self.config.model, "vision_n_ctx", 8192) or 8192),
            )

    @contextlib.asynccontextmanager
    async def acquire_vision_instance(self):
        """Check out ONE vision context for the life of a request.

        THE EXCLUSION IS THE POINT, not just the concurrency. Before this,
        every vision request shared one instance and nothing serialized them:
        ``generation_guard`` is a COUNTING guard (it holds off drains and
        scaling, and deliberately lets generations run together), so two
        concurrent vision calls landed on the same Llama object and raced on
        the handler's own token ledger — ``n_tokens`` and ``input_ids`` — plus
        its KV. It never fired only because every caller so far is sequential:
        fig_review loops figures one at a time. A queue gives exactly one
        owner per context, so the race is gone at width 1 and the pool is
        genuinely parallel above it.

        The instance is returned even when the request raises; losing one to
        an exception would shrink the pool silently until vision deadlocked.
        """
        await self._build_vision_pool()
        assert self._vision_pool is not None
        inst = await self._vision_pool.get()
        try:
            yield inst
        finally:
            # CLEAR THE CONTEXT BEFORE THE NEXT OWNER TAKES IT. A vision
            # request is one image and one answer — there is no prefix worth
            # carrying over — but the handler's token ledger and KV persist on
            # the instance, and for a DEEPSTACK projector (qwen3vl_merger,
            # clip.vision.is_deepstack_layers) the position accounting drifts
            # across the image-embedding splice. Measured 2026-08-23 on
            # qwen3.8-27b: request 1 answered, requests 2+ died with
            # `find_slot: non-consecutive token position` ->
            # `mtmd_helper_eval_chunk_single: Media evaluation failed with
            # error code -1`, permanently, for the life of the process. Muse
            # tolerates the carry-over, which is why this went unseen: every
            # caller so far served one family.
            #
            # reset() alone is NOT enough — it zeroes the counter and leaves
            # the KV cells (same lesson as the session restart path), so the
            # memory is cleared explicitly where the binding exposes it.
            try:
                inst.reset()
                _ctx = getattr(inst, "_ctx", None)
                _clear = getattr(_ctx, "memory_clear", None)
                if callable(_clear):
                    _clear(True)
            except Exception:  # noqa: BLE001 — never lose an instance to cleanup
                log.debug("vision context clear failed", exc_info=True)
            self._vision_pool.put_nowait(inst)

    async def get_vision_instance(self) -> Any:
        """One vision instance, built on first use.

        RETAINED FOR CALLERS THAT DO NOT CHECK OUT. It hands back a shared
        instance with no exclusion, which is safe only for a caller that
        guarantees it is the sole in-flight vision request. Prefer
        ``acquire_vision_instance()``; this exists so the pre-pool signature
        keeps working.
        """
        await self._build_vision_pool()
        return self._vision_instance

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
            # Resolve against THIS backend's config, not the global active
            # one: a secondary model (e.g. paddle OCR beside muse) must
            # never be warmed with the active model's static head — that
            # fed muse's BOS 200000 into paddle's 103,424-token vocab and
            # flagged its context on every boot (2026-08-15). A secondary
            # whose family cannot build a head (no format spec — OCR-only
            # models) warms clean: empty is the correct head for it.
            try:
                static_tokens = get_static_tokens(persona, config=self.config)
            except Exception as head_exc:  # noqa: BLE001
                log.warning(
                    "⚠️ No static head for this config [%s] (%s) — warming "
                    "with a clean state",
                    persona,
                    head_exc,
                )
                static_tokens = []
            n_tokens = len(static_tokens)

            # Eval-site vocab cross-check: the model itself is the ground
            # truth. A mismatched head decodes into llama.cpp's fatal
            # out-of-vocab error and flags the context; catch it here and
            # degrade to a clean no-static warm-up instead.
            try:
                model_n_vocab = int(llm_inst.n_vocab())
            except Exception:  # noqa: BLE001
                model_n_vocab = 0
            if static_tokens and model_n_vocab and max(static_tokens) >= model_n_vocab:
                log.error(
                    "❌ Static head [%s] holds id %d >= model n_vocab %d — "
                    "foreign-vocab bin; warming without a static prefix "
                    "(rebuild it via preprocessing for this config)",
                    persona,
                    max(static_tokens),
                    model_n_vocab,
                )
                static_tokens = []
                n_tokens = 0

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
                # Seed the tracker's cold-prefill rate with this genuine
                # cache-free eval — the basis for health's advisory
                # expectedEvalSeconds (timed BEFORE save_state so the blob
                # write doesn't dilute the rate).
                try:
                    from core.generation_tracker import get_tracker

                    get_tracker().seed_prefill_rate(
                        n_tokens, time.perf_counter() - started
                    )
                except Exception:  # advisory only — never block boot
                    pass
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
                    state_mb = self._static_states[persona].llama_state_size / (
                        1024 * 1024
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
                    n_tokens,
                    persona,
                    idx,
                )
                # Reasoning HEAD-SWAP: pin a head per non-default level (re-pinned
                # here on every refresh/rewarm since this is the single warm path).
                self._pin_reasoning_heads(llm_inst)
        except Exception as exc:
            # Do NOT let a half-warmed slot serve silently (empty/partial
            # static head = wrong context on every request). Flag it for the
            # self-heal path — the next acquire rebuilds the context and
            # re-runs this warm path — instead of swallowing the failure.
            log.error(f"❌ Warm-up failed for pool slot #{idx}: {exc}")
            llm_inst._needs_context_refresh = True
            # A WARM-UP failure is the strongest unservable signal there is:
            # the slot could not prefill its own static prefix, before a single
            # request arrived. Routing it through the same accounting as a
            # generate-path failure is what lets a configuration that cannot
            # decode take itself out at BOOT rather than after user work fails.
            #
            # The Hy3 40960 rung is the case: -3 on a 1,783-token static eval,
            # a heal, then -1 "exceeding capacity" forever. The old code counted
            # nothing here (_mark_decode_failure is only called from the
            # generate path), so unhealed_decode_failures stayed 0 through four
            # failed generations and the switch never armed.
            self._mark_decode_failure(llm_inst, exc)

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
                snapshots=self._snapshot_max,
                # The stateless flow cache under batched (2026-07-30): band
                # sized by flow_kv_cache_max iff the flag is on. Under
                # kv_unified (a batched requirement) the band divides nothing.
                flow_slots=(
                    self._flow_hot_set
                    if getattr(self.config.model, "flow_kv_cache", False)
                    else 0
                ),
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
        if bool(getattr(self.config.model, "vision_batched", False)):
            # BATCHED VISION: a synthetic persona with an EMPTY head.
            # prepare_seat skips the memory_seq_cp entirely when a head
            # holds zero tokens, so no band seq is consumed (seq=-1 is
            # never dereferenced) and the seq map is untouched — a vision
            # stream starts from a genuinely bare seq and its whole
            # prompt (rendered by the FAMILY renderer, not SOUL.md) is
            # installed by inference/vision_batched.py.
            heads["vision"] = PersonaHead(name="vision", seq=-1, tokens=[])
        for persona, head_seq in seq_map.persona_seqs.items():
            tokens = list(get_static_tokens(persona, config=self.config))
            started = time.perf_counter()
            ctx.memory_seq_rm(SEQ_WORKING, 0, -1)
            primary.reset()
            if tokens:
                primary.eval(tokens)
                ctx.memory_seq_rm(head_seq, 0, -1)
                ctx.memory_seq_cp(SEQ_WORKING, head_seq, -1, -1)
            heads[persona] = PersonaHead(name=persona, seq=head_seq, tokens=tokens)
            log.info(
                "🧩 Pinned persona head [%s] — %d tokens on seq %d in %.2fs",
                persona,
                len(tokens),
                head_seq,
                time.perf_counter() - started,
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
        # Memory black box (2026-08-03). The swarm kill died 2.5s into this
        # exact window with nothing recorded; the sampler runs for the life of
        # the rebuild so a transient between close and allocate cannot hide.
        # Best-effort by construction — instrumentation must never be what
        # breaks a rebuild.
        _memwatch = None
        try:
            from core.mem_probe import RebuildWatch

            _memwatch = RebuildWatch("batched", backend=self)
            _memwatch.__enter__()
        except Exception:  # noqa: BLE001
            _memwatch = None
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
        if _memwatch is not None:
            _memwatch.mark("closed")
        primary._ctx = internals.LlamaContext(
            model=primary._model, params=primary.context_params, verbose=False
        )
        primary._batch = internals.LlamaBatch(
            n_tokens=primary.n_batch,
            embd=0,
            n_seq_max=primary.context_params.n_seq_max,
            verbose=False,
        )
        if _memwatch is not None:
            _memwatch.mark("allocated")
        primary.input_ids = np.ndarray((primary._n_ctx,), dtype=np.intc)
        logits_rows = primary._n_ctx if primary._logits_all else 1
        primary.scores = np.ndarray((logits_rows, primary._n_vocab), dtype=np.single)
        primary._candidates = internals.LlamaTokenDataArray(n_vocab=primary._n_vocab)
        primary.n_tokens = 0
        primary._sampler = None
        primary._sampling_ctx = None
        heads, reasoning_heads = self._pin_batched_heads()
        if self._engine is not None:
            self._engine._persona_heads = heads
            self._engine._reasoning_heads = reasoning_heads
        self._demote_batched_snapshots()
        # Flow pins die with the context too — clear so the next use BUILDs
        # fresh instead of forking dead cells.
        _eng = getattr(self, "_engine", None)
        if _eng is not None and getattr(_eng, "_flow_pins", None):
            _n = len(_eng._flow_pins)
            _eng._flow_pins.clear()
            log.info("🔁 batched rebuild cleared %d flow pin(s)", _n)
        self._h_context_refreshes += 1
        log.info(
            "🧼 batched context rebuilt + heads re-pinned in %.2fs",
            time.perf_counter() - started,
        )
        if _memwatch is not None:
            _memwatch.__exit__(None, None, None)

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

        # Session flow-fork (the per-session resident fork) stays pool-only;
        # the STATELESS flow band is batched-native as of 2026-07-30 (BUILD
        # at retire, HIT via install_flow_sync) and lives on the seq map's
        # flow slots.
        if self._session_flow_fork:
            log.warning(
                "⚠️ resident_session_flow_fork is ON in config but is POOL-ONLY "
                "— disabled under batched. The per-session turn-0 flow fork "
                "will not happen; the stateless flow band below is unrelated."
            )
        self._session_flow_fork = False
        # Recompute: _flow_band was derived in __init__ as
        # (_flow_resident or _session_flow_fork), and _session_flow_fork just
        # became False. Reporting the stale value announced an 8-slot band on
        # every batched config with flow_kv_cache: false — the seq map sizes
        # flow_slots from flow_kv_cache ALONE, so the band did not exist. Read
        # the map, not the intent.
        self._flow_band = self._flow_resident or self._session_flow_fork
        _flow_slots = len(seq_map.flow_seqs)
        if _flow_slots:
            log.info("flow band ACTIVE in batched mode (%d slots)", _flow_slots)
        elif getattr(self.config.model, "flow_kv_cache", False):
            log.warning(
                "⚠️ flow_kv_cache is ON but the batched seq map allocated NO "
                "flow slots — the stateless flow cache is inert this boot"
            )

        # DECLARE WHAT WE WILL NOT DO, ONCE, AT BOOT. These features are
        # pool-only and were each announced per-request at log.debug — i.e.
        # invisible at the default level. A config could request a feature,
        # never receive it, and read as though it had: the same
        # record-says-one-thing/run-does-another shape as the transient-files
        # declaration drift. The per-request debug lines stay for tracing; this
        # is the line an operator actually sees.
        _ignored = []
        if getattr(self.config.model, "resident_strip_reasoning", False):
            _ignored.append(
                "resident_strip_reasoning (prior-turn CoT will ACCUMULATE in "
                "the live seq across every turn)"
            )
        if getattr(
            getattr(self.config, "generation", None), "degen_retry_enabled", None
        ):
            _ignored.append(
                "degen_retry sampling overrides (the retry still fires, but at "
                "temperature only — presence penalty and penalty window dropped)"
            )
        for _feat in _ignored:
            log.warning("⚠️ POOL-ONLY, ignored under batched: %s", _feat)

        # Seats carry the per-STREAM ceiling (min of pool allocation and
        # trained range) — the pool may be far larger than any one stream
        # is allowed to grow (the swarm/model context split).
        _seat_ctx = self._stream_ctx_limit() or primary._n_ctx
        self._engine_seats = [
            SeqSlot(seq=i, _n_ctx=min(int(primary._n_ctx), int(_seat_ctx)))
            for i in range(self._pool_size)
        ]

        gen_cfg = self.config.generation
        capture_dir = getattr(getattr(self.config, "logging", None), "directory", None)
        engine = BatchedEngine(
            primary,
            seq_map,
            n_batch=int(getattr(self.config.model, "n_batch", 2048) or 2048),
            prefill_chunk=getattr(self.config.resources, "batched_prefill_chunk", None),
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
                        gen_cfg.repetition_max_cycle_period or DEFAULT_MAX_CYCLE_PERIOD
                    ),
                    min_cycle_reps=gen_cfg.repetition_min_cycle_reps
                    or DEFAULT_MIN_CYCLE_REPS,
                )

            engine._repetition_guard_factory = _guard_factory
        else:
            engine._repetition_guard_factory = lambda: None
        engine._reasoning_heads = reasoning_heads
        # Cells the engine cannot see: the snapshot band lives in the backend's
        # registry, and under kv_unified those pins are free until their source
        # seat is cleared — then they are not. Admission over-reported free
        # cells by exactly this amount before 2026-08-25.
        engine.extra_occupancy_fn = self._batched_band_occupancy
        for seat in self._engine_seats:
            seat._engine_ref = engine
        engine.start()
        self._engine = engine

    def _batched_band_occupancy(self) -> int:
        """Cells pinned on the snapshot band, for the engine's admission math.

        CALLED ON THE DECODE THREAD from _free_cells, so it reads CACHED INTS
        only — no ctypes, no _ctx dereference. Three server-killing SIGSEGVs
        are documented at the n_ctx_seq read below for exactly that reason,
        and test_n_ctx_seq_health_read.py enforces it.

        Live pins (_batched_snap_seqs), not registry flags: a refresh demotes
        pins while leaving registry entries behind, and counting the stale
        ones would shrink the pool permanently.
        """
        total = 0
        for key in list(self._batched_snap_seqs):
            entry = self._snap_registry.get(key)
            if not entry:
                continue
            total += int(entry.get("static_len") or 0)
            total += len(entry.get("dyn_tokens") or ())
        return total

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
        # Memory black box — see the batched twin and core/mem_probe.py.
        _memwatch = None
        try:
            from core.mem_probe import RebuildWatch

            _memwatch = RebuildWatch("pool", backend=self)
            _memwatch.__enter__()
        except Exception:  # noqa: BLE001
            _memwatch = None
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
        if _memwatch is not None:
            _memwatch.mark("closed")
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
        if _memwatch is not None:
            _memwatch.mark("allocated")
        # Re-derive the per-stream ceiling from the REBUILT context. A refresh
        # can change the seq geometry (the resident-denial un-fragment path
        # rebuilds with n_seq_max=1 through here), and a stale _n_ctx would
        # re-open the exact blind spot the seq clamp exists to close.
        _seq_lim = self._seq_ctx_limit(inst._ctx, inst.context_params)
        _lim = self._stream_ctx_limit()
        _base = int(getattr(inst, "_persona_n_ctx", None) or inst.context_params.n_ctx)
        # Re-cache for the health register — the rebuilt context may have a
        # different seq geometry (see the build-site comment).
        inst._n_ctx_seq = _seq_lim
        inst._n_ctx = min(c for c in (_base, _lim, _seq_lim) if c)
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
        if _memwatch is not None:
            _memwatch.__exit__(None, None, None)

    async def _drain_for_refresh(self, reason: str) -> bool:
        """Close admissions and drain the batched pool for a refresh.

        Phase 1 (finish period): new acquires queue on the admission gate;
        in-flight work gets ``_refresh_drain_s`` to finish naturally.
        Phase 2 (force-clear) — RECOVERY REASONS ONLY: remaining sessions
        are expired through the SessionManager's normal expiry path, then
        any still-live streams are retired with a retriable error.

        PROACTIVE refreshes are POLITE (operator, 2026-08-03): a rot-
        clearing rebuild is never worth killing live work. When the finish
        period expires with a generation still running, a proactive refresh
        DEFERS — returns False, the caller aborts, and the cap re-fires at
        the next request boundary, which on a sequential agent workload is
        the natural between-turns quiet point. The bartowski laguna run
        made the cost concrete: the 30-min cap evicted a 52k-token batch
        generation — coherent, win-path-analyzing work — 2/3 of the way
        through, and the retry was doomed to the same wall. Force-clear
        remains for recovery reasons (decode-fatal latch heal), where the
        context is already broken and there is no work worth preserving.

        A POLITE drain clears on ``_active_generations == 0`` ALONE — idle
        checked-out seats do NOT block it. A memoryful session holds its
        seat for its whole life, so requiring ``_checked_out == 0`` made
        the predicate unsatisfiable on any session workload: every drain
        was a guaranteed full-window admission blackout followed by a
        deferral, re-fired seconds later (bartowski retry, 2026-08-02:
        ~5min blocked per ~15s worked from 17:53 on, with the GPU idle
        long enough for Metal to unwire the weights each cycle). Held-but-
        idle seats are safe to rebuild under: the admission gate is closed
        and ``generation_guard`` waits on it BEFORE incrementing, so no
        new GPU work can start; session seq state demotes to cold and the
        next fork re-prefills (the same path the fatal latch-heal rebuild
        has always exercised mid-session). Recovery drains keep the full
        both-zero predicate — force-clear genuinely empties the pool.

        Returns True when the pool is clear (caller refreshes); the gate is
        REOPENED BY THE CALLER's finally, not here.
        """
        polite = reason.startswith("proactive")
        self._refresh_admission_gate.clear()
        log.info(
            "🧼 refresh drain (%s): admissions gated; %d seat(s) out, "
            "%d generation(s) live; window %.0fs",
            reason,
            self._checked_out,
            self._active_generations,
            self._refresh_drain_s,
        )
        deadline = time.monotonic() + self._refresh_drain_s
        while time.monotonic() < deadline:
            if self._active_generations == 0 and (polite or self._checked_out == 0):
                if polite and self._checked_out > 0:
                    log.info(
                        "🧼 polite drain clear with %d idle seat(s) held — "
                        "sessions demote to cold and re-fork after the rebuild",
                        self._checked_out,
                    )
                return True
            await asyncio.sleep(1.0)

        # Polite deferral: proactive refreshes never force. Live work wins;
        # the cap re-fires at the next request boundary.
        if polite:
            log.info(
                "🧼 refresh deferred (%s): %d seat(s) out, %d generation(s) "
                "live at the finish deadline — live work wins; retrying at "
                "the next quiet boundary",
                reason,
                self._checked_out,
                self._active_generations,
            )
            return False

        # Force-clear stragglers (recovery reasons only).
        if self._session_expirer is not None and self._checked_out > 0:
            try:
                n = await self._session_expirer(
                    f"context refresh drain deadline ({reason})"
                )
                log.warning("🧼 refresh drain: force-expired %d session(s)", n)
            except Exception:  # noqa: BLE001 — drain must not die on expiry
                log.exception("refresh drain: session expiry failed")
        if self._engine is not None and self._active_generations > 0:
            try:
                n = await run_in_threadpool(
                    self._engine.evict_all_streams,
                    "context refresh — stream retired; retry lands on the fresh context",
                )
                log.warning("🧼 refresh drain: evicted %d stream(s)", n)
            except Exception:  # noqa: BLE001
                log.exception("refresh drain: stream eviction failed")
        # Short settle for the releases to land.
        settle = time.monotonic() + self._refresh_settle_s
        while time.monotonic() < settle:
            if self._checked_out == 0 and self._active_generations == 0:
                return True
            await asyncio.sleep(1.0)
        log.error(
            "⚠️ refresh drain failed to clear the pool (%d out, %d live)",
            self._checked_out,
            self._active_generations,
        )
        return False

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
            # with no live streams; idle checked-out seats are fine (gate
            # closed + generation_guard means they cannot start work, and
            # session seq state demotes to cold / re-forks). When busy:
            # - drain disabled (legacy): defer — the caller retries later.
            # - drain enabled: close the admission gate, give in-flight work
            #   the drain window to finish, then force-clear stragglers
            #   (sessions expire through their normal listener path; streams
            #   retire RETRIABLE — the same client recovery as KV eviction)
            #   and refresh. This is what makes the timed cap actually land
            #   under continuous multi-mission load (2026-07-16 souring:
            #   two arms kept the pool busy for 8.5h and every refresh
            #   deferred while both wedged on stub rewrites).
            if self._checked_out > 0 or self._active_generations > 0:
                if self._refresh_drain_s <= 0:
                    self._h_refresh_deferred += 1
                    return {
                        "refreshed": 0,
                        "reason": reason,
                        "status": "deferred_busy",
                    }
                drained = await self._drain_for_refresh(reason)
                if not drained:
                    self._h_refresh_deferred += 1
                    self._refresh_admission_gate.set()
                    return {
                        "refreshed": 0,
                        "reason": reason,
                        "status": "drain_timeout",
                    }
            engine = self._engine
            try:
                await run_in_threadpool(engine.pause)
                try:
                    await run_in_threadpool(self._rebuild_batched_context)
                    engine._batch = None  # re-allocate against the fresh context
                    self._h_requests_since_refresh = 0
                    self._last_refresh_monotonic = time.monotonic()
                finally:
                    engine.resume()
            finally:
                # Reopen admissions whatever happened — queued acquirers
                # must never starve behind a failed refresh.
                self._refresh_admission_gate.set()
            elapsed = time.perf_counter() - started
            log.info(
                "✅ Batched context refresh #%d in %.2fs (reason=%s)",
                self._h_context_refreshes,
                elapsed,
                reason,
            )
            return {
                "refreshed": 1,
                "reason": reason,
                "status": "ok",
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
            self._h_context_refreshes,
            len(targets),
            elapsed,
            reason,
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
        self._last_refresh_monotonic = time.monotonic()
        while True:
            try:
                await asyncio.sleep(15)
                if self._primary_instance is None:
                    continue
                reason = self._refresh_decision()
                if reason:
                    log.info(
                        "🧼 proactive refresh (%s): %d req since last "
                        "(interval %d), cap %ds — rebuilding context",
                        reason,
                        self._h_requests_since_refresh,
                        self._refresh_interval,
                        self._refresh_seconds,
                    )
                    await self.refresh_context(reason=reason)
            except asyncio.CancelledError:
                return
            except Exception:
                # A refresh failure must NEVER kill this loop: the task
                # object is held forever, so a dead loop is SILENT (asyncio
                # only reports unretrieved exceptions at GC) and every future
                # auto-refresh is lost — the 2026-07-16 18:12 drain fired,
                # threw somewhere after its first phase, and the server ran
                # the evening with no anti-souring protection and no
                # traceback. Log loudly, back off, keep looping.
                log.exception("💥 proactive refresh attempt failed — loop continues")
                await asyncio.sleep(60)

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
            if self._checked_out > 0 or self._active_generations > 0:
                if self._refresh_drain_s > 0 and self._decode_mode == "batched":
                    # Drain enabled: fire anyway — refresh_context gates
                    # admissions, drains the finish window, force-clears
                    # stragglers. This is the fix for continuous
                    # multi-mission load, where the busy check above
                    # deferred forever while the process soured.
                    return "proactive-timed-drain"
                self._h_refresh_deferred += 1
                return None
            return "proactive-timed"
        return None

    async def _seat_reaper_loop(self) -> None:
        """Backstop for leaked batched seats: a consumer that vanishes
        without releasing (lazily-finalized asyncgen chain after a watchdog
        cancel) strands its seat checked out forever — reproduced live as 30
        phantom seats / zero decode / terminal wedge. The deterministic
        drains (shielded aclose + cancel-proof release/acquire) make the
        common paths leak-free; this loop reclaims whatever still slips
        through.

        Every 60s: any LEASED, unpinned seat with no live engine stream
        accrues a strike; two consecutive strikes (>60s idle-while-leased,
        comfortably past the acquire→submit gap) → loud warning + reclaim
        through the normal release path. Deliberately NOT gated on
        _active_generations — the session orphan reaper self-disabled on
        exactly that counter when the leak inflated it. Counter drift is
        reported log-only: a GC-finalized guard still decrements later, so
        auto-correcting _active_generations here would double-count.
        """
        strikes: Dict[int, int] = {}
        while True:
            try:
                await asyncio.sleep(60)
                await self._seat_reaper_sweep(strikes)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("💥 seat reaper sweep failed — loop continues")
                await asyncio.sleep(60)

    async def _seat_reaper_sweep(self, strikes: Dict[int, int]) -> None:
        """One reaper pass (see _seat_reaper_loop). ``strikes`` carries the
        per-seat consecutive-miss count across sweeps."""
        engine = self._engine
        if engine is None:
            return
        from inference.batched_engine import StreamPhase

        try:
            # LIVE = admitted streams AND queued admissions. A request the
            # engine parked in _waiting (QUEUE verdict — not enough free
            # cells) holds its pre-acquired seat but has no StreamState yet,
            # so a snapshot of _streams alone calls its seat orphaned. That
            # false positive IS the seq-wedge (2026-08-19, X=1764/Y=9595):
            # the reaper reclaimed a parked large request's seat, its
            # clear_seat landed between the admitted stream's prefill
            # chunks, and the KV lost 7,830 positions mid-flight. Queue
            # waits under contention run minutes — far past two sweep
            # strikes — and only LARGE prompts queue, which is why every
            # capture involved a large-prompt handoff. Both sets are read
            # inside one control op: the decode thread owns _streams and
            # _waiting, so this is the only race-free vantage.
            live = await asyncio.wrap_future(
                engine.control(
                    lambda: {
                        id(s.slot)
                        for s in engine._streams.values()
                        if s.phase is not StreamPhase.DONE
                    }
                    | {id(r.slot) for r in engine._waiting if r.slot is not None}
                )
            )
        except Exception:  # noqa: BLE001 — engine busy/parked, try next sweep
            return
        # THIRD LIVE STATE (2026-08-27 01:00-02:30, 19 false reclaims): a
        # batched-vision seat between acquire and stream submit — encode +
        # multimodal install, seconds to a minute under the encode lock —
        # has no StreamState and no _waiting entry. Same class as the
        # 2026-08-19 parked-admission wedge above; same fix shape: union
        # the installing registry (event-loop-mutated, race-free here).
        live = live | set(getattr(self, "_vision_installing", ()) or ())
        now = time.monotonic()
        for seat in list(self._engine_seats):
            leased = getattr(seat, "_leased_at", None)
            if leased is None or seat.pinned or id(seat) in live:
                strikes.pop(id(seat), None)
                continue
            # STRIKES ARE PER LEASE, not per seat: tonight's captures show
            # "leased 2s — reclaiming" because a fresh lease inherited the
            # PREVIOUS lease's strike. Key the count to the lease timestamp
            # and reset when it changes.
            prev = strikes.get(id(seat))
            if prev is None or prev[0] != leased:
                strikes[id(seat)] = (leased, 1)
                continue
            strikes[id(seat)] = (leased, prev[1] + 1)
            if strikes[id(seat)][1] < 2:
                continue
            strikes.pop(id(seat), None)
            log.warning(
                "🧹 seat reaper: seat seq %d leased %.0fs with no live "
                "stream — reclaiming (consumer cancelled without releasing?)",
                seat.seq,
                now - leased,
            )
            # Mark BEFORE releasing: a late duplicate release from the
            # leaked consumer (however it interleaves) hits the
            # _reaper_reclaimed guard in release_instance and is ignored;
            # acquire clears the mark on the next lease.
            self._reaper_reclaimed.add(id(seat))
            await self._release_seat(seat)
        if self._active_generations > len(live) + len(strikes):
            log.warning(
                "⚠️ counter drift: _active_generations=%d vs engine "
                "active_streams=%d — a generation guard likely awaits GC "
                "finalization (log-only, self-corrects when the guard "
                "finalizes)",
                self._active_generations,
                len(live),
            )

    @staticmethod
    def _is_fatal_decode(exc: Exception) -> bool:
        """Codes that mean the CONTEXT is unusable, not that the caller erred.

        -3 (GGML_STATUS_FAILED) latches the Metal backend. -2 is an allocation
        failure. -1 is normally a caller bug — an empty or oversized batch — but
        it is included because it is what the SAME fault re-presents as after a
        rebuild: the Hy3 40960 rung failed -3 on its static eval, healed, then
        returned -1 "Invalid input batch (exceeding capacity)" on every attempt
        with an unchanged 1,800-token batch that fits n_batch 2048 comfortably.
        A batch that was valid before a rebuild and invalid after it is a
        context that cannot serve, not a malformed request.
        """
        s = str(exc)
        return any(f"code {c}" in s for c in ("-3", "-2", "-1"))

    def _mark_decode_failure(self, inst: Any, exc: Exception) -> None:
        """Record a fatal decode failure and flag the instance for a context
        refresh. The Metal backend's sticky error latch means THIS context
        will fail every subsequent decode — the flag routes it through
        _heal_instance before it serves again. The failing turn still errors
        (caller sees it); the SLOT self-heals."""
        self._h_decode_failures += 1
        # Already rebuilt since the last failure and failing again? Then the
        # rebuild is not the cure and repeating it just burns time while the
        # weights stay resident.
        if getattr(inst, "_healed_since_failure", False):
            self._consecutive_unhealed_decode_failures += 1
        inst._healed_since_failure = False
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
        self._maybe_declare_unservable()

    # How many heal-then-fail-again cycles before the configuration is called
    # unservable. 2 means: rebuild once, fail again, rebuild once more, fail
    # again — no reasonable transient survives that.
    _UNHEALED_LIMIT = int(os.environ.get("OURO_UNHEALED_DECODE_LIMIT", "2"))

    def _maybe_declare_unservable(self) -> None:
        """Stop serving when rebuilding the context stops helping.

        THE PROCESS EXITS rather than merely refusing work, and that is the
        point: a server stuck in a heal/fail loop is holding its weights
        resident — 108 GB in the case that motivated this — while the decode
        that cannot fit keeps asking the allocator for more. On 2026-07-28 that
        state preceded a hard reboot by two rungs of a context ladder. Releasing
        the memory is the only action that protects the machine; refusing
        requests while still holding it does not.

        Exit is via SIGTERM to self so the normal shutdown path runs (the pool
        drains rather than leaking, which a hard kill does not do — see the
        single-instance pool leak of 2026-07-19).
        """
        if self._unservable:
            return
        if self._consecutive_unhealed_decode_failures < self._UNHEALED_LIMIT:
            return
        self._unservable = True
        log.error(
            "🛑 UNSERVABLE: %d fatal decodes survived a context rebuild. This "
            "is not a latch — the configuration cannot decode. Holding %s "
            "resident while retrying is what precedes a machine reboot, so "
            "this process is shutting down. Lower n_ctx (the last "
            "measured-decodable value belongs in the config) or reduce the "
            "model; raising kv_preflight_gb is how the guard gets bypassed, "
            "not how the load is made to fit.",
            self._consecutive_unhealed_decode_failures,
            getattr(getattr(self, "config", None), "model", None)
            and getattr(self.config.model, "name", "the model")
            or "the model",
        )
        try:
            import signal as _signal

            os.kill(os.getpid(), _signal.SIGTERM)
        except Exception:  # noqa: BLE001 — never mask the original decode error
            log.exception("could not signal self; the flag stays set")

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
            # Armed: if the NEXT decode on this instance also fails, the
            # rebuild demonstrably did not help.
            inst._healed_since_failure = True
            self._h_latch_heals += 1
            log.info(
                "🩹 latch heal #%d complete [%s] — context rebuilt, slot healthy",
                self._h_latch_heals,
                getattr(inst, "_persona", "default"),
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
        return self._pool_seq_map().reasoning_base

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
        inst_static_len = int(
            getattr(inst, "_static_len", self._resident_static_len) or 0
        )
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
                toks = build_static_tokens(
                    self.config, reasoning=level, persona=persona
                )
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
        except (
            Exception
        ) as exc:  # noqa: BLE001 — disable head-swap for this inst, stay safe
            log.warning(
                "reasoning head pin failed (%s) — head-swap off for this slot", exc
            )
            inst._reasoning_seqs = {}
        finally:
            # Restore seq 0 to the pristine static so acquire_instance's fork is sound.
            self._resident_restore_static(inst)

    @staticmethod
    def _clamp_reasoning_level(level: str, default: str, pinned) -> str:
        """Resolve a requested level to one this family can actually serve.

        Walks the canonical ladder DOWNWARD from the request to the first
        level that is either the default (rides SEQ_STATIC / persona head)
        or has a pinned head. A request for xhigh on a family whose map
        stops at high therefore serves HIGH — not, as before, a silent
        fall-through to the default: the caller asked for the ceiling, so
        give the highest ceiling that exists (2026-08-16, operator).
        """
        ladder = ("xhigh", "high", "medium", "low")
        if level not in ladder:
            return level
        pinned = set(pinned or ())
        for lv in ladder[ladder.index(level) :]:
            if lv == default or lv in pinned:
                return lv
        return default

    def _install_reasoning_head(self, inst: Any, level: str) -> bool:
        """Fork the pinned head for ``level`` onto the live seq — the reasoning
        HEAD-SWAP. Whole-seq replace (like _resident_restore_static), so it is sound
        only at request/turn START (nothing above the head yet); the session
        manager calls it at turn 0. Returns True if a head was installed."""
        if self._reasoning_gate_family:
            # Gate family: the per-turn gate is the actuator; the session
            # keeps the default (activation) head. See __init__ note —
            # baking the low head here cost gemma every CoT of a tier arm.
            return False
        _served = self._clamp_reasoning_level(
            level, self._reasoning_default_level, self._reasoning_pin_levels
        )
        if _served != level:
            log.info("reasoning %s → %s (family ceiling)", level, _served)
            level = _served
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
                level,
                inst.seq,
                head.n_tokens,
            )
            return True
        seqs = getattr(inst, "_reasoning_seqs", None) or {}
        if level not in seqs:
            # WARNING, not silence: an unpinned level on THIS instance means the
            # pin didn't survive (or never reached) this pool slot — the fully
            # silent former return here cost a probe cycle (2026-08-16).
            log.warning(
                "reasoning head-swap %s: level not pinned on instance (has %s)",
                level,
                sorted(seqs) or "none",
            )
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
            # TEXT-RESOLVED ALIAS (2026-08-03, the gemma-31b silent-off bug).
            # Pinning dedups by RENDERED TEXT (bimodal families collapse:
            # gemma high renders the same <|think|> as medium), but this
            # lookup was by LEVEL NAME — so a request for the deduped level
            # found nothing, was "ignored", and served the RESTING head,
            # which under per_request is the OFF state. gemma `high`
            # measured 6 tokens of empty channel while `medium` thought 20k
            # chars. Resolve through the family map: if the requested
            # level's text matches a pinned sibling's (or the default's),
            # use that head.
            try:
                from formats.registry import get_renderer

                _lv = dict(
                    get_renderer(self.config.model.family).s.reasoning.levels or {}
                )
            except Exception:  # noqa: BLE001 — no map, no alias
                _lv = {}
            want = _lv.get(level, level)
            if (
                _lv.get(self._reasoning_default_level, self._reasoning_default_level)
                == want
            ):
                return self._reasoning_head_source(inst, self._reasoning_default_level)
            for sib, seq in seqs.items():
                if _lv.get(sib, sib) == want:
                    level = sib
                    break
            else:
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
        if self._reasoning_gate_family:
            # Gate family: same stand-down as _install_reasoning_head — a
            # low splice would strip the activation token mid-session.
            return False
        _served = self._clamp_reasoning_level(
            level, self._reasoning_default_level, self._reasoning_pin_levels
        )
        if _served != level:
            log.info("reasoning %s → %s (family ceiling)", level, _served)
            level = _served
        if self._decode_mode == "batched":
            cur = (
                getattr(inst, "_reasoning_current", None)
                or self._reasoning_default_level
            )
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
                level,
                inst.seq,
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
                cur,
                cur_hlen,
                level,
                hlen,
                n_tokens,
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
            level,
            hlen,
            n_tokens - hlen,
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
        if level:
            _served = self._clamp_reasoning_level(
                level, self._reasoning_default_level, self._reasoning_pin_levels
            )
            if _served != level:
                log.info(
                    "completion reasoning %s → %s (family ceiling)", level, _served
                )
                level = _served
        if not level or level == self._reasoning_default_level:
            return prompt_tokens
        if not (self._reasoning_head_swap and getattr(self, "_resident_active", False)):
            if self._reasoning_gate_family:
                # NOT ignored: on gate-level families the level already
                # reached the renderer's per-turn think gate inside the
                # prompt build (build_full_prompt), and the head-swap is
                # merely the wrong actuator to also apply it. Warning here
                # cried wolf on every request — and a false "ignored" is as
                # costly as a silent no-op: it sent the 2026-08-22 deepseek
                # CoT audit chasing a phantom for the exact defect class the
                # warning exists to expose.
                log.debug(
                    "completion reasoning=%s applied via genprompt gate "
                    "(head-swap not in play)",
                    level,
                )
                return prompt_tokens
            # WARNING, not debug: a requested level silently not applying is the
            # exact no-op class that hid the muse dial for two probes
            # (2026-08-16) — say WHICH gate refused.
            log.warning(
                "completion reasoning=%s ignored (head_swap=%s resident_active=%s)",
                level,
                self._reasoning_head_swap,
                getattr(self, "_resident_active", False),
            )
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
                level,
                hlen,
                static_len,
                len(prompt_tokens),
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
        inst.input_ids[n_keep : n_tokens - n_discard] = inst.input_ids[
            n_keep + n_discard : n_tokens
        ]
        inst.n_tokens = n_tokens - n_discard
        log.warning(
            "🪟 windowed resident seq: dropped %d oldest tokens "
            "(kept %d static head + %d recent)",
            n_discard,
            n_keep,
            inst.n_tokens - n_keep,
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
                    flow_key,
                    seq,
                    flow_prefix_len,
                )
            else:
                # BUILD: seq 0 holds the global static (acquire fork); eval the flow
                # head on top, then pin a copy on a dedicated flow seq.
                inst.eval(
                    list(prompt_tokens[self._resident_static_len : flow_prefix_len])
                )
                seq = self._alloc_flow_seq(inst, flow_key)
                ctx.memory_seq_rm(seq, 0, -1)
                ctx.memory_seq_cp(SEQ_WORKING, seq, -1, -1)
                inst.input_ids[:flow_prefix_len] = ids
                self._h_flow_builds += 1
                log.info(
                    "🆕 resident flow BUILD %r (seq %d, %d tok)",
                    flow_key,
                    seq,
                    flow_prefix_len,
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

    def _ask_can_shift(self) -> Optional[bool]:
        """Can this arch host the resident cache? Asked ALWAYS, acted on only
        when requested. Returns None when the question cannot be put (a test
        double, or a binding without the call) — None means UNKNOWN and must
        never be collapsed to False, which would read as a measured 'no'."""
        try:
            return bool(self._primary_instance._ctx.memory_can_shift())
        except Exception:  # noqa: BLE001 — a probe must not break a load
            log.debug("memory_can_shift() unavailable", exc_info=True)
            return None

    def _session_strategy(self) -> str:
        """Which per-turn KV mechanism this server will ACTUALLY use.

        Derived, never declared: `resident_seq_cache: true` in a config is a
        REQUEST that the can_shift gate may deny, and the denial silently lands
        on a different path. Naming the effective strategy is the difference
        between a fact and an inference."""
        if self._resident_active:
            return "resident"
        # Only two strategies exist. The legacy save_state splice was deleted
        # 2026-07-30 and the validator refuses `session_full_replay: false`, so
        # non-resident is full_replay by construction. Returning a third name
        # here put a dead value into the health register.
        return "full_replay"

    def _log_session_strategy(self) -> None:
        """One line, every load, whatever the flags.

        Costs nothing and closes a real forensic gap: hy3's session path had to
        be reverse-engineered from `static=0 tok` inside a generation log line,
        and its can_shift answer did not exist anywhere because the gate only
        ran when resident was requested."""
        strategy = self._session_strategy()
        try:
            params = self._primary_instance.context_params
            n_ctx = int(getattr(params, "n_ctx", 0) or 0)
            n_seq = max(1, int(getattr(params, "n_seq_max", 1) or 1))
            # The REAL per-seq window, asked of the context — not the display
            # arithmetic. The arithmetic misled once already (it assumed /2
            # for a config the stock bands split /12).
            seq_win = self._seq_ctx_limit(
                getattr(self._primary_instance, "_ctx", None), params
            )
        except Exception:  # noqa: BLE001
            n_ctx, n_seq, seq_win = 0, 1, 0

        shift = (
            "unknown"
            if self._session_can_shift is None
            else str(self._session_can_shift)
        )
        detail = ""
        if strategy != "resident" and self._session_can_shift is True:
            # THE ACTIONABLE CASE: a flat path is available and unused. This is
            # exactly hy3 — quadratic re-prefill by omission, not by necessity.
            detail = (
                " — resident AVAILABLE but not enabled; this model is "
                "paying full re-prefill per session turn"
            )

        log.info(
            "🧩 session strategy: %s (resident_requested=%s, memory_can_shift=%s, "
            "n_ctx=%d, n_seq_max=%d, n_ctx_seq=%d)%s",
            strategy,
            self._resident_requested,
            shift,
            n_ctx,
            n_seq,
            seq_win or (n_ctx // n_seq),
            detail,
        )

    def _pool_seq_map(self):
        """The pool band layout for the CURRENT flags (see seq_layout.py) —
        the one place _snap_seq_base/_reasoning_seq_base/n_seq_max derive
        from, so a band resize cannot desync them."""
        return plan_pool_seq_map(
            flow_hot_set=self._flow_hot_set if self._flow_band else 0,
            snapshot_max=self._snapshot_max,
            reasoning_levels=(
                self._reasoning_pin_levels if self._reasoning_head_swap else []
            ),
        )

    def _snap_seq_base(self) -> int:
        return self._pool_seq_map().snap_base

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
        # Pool instances carry `_persona`; batched SeqSlots carry `persona`.
        inst_persona = getattr(inst, "_persona", None) or getattr(
            inst, "persona", "default"
        )
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
            return self._snapshot_seat(inst, key)
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
            key,
            seq,
            n_tokens,
            n_tokens - static_len,
        )
        return {"tokens": n_tokens, "resident": True}

    def fork_snapshot_seq(self, inst: Any, key: str) -> Optional[int]:
        """Fork snapshot ``key`` onto the working seq. HOT (pinned on this
        instance): pure seq_cp, ~zero cost. Cold miss: returns None — caller
        runs rebuild_snapshot_cold. Unknown key: KeyError."""
        if self._decode_mode == "batched":
            return self._fork_snapshot_seat(inst, key)
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
        if self._decode_mode == "batched":
            raise RuntimeError(
                f"snapshot {key!r} is COLD under batched mode — the cold rebuild "
                "replays via inst.eval, which a seat does not have, and a "
                "control-op replay would stall every live stream. Re-capture "
                "from a live session (v1 boundary, 2026-07-30)."
            )
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
        log.info("🧊 snapshot %r cold rebuild: %d tokens re-prefixed", key, n_total)
        return n_total

    def _demote_batched_snapshots(self) -> int:
        """The rebuilt context holds NO snapshot cells: demote every hot pin
        to cold (registry survives; resident flag cleared) — the same demotion
        the pool refresh performs. Under batched, cold = re-capture (the v1
        boundary rebuild_snapshot_cold enforces with a clean raise). Extracted
        from _rebuild_batched_context so it is testable without a context."""
        for _k in list(self._batched_snap_seqs):
            if _k in self._snap_registry:
                self._snap_registry[_k]["resident"] = False
        n = len(self._batched_snap_seqs)
        self._batched_snap_seqs.clear()
        if n:
            log.info("📸 batched rebuild demoted %d hot snapshot(s) to cold", n)
        return n

    def _snapshot_seat(self, seat: Any, key: str) -> dict:
        """Batched capture: pin a session seat's live KV on a snapshot-band seq.

        Was "pool-only in batched mode v1" (a hard raise — Block E2 measured
        the refusal clean, 2026-07-30); the band now exists in the seq map when
        ``session_snapshot_max > 0``. All KV surgery routes through the
        engine's control inbox, so it lands at a step boundary on the decode
        thread — the same contract as ``SeqSlot.purge_to``. Under kv_unified
        (a batched requirement) the band divides nothing; pinned cells accrue
        per token like any other seq."""
        engine = getattr(seat, "_engine_ref", None)
        if engine is None:
            raise RuntimeError("batched snapshot needs a seat-attached session")
        smap = self._batched_seq_map()
        if not len(smap.snap_seqs):
            raise RuntimeError(
                "session snapshots disabled: session_snapshot_max is 0 on this "
                "config (the batched band is sized from it)"
            )
        if key in self._snap_registry:
            raise RuntimeError(f"snapshot key {key!r} already exists — purge first")
        used = set(self._batched_snap_seqs.values())
        free = [q for q in smap.snap_seqs if q not in used]
        if not free:
            # Same contract as the pool band: capacity errors are the caller's
            # signal to purge — never silent eviction.
            raise RuntimeError(
                f"snapshot capacity ({len(smap.snap_seqs)}) reached — purge one first"
            )
        if getattr(seat, "has_media", False):
            # A multimodal install left image-embedding rows on this seq;
            # input_ids holds NEGATIVE sentinels for them. A snapshot's
            # dyn_tokens would capture those sentinels and a later restore
            # would replay them as real token ids — silent garbage. Vision
            # seats are stateless single turns by design; refuse loudly.
            raise RuntimeError(
                "refusing to snapshot a seat holding media rows "
                f"(seq {seat.seq}); vision streams are not session-resumable"
            )
        snap_seq = free[0]
        n_tokens = int(seat.n_tokens)
        static_len = int(getattr(seat, "static_len", 0) or 0)

        def _pin() -> None:
            ctx = self._primary_instance._ctx
            ctx.memory_seq_rm(snap_seq, 0, -1)
            ctx.memory_seq_cp(seat.seq, snap_seq, -1, -1)

        engine.control(_pin).result(timeout=30)
        self._batched_snap_seqs[key] = snap_seq
        self._snap_registry[key] = {
            "dyn_tokens": [int(t) for t in seat.input_ids[static_len:n_tokens]],
            "static_len": static_len,
            "persona": getattr(seat, "persona", "default"),
            "turn_count": 0,  # caller (session manager) overwrites
            "created_at": time.time(),
            "resident": True,
        }
        log.info(
            "📸 batched snapshot %r pinned: seq %d, %d tokens (%d dynamic)",
            key,
            snap_seq,
            n_tokens,
            n_tokens - static_len,
        )
        return {"tokens": n_tokens, "resident": True}

    def _fork_snapshot_seat(self, seat: Any, key: str) -> Optional[int]:
        """Batched hot fork: snapshot-band seq → this session's seat.

        Returns the forked position, or None on a cold entry — and under
        batched, cold stays a CLEAN REFUSAL downstream (rebuild_snapshot_cold
        raises): the pool cold path replays via ``inst.eval``, which a seat
        does not have, and evaluating a 30k-token history inside a control op
        would stall every live stream at that step boundary. v1 boundary,
        stated: capture-then-fork-hot is the designed use (label trees, doc
        fan-outs); a snapshot that survived a context rebuild must be
        re-captured from a live session."""
        entry = self._snap_registry[key]
        self._guard_snapshot_persona(seat, key, entry)
        snap_seq = self._batched_snap_seqs.get(key)
        if snap_seq is None or not entry.get("resident"):
            return None
        engine = getattr(seat, "_engine_ref", None)
        if engine is None:
            raise RuntimeError("batched snapshot fork needs a seat-attached session")
        static_len = int(entry["static_len"])
        n_total = static_len + len(entry["dyn_tokens"])
        _heads = getattr(getattr(self, "_engine", None), "_persona_heads", {}) or {}
        head = _heads.get(entry.get("persona", "default"))
        head_tokens = list(getattr(head, "tokens", []) or [])[:static_len]

        def _fork() -> None:
            ctx = self._primary_instance._ctx
            ctx.memory_seq_rm(seat.seq, 0, -1)
            ctx.memory_seq_cp(snap_seq, seat.seq, -1, -1)

        engine.control(_fork).result(timeout=30)
        seat.input_ids = list(head_tokens) + [int(t) for t in entry["dyn_tokens"]]
        seat.n_tokens = n_total
        log.info(
            "🌿 batched snapshot %r forked onto seat seq %d (%d tokens)",
            key,
            seat.seq,
            n_total,
        )
        return n_total

    def purge_snapshot(self, key: str) -> bool:
        """Free ``key`` everywhere: hot seqs on every instance + registry."""
        found = key in self._snap_registry
        self._snap_registry.pop(key, None)
        snap_seq = getattr(self, "_batched_snap_seqs", {}).pop(key, None)
        if snap_seq is not None and getattr(self, "_engine", None) is not None:
            with contextlib.suppress(Exception):
                self._engine.control(
                    lambda: self._primary_instance._ctx.memory_seq_rm(snap_seq, 0, -1)
                ).result(timeout=30)
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
        log.info(
            "📸 snapshot %r registered (replay mode, %d tok)", key, len(dyn_tokens)
        )
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
        if not self._refresh_admission_gate.is_set():
            # A drain-refresh is in progress: queue here (a slow request, not
            # a failed step). Budget = the drain window + rebuild + margin.
            try:
                await asyncio.wait_for(
                    self._refresh_admission_gate.wait(),
                    timeout=self._refresh_drain_s + 120,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    "Context refresh did not complete within its drain window"
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

        # Refuse arithmetically-impossible swa_full KV allocations BEFORE
        # any Metal work (reboot #3: no tripwire outraces a 451GB wiring
        # burst; the header math must gate the load).
        self._kv_preflight()

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
        # corrupts it — see dev/archive/docs/CACHE_STATE.md architecture matrix).
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
        # — true on SWA models with swa_full (+ kv_unified) and on can-shift
        # hybrids (Qwen3-Next); FALSE on interleaved-SWA models WITHOUT
        # swa_full (gpt-oss/OLMo 3/Gemma class) and on pure-recurrent state.
        # When false, force the resident path off and fall back to the legacy
        # save_state/full_replay path — AND un-fragment the context: it was
        # allocated with the resident seq-band n_seq_max, and llama.cpp splits
        # n_ctx per sequence (n_ctx_seq = n_ctx / n_seq_max), so the fallback
        # would otherwise run on a fraction of the configured window (observed
        # 2026-07-22: OLMo's 65k became 5,632/seq — the first design prompt
        # failed to decode at all; qwen3.5's 264k became 22k/seq).
        self._session_can_shift = self._ask_can_shift()
        if self._resident_requested:
            can_shift = bool(self._session_can_shift)
            # M-RoPE EXCEPTION (2026-08-29). can_shift served as a proxy for
            # "normal attention KV where seq ops work", and for every model
            # before paddle the two were the same fact. M-RoPE models return
            # can_shift=False for a DIFFERENT reason: shifting rotated
            # multi-section positions is ill-defined (llama-kv-cache.cpp
            # get_can_shift: n_pos_per_embd() > 1), while the cache itself is
            # ordinary — memory_seq_rm and multi-seq decode verified clean on
            # paddle hardware (probe_vision_kv_integrity.py, 2026-08-29:
            # neighbour KV byte-identical, joint step legal). So an M-RoPE
            # model may host the resident cache and the batched engine; the
            # shift-DEPENDENT features stay off via _session_can_shift=False
            # (session window slide refuses, media seats refuse in
            # window_seat_sync). Recurrent models still land in the else.
            mrope = False
            if not can_shift:
                try:
                    import llama_cpp as _lc

                    rt = int(
                        _lc.llama_model_rope_type(self._primary_instance._model.model)
                    )
                    mrope = rt in (
                        int(_lc.llama_rope_type.LLAMA_ROPE_TYPE_MROPE),
                        int(_lc.llama_rope_type.LLAMA_ROPE_TYPE_IMROPE),
                    )
                except Exception:  # noqa: BLE001 — unknowable => keep old gate
                    mrope = False
            self._resident_active = can_shift or mrope
            if mrope and not can_shift:
                log.info(
                    "🧩 Resident-seq cache ACTIVE via the M-RoPE exception "
                    "(memory_can_shift=False because positions are "
                    "multi-section, not because seq ops fail; KV shift "
                    "features stay disabled)"
                )
            elif can_shift:
                log.info("🧩 Resident-seq cache ACTIVE (memory_can_shift=True)")
            else:
                log.warning(
                    "🧩 resident_seq_cache requested but memory_can_shift=False "
                    "(interleaved-SWA without swa_full, or recurrent memory) — "
                    "falling back to FULL REPLAY, which re-prefills the whole "
                    "session every turn (measured 7.8-9.8x more prefill at "
                    "depth 16). For iSWA models (gpt-oss / OLMo 3 / Gemma "
                    "class) set swa_full: true + kv_unified: true to enable "
                    "the resident cache; recurrent/hybrid architectures "
                    "(qwen GDN class) are refused regardless of flags."
                )
                _params = self._primary_instance.context_params
                _nsm = int(getattr(_params, "n_seq_max", 1) or 1)
                if _nsm > 1 and self._decode_mode != "batched":
                    log.warning(
                        "🧩 un-fragmenting: context was allocated with the "
                        "resident seq band (n_seq_max=%d) — rebuilding "
                        "single-seq to restore the full per-sequence window",
                        _nsm,
                    )
                    _params.n_seq_max = 1
                    await run_in_threadpool(
                        self._refresh_context_sync, self._primary_instance
                    )

        # Clamp the PRIMARY's per-stream ceiling by the true per-seq window.
        # _create_shared_instance clamps pool slots 1..N, but the primary IS
        # pool slot 0 and its _n_ctx came from upstream Llama as the TOTAL
        # allocation — a session pinned to slot 0 on a fragmented context had
        # no guard at all. Placed after the resident gate so a denial's
        # un-fragment rebuild (n_seq_max back to 1) is what gets measured.
        try:
            _p = self._primary_instance
            _seq_lim = self._seq_ctx_limit(_p._ctx, _p.context_params)
            _lim = self._stream_ctx_limit()
            _p._n_ctx_seq = _seq_lim  # health register reads this, not the live ctx
            _p._n_ctx = min(c for c in (int(_p._n_ctx), _lim, _seq_lim) if c)
        except Exception:  # noqa: BLE001 — test doubles without a real ctx
            log.debug("primary per-seq clamp skipped", exc_info=True)

        self._log_session_strategy()

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
                "ignored",
                self._slot_personas[0],
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
            # Seat reaper: reclaims seats leaked by cancelled consumers
            # (batched-only — pool instances release deterministically).
            self._seat_reaper_task = asyncio.create_task(self._seat_reaper_loop())
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
                    self._persona_queues[persona] = asyncio.Queue(
                        maxsize=self._pool_size
                    )
            for inst in self._all_instances:
                self._persona_queues[getattr(inst, "_persona", "default")].put_nowait(
                    inst
                )

            log.info(
                f"✅ Llama pool ready ({len(self._all_instances)} instances, "
                f"personas={self._slot_personas}, "
                f"shared_model=True, hybrid={self._is_hybrid})"
            )

        # Signal that the backend is fully ready for inference.
        self._ready_event.set()

        # Start the proactive context-refresh loop (interval-gated when idle; the wall-clock cap fires under load via the drain when context_refresh_drain_s > 0).
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
        if self._seat_reaper_task is not None:
            self._seat_reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._seat_reaper_task
            self._seat_reaper_task = None

        if self._engine is not None:
            # Stop the decode thread first: it owns all context operations,
            # and closing the context underneath a live step is a crash.
            await run_in_threadpool(self._engine.shutdown)
            self._engine = None
            self._engine_seats = []

        pool_size = len(self._all_instances)

        # Free the vision instance FIRST — it holds a context over the shared
        # model plus an mtmd context of its own, and the primary's close() will
        # not free the handler because the handler lives here, not there.
        # EVERY vision context, not just the first. Each pool member owns its
        # own mtmd_ctx and its own llama_context; freeing only _vision_instance
        # would strand the rest — wired GPU memory with no handle left, which
        # is the leak class the factory's teardown latch exists for.
        for vinst in self._vision_instances or (
            [self._vision_instance] if self._vision_instance is not None else []
        ):
            handler = getattr(vinst, "chat_handler", None)
            if handler is not None:
                try:
                    handler.close()  # frees mtmd_ctx
                except Exception as exc:  # noqa: BLE001 — teardown must finish
                    log.warning(f"⚠️ Error closing vision handler: {exc}")
                vinst.chat_handler = None
            self._close_shared_context(vinst)
        self._vision_instances = []
        self._vision_pool = None
        self._vision_instance = None

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

        # From here the instance is off its queue: any exception — including
        # a consumer CancelledError landing in the heal/restore awaits below
        # — must put it back, or the slot is lost with no release ever
        # scheduled (every entry point acquires OUTSIDE its try).
        counted = False
        try:
            # Belt over the release-side heal: a flagged instance must never
            # serve (its Metal latch fails every decode) — heal before handout.
            # (Batched seats carry the flag too, but their heal is the engine
            # rebuild, not the per-context refresh.)
            if self._decode_mode != "batched" and getattr(
                inst, "_needs_context_refresh", False
            ):
                await self._heal_instance(inst)

            # Pool-membership accounting only. GPU-busy tracking lives in
            # generation_guard — a checkout (e.g. a session pinned between
            # turns) is not GPU work and must not block scaling drains.
            self._checked_out += 1
            counted = True

            # Restore seq 0 to the pristine post-static-tokens state. Guard it
            # like any other GPU work. Resident: fork SEQ_STATIC → seq 0
            # (intra-context copy). Legacy: load_state the (multi-GB) snapshot
            # blob. Batched: fork the persona head onto the seat's seq via a
            # control op (the decode thread owns all KV surgery, applied
            # between steps).
            if self._decode_mode == "batched":
                seat, engine = inst, self._engine
                async with self.generation_guard():
                    await asyncio.wrap_future(
                        engine.control(lambda: engine.prepare_seat(seat, persona_key))
                    )
                self._reaper_reclaimed.discard(id(seat))
                seat._leased_at = time.monotonic()
                log.debug(
                    "🧩 Forked persona head [%s] → seat seq %d (batched)",
                    persona_key,
                    seat.seq,
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
        except BaseException:
            if counted:
                self._checked_out = max(0, self._checked_out - 1)
            if self._decode_mode == "batched":
                inst._leased_at = None
            with contextlib.suppress(Exception):
                queue.put_nowait(inst)
            raise

    @staticmethod
    def weights_bytes_total(path: str, split_count: Any = None) -> int:
        """Total on-disk weight bytes, summing EVERY shard of a split GGUF.

        ``os.path.getsize(path)`` sees only the shard it was handed, and large
        models ship split. Step-3.7-Flash currently ships as
        ``...-00001-of-00003.gguf`` at 5MB + 49.4GB + 45.9GB = 95.3GB
        (unsloth UD-IQ4_XS; the retired stepfun quant was ~2.7GB heavier).
        Measuring shard 1 alone would report 5MB against 95.3GB actual, in the
        one calculation whose entire job is refusing an allocation that would
        hard-reboot the machine.

        ``split.count`` comes from the GGUF header when available; the
        filename pattern is the fallback, since a header read that failed
        upstream still leaves the naming convention intact. Both paths verify
        each sibling exists before counting it, and a single-file model simply
        returns its own size.

        A sibling ``mmproj-*.gguf`` (the vision projector — 7.9GB next to
        Step-3.7, 2.3GB next to gemma-4) is correctly EXCLUDED, because it only
        matches by living in the same directory and not by the shard pattern.
        Anything that switches this to a directory glob must re-exclude it by
        name or it silently adds ~8GB to a budget measured in single-digit GB
        of headroom.

        NOTE this is FILE size, which is an upper bound on resident footprint,
        not a measurement of it: an MoE under mmap pages in only what it routes
        to. step-3.7 at its verified 138240 ceiling accounts to 146.3GB here
        against a 121.8GB peak RSS. That over-count is safe (it refuses early)
        but it is why `probe_verified_n_ctx` exists.
        """
        import glob as _glob
        import re as _re

        total = os.path.getsize(path)
        m = _re.search(r"-(\d{5})-of-(\d{5})\.gguf$", path)
        if not m:
            return total  # not a split model

        try:
            n = int(split_count) if split_count else int(m.group(2))
        except (TypeError, ValueError):
            n = int(m.group(2))

        stem = path[: m.start()]
        suffix = m.group(2)
        shards = [f"{stem}-{i:05d}-of-{suffix}.gguf" for i in range(1, n + 1)]
        found = [s for s in shards if os.path.exists(s)]
        if len(found) < n:
            # Naming drifted from the convention — fall back to a glob so an
            # unusual layout still counts more than one shard.
            found = _glob.glob(f"{stem}-*-of-{suffix}.gguf") or [path]
        return sum(os.path.getsize(s) for s in found)

    @staticmethod
    def kv_bytes_from_header(
        n_ctx: int, kvh_per_layer: list, key_len: int, value_len: int
    ) -> int:
        """Full-KV (swa_full) byte cost for ``n_ctx`` cells from GGUF header
        facts: Σ_layers kv_heads_i × (key_len + value_len) × 2 bytes (f16 K+V)
        × n_ctx. Validated against measurement 2026-07-24: gpt-oss 72KB/tok
        (predicted wired to within ~3GB at every ladder rung), gemma-4
        1.68MB/tok (451GB @262k — reboot #3's arithmetic)."""
        per_tok = sum(
            int(h) * (int(key_len) + int(value_len)) * 2 for h in kvh_per_layer
        )
        return per_tok * int(n_ctx)

    def _kv_preflight(self) -> None:
        """Refuse an arithmetically-impossible swa_full KV allocation BEFORE
        touching Metal. Three hard reboots (2026-07-24) were precomputable
        from the GGUF header; the memguard tripwire cannot outrace a
        hundreds-of-GB wiring burst, so the guard must run pre-allocation.
        Best-effort on header reads (never blocks a load on a read failure —
        only refuses on a CONFIRMED oversize); swa_full-only (windowed-SWA
        allocations are small by construction)."""
        import os

        m = getattr(self.config, "model", None)
        # RUNS FOR EVERY CONFIG as of 2026-07-29. It used to return here unless
        # swa_full was set, on the premise that "windowed-SWA allocations are
        # small by construction" — true for a real sliding-window model, and
        # simply false for one that has no sliding window at all. That left 7 of
        # 19 configs (glm-4.7-flash/deepseek2-MLA, hy3, gemma-4-26b-a4b,
        # laguna-xs, mistral-medium, qwen3.5-122b, qwen3.6-27b) allocating full
        # KV with no preflight whatsoever.
        #
        # The premise still earns something, so it survives as a BUDGET choice
        # rather than an on/off switch (see `budget_gb` below): swa_full configs
        # are held to their configured budget, non-swa_full configs only to the
        # physical ceiling. That catches the catastrophic case everywhere
        # without re-introducing the false refusals a tight formula produces on
        # genuinely windowed models.
        swa_full = bool(getattr(m, "swa_full", False))
        try:
            from gguf import GGUFReader

            path = str(m.path)
            r = GGUFReader(path)

            def _field(key: str):
                f = r.get_field(key)
                return f.contents() if f is not None else None

            arch = _field("general.architecture")
            n_layer = int(_field(f"{arch}.block_count") or 0)
            kvh = _field(f"{arch}.attention.head_count_kv")
            key_len = _field(f"{arch}.attention.key_length")
            value_len = _field(f"{arch}.attention.value_length") or key_len
            if key_len is None:
                emb = _field(f"{arch}.embedding_length")
                heads = _field(f"{arch}.attention.head_count")
                key_len = value_len = int(emb) // int(heads)
            kvh_list = (
                [int(h) for h in kvh]
                if hasattr(kvh, "__len__")
                else [int(kvh)] * n_layer
            )
            if not kvh_list or not key_len:
                return
            kv_bytes = self.kv_bytes_from_header(
                m.n_ctx, kvh_list, int(key_len), int(value_len)
            )
            # MEASURED override. The header formula assumes swa_full gives every
            # layer a full-size cache. On interleaved-SWA architectures that
            # over-predicts: gemma-4-31b measures 0.87 MB/token against a
            # formula ~1.72 — almost exactly 2x — so a config stable at ~81GB
            # peak got REFUSED at a computed 130.4GB and an unattended batch
            # lost the arm (2026-07-27).
            #
            # A config may therefore declare bytes/token it has actually
            # measured. The guard still runs — it is re-armed against real
            # geometry rather than disabled — which is the safe way to fix an
            # over-prediction. Raising kv_preflight_gb past physical memory
            # would silence the guard instead, and it exists because three
            # hard reboots were precomputable.
            measured = getattr(m, "kv_bytes_per_token_measured", None)
            if measured:
                kv_bytes = int(measured) * int(m.n_ctx)
                log.info(
                    "🧮 KV preflight: using MEASURED %.2f MB/token (config) "
                    "instead of the %.2f MB/token header formula",
                    int(measured) / 1e6,
                    self.kv_bytes_from_header(
                        m.n_ctx, kvh_list, int(key_len), int(value_len)
                    )
                    / int(m.n_ctx)
                    / 1e6,
                )
            split_count = _field("split.count")
            weights_bytes = self.weights_bytes_total(path, split_count)
        except Exception:  # noqa: BLE001 — preflight must never block a load
            return
        # ── THE HARD CEILING, WHICH NOTHING MAY RAISE ────────────────
        # Measured 2026-07-29: step-3.7 at n_ctx 262144 computed 143.7GB against
        # 137.4GB physical — 6.3GB over, 4.6% — and HARD-REBOOTED the machine
        # 2m51s into the load. llama.cpp emitted no error code at all: the
        # process died allocating, so the always-on decode-code guard never had
        # a call to return from. That crash required OURO_KV_PREFLIGHT_GB=9999
        # to produce, i.e. this guard was correct and had to be switched off.
        #
        # So the override may LOWER the budget or wave through a marginal case;
        # it may not authorise an allocation the machine cannot physically
        # satisfy. Note iogpu.wired_limit_mb is NOT a defence here — wired
        # peaked at 97.5GB against a 116GB cap while free memory sat at 0.06GB.
        physical_gb = _physical_memory_gb()
        hard_ceiling_gb = physical_gb * _PHYSICAL_SAFETY_FRACTION

        env_budget = os.environ.get("OURO_KV_PREFLIGHT_GB")
        cfg_budget = getattr(m, "kv_preflight_gb", None)
        if swa_full:
            asked_gb = float(env_budget or cfg_budget or hard_ceiling_gb)
        else:
            # Windowed SWA genuinely allocates less than this formula predicts
            # on a real sliding-window model, so do not hold it to a tight
            # configured budget — but the machine's physical limit is not a
            # matter of configuration.
            asked_gb = hard_ceiling_gb
        budget_gb = min(asked_gb, hard_ceiling_gb)
        clamped = budget_gb < asked_gb

        # VISION ADDS TO THE BUDGET, AND ONLY WHEN CONFIGURED.
        # weights_bytes_total() deliberately EXCLUDES a sibling mmproj-*.gguf
        # (see its docstring — a directory glob there would silently add ~8GB
        # to a budget measured in single-digit GB of headroom). That exclusion
        # is correct for a text-only load and must stay, so the projector is
        # added HERE instead, where we know we are actually going to load it.
        # Step-3.7's projector is 7.92GB, so under-counting is not academic.
        # The vision context's own KV rides along at n_seq_max=1.
        vision_bytes = 0
        mmproj = getattr(self.config.model, "mmproj_path", None)
        if mmproj:
            # TIMES THE ENCODER POOL. Each batched-vision encoder is its own
            # mtmd context with its own projector upload
            # (vision_batched_encoders; 1 for every config that predates the
            # 2026-08-29 pool). Same lesson as the vision-context width
            # under-count below: a preflight that prices one copy approves a
            # pool it cannot afford.
            enc_copies = 1
            if bool(getattr(self.config.model, "vision_batched", False)):
                enc_copies = max(
                    1,
                    int(getattr(self.config.model, "vision_batched_encoders", 1) or 1),
                )
            try:
                vision_bytes += os.path.getsize(str(mmproj)) * enc_copies
            except OSError as exc:
                log.warning("mmproj not readable for preflight (%s): %s", mmproj, exc)
            v_ctx = int(getattr(self.config.model, "vision_n_ctx", 8192) or 8192)
            text_ctx = int(getattr(self.config.model, "n_ctx", 0) or 0)
            # TIMES THE POOL WIDTH. _build_vision_pool builds vision_pool_size
            # private contexts, not one, and this counted a single context —
            # so a config asking for 4 was preflighted at a quarter of its
            # cost. Live: paddle at vision_pool_size 4 x vision_n_ctx 32768
            # preflighted "1.49GB OK" and then OOMed the process while
            # building the pool it had just approved.
            v_width = max(
                1, int(getattr(self.config.model, "vision_pool_size", 1) or 1)
            )
            if kv_bytes and text_ctx:
                vision_bytes += int(kv_bytes * (v_ctx / float(text_ctx))) * v_width

        total_gb = (kv_bytes + weights_bytes + vision_bytes) / 1e9
        if vision_bytes:
            log.info(
                "👁  Vision adds %.2fGB to the preflight "
                "(projector + %d x %d-token ctx)",
                vision_bytes / 1e9,
                max(1, int(getattr(self.config.model, "vision_pool_size", 1) or 1)),
                int(getattr(self.config.model, "vision_n_ctx", 8192) or 8192),
            )

        # ── A PROBE-VERIFIED CEILING OUTRANKS THE ARITHMETIC ─────────
        # This n_ctx has been observed to load AND decode on this machine, so a
        # formula that says otherwise is wrong about the formula, not about the
        # machine. Bound to the weights it was measured against: a requant
        # invalidates the measurement and drops us back to the estimate.
        verified = getattr(m, "probe_verified_n_ctx", None)
        verified_w = getattr(m, "probe_verified_weights_bytes", None)
        if verified and m.n_ctx <= int(verified):
            if verified_w and abs(int(verified_w) - weights_bytes) > 1_000_000:
                log.warning(
                    "⚠️ probe verification for n_ctx<=%d is STALE — weights are "
                    "%.1fGB now, %.1fGB when measured. Falling back to the "
                    "formula; re-run `--probe-context %s` to re-verify.",
                    int(verified),
                    weights_bytes / 1e9,
                    int(verified_w) / 1e9,
                    getattr(m, "name", "this model"),
                )
            else:
                log.info(
                    "🧮 KV preflight: n_ctx=%d is within the PROBE-VERIFIED "
                    "ceiling %d (measured to load and decode on this machine) "
                    "— arithmetic estimate %.1fGB not enforced",
                    m.n_ctx,
                    int(verified),
                    total_gb,
                )
                return

        if total_gb > budget_gb:
            measured = getattr(m, "kv_bytes_per_token_measured", None)
            basis = "MEASURED bytes/token" if measured else "the header FORMULA"
            hint = (
                ""
                if measured
                else (
                    " This estimate is the header formula, which has "
                    "over-predicted by ~2x on interleaved-SWA and MLA "
                    "architectures — if you have MEASURED this model's KV, set "
                    "kv_bytes_per_token_measured and the guard re-arms against "
                    "real geometry instead of being raised past it."
                )
            )
            raise RuntimeError(
                f"KV preflight REFUSED: n_ctx={m.n_ctx} needs "
                f"{kv_bytes / 1e9:.1f}GB KV + {weights_bytes / 1e9:.1f}GB "
                f"weights = {total_gb:.1f}GB > {budget_gb:.1f}GB "
                f"(physical {physical_gb:.1f}GB"
                f"{', budget CLAMPED to the physical ceiling' if clamped else ''})"
                f", computed from {basis}. An allocation past physical memory "
                f"hard-reboots this machine before any error code is returned "
                f"(measured 2026-07-29 at 4.6% over). Shrink n_ctx.{hint}"
            )
        log.info(
            "🧮 KV preflight: n_ctx=%d → %.1fGB KV + %.1fGB weights = %.1fGB "
            "(budget %.1fGB, physical %.1fGB, swa_full=%s)%s — OK",
            m.n_ctx,
            kv_bytes / 1e9,
            weights_bytes / 1e9,
            total_gb,
            budget_gb,
            physical_gb,
            swa_full,
            " [budget clamped to physical ceiling]" if clamped else "",
        )

    def _stream_ctx_limit(self) -> int:
        """The per-STREAM token ceiling (the swarm/model context split):
        min(pool allocation n_ctx, trained range model_max_context). 0 when
        the config doesn't carry either (bare test doubles)."""
        m = getattr(self.config, "model", None)
        lim = getattr(m, "stream_context_limit", None)
        if lim:
            return int(lim)
        return int(getattr(m, "n_ctx", 0) or 0)

    @staticmethod
    def _seq_ctx_limit(ctx_obj: Any, params: Any) -> int:
        """The TRUE per-sequence window of a built context. 0 = unknown.

        Under ``kv_unified: false`` llama.cpp splits the allocation:
        ``n_ctx_seq ≈ n_ctx / n_seq_max``. Every historical casualty of NOT
        knowing this was exactly the stock 12-seq band: OLMo 65,536 → 5,632,
        qwen3.5 264,192 → 22,016, and the 2026-07-29 hy3 sweep failure
        (32,768 → 2,730, BELOW its 1,793-token static prefix — decode failed
        outright). Every per-stream guard keyed off ``inst._n_ctx`` while
        believing it had ~12× the cells llama.cpp actually gave the seq, so
        the failure mode was a raw llama_decode error instead of a clean
        refusal or a well-placed window.

        Ask the context itself (``llama_n_ctx_seq``, exposed by the binding
        and correct in BOTH modes — it returns n_ctx under kv_unified); fall
        back to the arithmetic only when the call is unavailable (older
        binding, test double). 0 means "could not determine" and callers must
        not clamp on it — an unknown must never masquerade as a measurement.
        """
        try:
            n = int(ctx_obj.n_ctx_seq())
            if n > 0:
                return n
        except Exception:  # noqa: BLE001 — probe, then arithmetic
            pass
        try:
            n_ctx = int(getattr(params, "n_ctx", 0) or 0)
            n_seq = max(1, int(getattr(params, "n_seq_max", 1) or 1))
            if not n_ctx:
                return 0
            if bool(getattr(params, "kv_unified", False)):
                return n_ctx
            return n_ctx // n_seq
        except Exception:  # noqa: BLE001
            return 0

    async def _release_seat(self, seat: Any) -> None:
        """Batched seat return: clear its seq (control op) and requeue. A
        dead seat (engine fatal) still requeues — the next prepare_seat
        re-forks onto a rebuilt context or errors loudly.

        Cancellation-proof by design: the clear is shielded (a pending
        consumer CancelledError is a BaseException that would sail past
        ``except Exception`` and skip the requeue — the reproduced
        30-phantom-seat wedge), and the requeue + counter decrement run in a
        ``finally`` so they happen even when the clear fails — a dirty seat
        is safe to hand out again (prepare_seat starts with memory_seq_rm).
        """
        engine = self._engine
        seat._leased_at = None
        try:
            with anyio.CancelScope(shield=True):
                await asyncio.wrap_future(
                    engine.control(lambda: engine.clear_seat(seat))
                )
        except Exception as exc:  # noqa: BLE001 — return the seat regardless
            log.warning("⚠️ seat clear failed on release: %s", exc)
        finally:
            if self._pool_queue is not None:
                self._pool_queue.put_nowait(seat)
            self._checked_out = max(0, self._checked_out - 1)
            log.debug(
                "🔧 Released seat seq %d (idle=%d, checked_out=%d)",
                seat.seq,
                self._pool_queue.qsize() if self._pool_queue else 0,
                self._checked_out,
            )

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
            seat = inst
            if id(seat) in self._reaper_reclaimed:
                # The seat reaper already reclaimed this lease (leaked by a
                # GC-deferred consumer whose finally fired late) — a second
                # requeue would seat two streams on one seq.
                self._reaper_reclaimed.discard(id(seat))
                log.debug(
                    "🔧 Seat seq %d already reclaimed by reaper — duplicate "
                    "release ignored",
                    seat.seq,
                )
                return
            await self._release_seat(seat)
            return
        # Cancellation-proof, mirroring _release_seat: the requeue + counter
        # decrement run in a ``finally`` so a CancelledError landing in the
        # heal (a BaseException that sails past ``except Exception``) can no
        # longer skip them — on a limit=1 pool that stranded the ONLY
        # instance until restart. The shield covers anyio-delivered cancels;
        # a native task.cancel() still interrupts the heal coroutine, which
        # is recoverable BY DESIGN: _heal_instance leaves the flag set on
        # any failure, and the next release/acquire retries it.
        proactive: Optional[str] = None
        try:
            # SESSION-RELEASE PROACTIVE REFRESH (2026-08-07). The polling
            # refresh loop starved on pool-mode continuous load: trigger (a)
            # needs an idle 15s poll that pinned sessions never allow, and
            # trigger (b)'s fire-while-busy drain is batched-only — so a
            # 45-hour run logged ZERO refreshes while the volume-driven rot
            # climbed to 22 degeneration events and killed the server. A
            # release is the one moment the instance is guaranteed
            # session-free, so past-due counters piggyback the existing
            # heal path (context rebuild + persona re-warm, already
            # cancellation-proof) right here.
            if not getattr(inst, "_needs_context_refresh", False):
                since = self._h_requests_since_refresh
                elapsed = time.monotonic() - (self._last_refresh_monotonic or 0.0)
                if since >= self._refresh_interval:
                    proactive = (
                        f"release-interval ({since} >= {self._refresh_interval})"
                    )
                elif since > 0 and elapsed >= self._refresh_seconds:
                    proactive = (
                        f"release-timecap ({int(elapsed)}s >= "
                        f"{self._refresh_seconds}s)"
                    )
                if proactive:
                    inst._needs_context_refresh = True
            if getattr(inst, "_needs_context_refresh", False):
                with anyio.CancelScope(shield=True):
                    await self._heal_instance(inst)
                if proactive and not getattr(inst, "_needs_context_refresh", False):
                    # Heal succeeded (it clears the flag on success):
                    # book the proactive refresh.
                    self._h_context_refreshes += 1
                    self._h_requests_since_refresh = 0
                    self._last_refresh_monotonic = time.monotonic()
                    log.info(
                        "✅ Session-release context refresh #%d (%s)",
                        self._h_context_refreshes,
                        proactive,
                    )
        except Exception as exc:  # noqa: BLE001 — return the instance regardless
            log.warning("⚠️ instance heal failed on release: %s", exc)
        finally:
            if self._pool_queue is not None:
                queue = self._persona_queues.get(
                    getattr(inst, "_persona", "default"), self._pool_queue
                )
                try:
                    queue.put_nowait(inst)
                except asyncio.QueueFull:
                    # Only reachable on a double-release; a second requeue
                    # would hand the same instance to two streams.
                    log.warning("⚠️ duplicate instance release ignored")
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

    def _build_generate_kwargs(self, temperature: float) -> dict:
        """Build kwargs for the low-level ``Llama.generate()`` method.

        The ``generate()`` API uses different parameter names than
        ``create_completion()`` (e.g. ``temp`` instead of
        ``temperature``).  It also does **not** accept ``max_tokens``,
        ``stream``, or ``stop`` — those are handled by our own loop.
        """
        gen = self.config.generation

        def _set(value: Any, default: Any) -> Any:
            """Config value if PRESENT, else the default.

            `x or default` was the idiom here and it silently discarded every
            legitimate zero — and zero is the MEANINGFUL "disabled" value for
            two of these knobs, not an absence:

              * `top_k: 0` is llama.cpp's "no top-k limit". Six configs declare
                it (devstral, glm, both gpt-oss, hy3, mistral) and every one of
                them was being served a hard 40-token cutoff instead.
              * `min_p: 0.0` is the Gemma team's own canonical inference config
                (temp 1.0 / top_k 64 / top_p 0.95 / min_p 0.0). Seven configs
                declare it and all were silently raised to 0.05.

            So a config could record the vendor-recommended setting, pass
            review, and be overridden by the loader — the failure mode that
            broke glm's first run, one layer further down. Only `None` means
            "not configured"; `Optional[float]` in the schema already said so.

            Found 2026-07-31 while asking why gemma-4-31b and gemma-4-26b
            declared different penalty knobs. They did not differ in effect;
            this did.
            """
            return default if value is None else value

        kwargs: dict[str, Any] = {
            "temp": temperature,
            "top_p": _set(gen.top_p, 0.95),
            "top_k": _set(gen.top_k, 40),
            "min_p": _set(gen.min_p, 0.05),
            "present_penalty": _set(gen.presence_penalty, 0.0),
            # Neutral by default. If a model starts jamming on a token run,
            # a small bump (1.05-1.15) is the first lever to try before
            # anything structural — vendor guidance offers no default here,
            # only a remedial range once repetition is actually observed.
            "repeat_penalty": _set(gen.repeat_penalty, 1.0),
            "reset": False,  # Preserve static state loaded by acquire_instance
        }
        # Penalty lookback window — the library default (64 tokens) is blind
        # to paragraph-scale cycles; GDN-hybrid configs widen it so the
        # classic penalties can see an ~800-token orbit.
        if gen.penalty_last_n:
            kwargs["penalty_last_n"] = int(gen.penalty_last_n)
        # DRY sampler — long-period repetition breaker; only touched when a
        # config opts in, so every other model's sampler chain is unchanged.
        if gen.dry_multiplier and gen.dry_multiplier > 0:
            kwargs["dry_multiplier"] = float(gen.dry_multiplier)
            kwargs["dry_base"] = float(gen.dry_base or 1.75)
            kwargs["dry_allowed_length"] = int(gen.dry_allowed_length or 2)
            kwargs["dry_penalty_last_n"] = int(
                gen.dry_penalty_last_n if gen.dry_penalty_last_n is not None else -1
            )
        if gen.penalty_freq:
            kwargs["penalty_freq"] = float(gen.penalty_freq)
        # Reasoning budget / logit bias: batched-engine knobs (see
        # batched_engine._build_sampling_params). Emitted only when configured
        # so the pool path, which forwards this dict to Llama.generate(), never
        # sees a key it does not understand.
        if gen.reasoning_budget is not None:
            kwargs["reasoning_budget"] = int(gen.reasoning_budget)
            if gen.reasoning_start:
                kwargs["reasoning_start"] = str(gen.reasoning_start)
            if gen.reasoning_end:
                kwargs["reasoning_end"] = str(gen.reasoning_end)
        if gen.logit_bias:
            kwargs["logit_bias"] = dict(gen.logit_bias)
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
        ``reset=False`` so the static-context state established by
        ``acquire_instance()`` is preserved.  Only the *dynamic*
        portion of the prompt (user message, chat template) is
        evaluated — the static knowledge tokens are already in the
        KV cache from the static fork (resident/batched) or
        ``load_state()`` (legacy).

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

        # Think-hold on the POOL path: forwarded to Llama.generate() as a
        # logits processor (the fork wraps it into a CustomSampler in the
        # chain — same seam the batched path uses directly). The closure's
        # counter increments once per sampled token, so the ban covers the
        # first N generated tokens of THIS stream only.
        _think_hold = kwargs.pop("think_hold", None)
        if _think_hold:
            _th_token = int(_think_hold["token_id"])
            _th_n = int(_think_hold["n"])
            _th_state = {"count": 0}

            def _think_hold_processor(_input_ids, scores):
                if _th_state["count"] < _th_n:
                    scores[_th_token] = -1e30
                _th_state["count"] += 1
                return scores

            kwargs["logits_processor"] = _think_hold_processor
            log.info(
                "🤔 think-hold armed (pool): ban token %d for first %d tokens",
                _th_token,
                _th_n,
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
            # The M8 save_state-BLOB flow cache lived here and was deleted
            # 2026-07-30 (dev/caching/CORPUS.md — rap sheet: save_state churn
            # corrupts static KV over a run, SWA pruning fragility, multi-GB
            # blob overflow). Every flow-capable config runs resident (the
            # seq-ops hot-set above); a non-resident pool config with
            # flow_kv_cache on serves from the static base and counts a
            # fallback, so the mismatch is visible in health.
            self._h_flow_fallbacks += 1
            log.info(
                "flow_kv_cache requested for %r but resident cache inactive — "
                "M8 blob path retired; serving from the static base",
                flow_key,
            )

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
        buffer_mode = effective_max <= BUFFER_MODE_MAX_TOKENS

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
        # Per-request sampling overrides (Llama.generate parameter names) —
        # the degeneration-retry recipe uses this to re-drive a purged turn
        # at temp 1.0 + presence_penalty with a widened window without
        # touching the config-level defaults.
        _sampling_overrides = kwargs.pop("sampling_overrides", None)
        if _sampling_overrides:
            gen_kwargs.update(_sampling_overrides)
        # Think-hold processor built earlier in this call (kwargs carry it as
        # "logits_processor") — forward to Llama.generate.
        _lp = kwargs.pop("logits_processor", None)
        if _lp is not None:
            gen_kwargs["logits_processor"] = _lp
        from formats.registry import get_renderer

        # Use caller-provided stops when given (session mode) — otherwise
        # default to completion-mode stops from the renderer.
        session_mode = stop_texts is not None
        if stop_texts is None:
            stop_texts = get_renderer(self.config.model.family).stop_tokens()

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
        completion_tokens: List[int] = []
        is_first_token = True

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

        # Shared per-token state machine (inference/token_pipeline.py) — the
        # same class the batched engine binds per stream. This loop keeps
        # only what is caller-side by contract: EOG detection, tracker
        # marks, the max-tokens budget, and exception/telemetry policy.
        pipeline = TokenPipeline(
            instance,
            stop_texts,
            guard=guard,
            final_stop=final_stop,
            long_cycle_on=long_cycle_on,
            buffer_mode=buffer_mode,
            capture_dir=capture_dir or "./logs",
            capture_meta=lambda: build_capture_meta(
                instance, kwargs.get("request_id", ""), temperature, prompt_tokens
            ),
            initial_prior_tokens=prompt_tokens,
        )

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
                # End-of-generation token check (caller-side by contract)
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

                verdict = pipeline.feed(token)
                if verdict.degenerate:
                    # Guard/long-cycle/detok abort — capture the specimen and
                    # raise through the clean DegenerateGenerationError path.
                    gen_end_reason = verdict.degenerate
                    self._h_runaway_captures += 1
                    pipeline.dump_capture(verdict.degenerate)
                    raise DegenerateGenerationError(
                        verdict.degenerate,
                        tokens_generated=len(completion_tokens),
                    )

                if verdict.end_reason == "final_channel_close":
                    gen_end_reason = "final_channel_close"
                    self._h_final_channel_stops += 1
                    log.debug(
                        "🛑 final-channel stop: turn ended at non-empty final "
                        "close (%d tokens)",
                        len(completion_tokens),
                    )

                # Yield only the *new* bytes that form valid UTF-8 (pipeline
                # holds back incomplete multi-byte chars; buffer_mode holds
                # everything until flush).
                text = pipeline.pop_text()
                if text is not None:
                    yield text

                if verdict.stop or len(completion_tokens) >= effective_max:
                    break

            # Flush any remaining bytes (final multi-byte char or buffered-mode
            # content). Preserve a specific in-loop reason (e.g.
            # final_channel_close) — only plain budget/EOG exits fall through
            # to "completed".
            if gen_end_reason is None:
                gen_end_reason = "completed"
            _tail = pipeline.flush()
            if _tail:
                yield _tail

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
            instance._last_cache_hit = (
                bool(flow_hit_telemetry) or int(kv_base) > self._resident_static_len
            )
            # A decode completed, so whatever was wrong is no longer wrong.
            # Resetting here is what keeps the unservable escalation aimed at
            # a CONSECUTIVE heal/fail loop rather than at a slow drip of
            # unrelated transients accumulating over a long run.
            if self._consecutive_unhealed_decode_failures:
                log.info(
                    "✅ decode recovered after %d unhealed failure(s) — counter reset",
                    self._consecutive_unhealed_decode_failures,
                )
                self._consecutive_unhealed_decode_failures = 0
            instance._healed_since_failure = False
        except RuntimeError as e:
            # llama_decode -3 (GGML_STATUS_FAILED) latches the Metal backend:
            # ggml-metal sets a sticky has_error on any failed command buffer
            # and every later decode on this CONTEXT fails until it is
            # recreated. Mark the instance so release/acquire heals it via a
            # targeted context refresh; -2 (alloc failed) gets the same cure.
            if self._is_fatal_decode(e):
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
                pipeline.dump_capture(
                    "abandoned by consumer (watchdog cancel or disconnect)"
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
                agen = self._batched_stream(
                    instance, prompt_tokens, max_tokens, temperature, **kwargs
                )
                try:
                    async for chunk in agen:
                        parts.append(chunk)
                finally:
                    # Deterministic finalization: a consumer cancel (agent
                    # watchdog) abandons the delegated asyncgen mid-yield and
                    # its finally (bridge.closed + engine.cancel) would defer
                    # to GC — the engine keeps decoding into a dead bridge and
                    # this guard never exits (the phantom in_flight wedge).
                    # aclose() runs it NOW; shielded so a pending cancel can't
                    # skip it (the pool branch's close-before-release pattern).
                    with anyio.CancelScope(shield=True):
                        await agen.aclose()
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
        abandons the stream (agent-side watchdog cancel, disconnect), the
        finally deterministically closes the sync generator — waiting out
        any in-flight next() on a worker thread — BEFORE the guard drops,
        so the instance is never re-acquired while the old stream can
        still touch its sampling state. Lazy asyncgen finalization paths
        (a cancelled caller that never runs our finally promptly) are
        covered by the entry check, which retires a registered prior
        stream before the new stream's first token.

        ``_nested_guard``: see generate_async.
        """
        nested = kwargs.pop("_nested_guard", False)
        async with self.generation_guard(nested=nested):
            self._h_requests_since_refresh += 1  # drives the periodic context refresh
            if self._decode_mode == "batched":
                agen = self._batched_stream(
                    instance, prompt_tokens, max_tokens, temperature, **kwargs
                )
                try:
                    async for chunk in agen:
                        yield chunk
                finally:
                    # See generate_async: run _batched_stream's finally NOW,
                    # shielded, instead of at GC. (If OUR consumer abandons
                    # this generator lazily too, this still fires at its
                    # finalization — the seat reaper backstops the gap.)
                    with anyio.CancelScope(shield=True):
                        await agen.aclose()
                return
            # An abandoned prior stream on this instance is a live hazard:
            # its deferred GeneratorExit (the worker thread's in-flight
            # next() keeps it alive past cancellation, GC closes it later)
            # tears down shared sampling state mid-flight under whoever
            # decodes next — live: a watchdog-cancelled 33k-token runaway's
            # close fired 1.1s into the NEXT request on the same instance
            # → copy_logits(None) → the mission made no inference progress
            # for 23 minutes. Retire it before the new stream's first token.
            prior = getattr(instance, "_active_stream_gen", None)
            if prior is not None:
                log.warning(
                    "🧹 retiring abandoned stream on instance [%s] before reuse",
                    getattr(instance, "_persona", "default"),
                )
                if not await run_in_threadpool(self._close_stream_gen, prior):
                    self._mark_decode_failure(
                        instance,
                        RuntimeError("abandoned stream would not close"),
                    )
                instance._active_stream_gen = None
            sync_gen = self.generate_stream_sync(
                instance, prompt_tokens, max_tokens, temperature, **kwargs
            )
            instance._active_stream_gen = sync_gen
            try:
                async for chunk in iterate_in_threadpool(sync_gen):
                    yield chunk
            finally:
                # Close-before-release: whether the stream completed, errored,
                # or the consumer abandoned it (watchdog cancel), the sync
                # generator must be fully closed — its finally run, no thread
                # still inside instance.generate — before the guard drops and
                # the instance can serve again. Shielded: cancellation of the
                # surrounding task must not skip this.
                with anyio.CancelScope(shield=True):
                    closed = await run_in_threadpool(self._close_stream_gen, sync_gen)
                if getattr(instance, "_active_stream_gen", None) is sync_gen:
                    instance._active_stream_gen = None
                if not closed:
                    self._mark_decode_failure(
                        instance,
                        RuntimeError("stream generator would not close after abandon"),
                    )

    @staticmethod
    def _close_stream_gen(gen: Any, timeout_s: float = 30.0) -> bool:
        """Deterministically close a sync stream generator, waiting out an
        in-flight ``next()``.

        A cancelled consumer leaves the generator in one of two states:
        suspended at a yield (``close()`` succeeds immediately, running its
        finally), or still EXECUTING inside ``next()`` on a threadpool
        worker computing one more token (``close()`` raises ValueError —
        generators are not thread-safe). Poll until the in-flight call
        returns, then close. Returns False only if the generator never
        stopped executing within ``timeout_s`` — the caller must then
        flag the instance for a context refresh instead of reusing it.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                gen.close()
                return True
            except ValueError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            except Exception:  # noqa: BLE001 — the generator's finally raised
                log.exception("stream generator close raised (state retired anyway)")
                return True

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
        # Stateless flow cache under batched (2026-07-30; was a documented
        # pool-only deferral). HIT = install the pinned [static + flow head]
        # onto this fresh seat and prefill only the tail; MISS = request a
        # BUILD — this stream prefills [head + tail] normally and, on a NORMAL
        # completion, the decode thread range-copies [0, prefix_len) onto the
        # flow band. Measured payout on glm (pool, same mechanism):
        # 3.51s/call = 49% of prefill at realistic head sizes.
        flow_key = kwargs.pop("flow_key", None)
        flow_prefix_len = int(kwargs.pop("flow_prefix_len", 0) or 0)
        flow_build = None
        flow_hit = False
        if (
            flow_key
            and static_in_prompt
            and 0 < flow_prefix_len <= len(prompt_tokens)
            and self._engine is not None
            and len(self._batched_seq_map().flow_seqs)
        ):
            _fresh_seat = int(seat.n_tokens or 0) == int(seat.static_len)
            _pin = self._engine._flow_pins.get(flow_key)
            if _pin is not None and _fresh_seat and _pin.n_tokens == flow_prefix_len:
                self._engine.install_flow_sync(seat, _pin)
                flow_hit = True
            elif _fresh_seat:
                flow_build = (
                    flow_key,
                    flow_prefix_len,
                    list(prompt_tokens[:flow_prefix_len]),
                )
        # Sampling overrides are pool-only in v1 (the degen-retry path); the
        # batched engine's per-stream sampling doesn't take them yet.
        if kwargs.pop("sampling_overrides", None):
            log.debug("sampling_overrides is pool-only — ignored in batched mode")

        # Per-request reasoning level for STATELESS completions (batched
        # parity with generate_stream_sync). Session turns never carry the
        # kwarg. Preconditions: fresh seat (only its head in KV — the same
        # contract the static split below already assumes), pinned level head
        # of equal length. Install head + swap prompt head tokens together so
        # the split stays exact; restore the persona head afterward so the
        # next request on this seat sees its contract intact.
        _reasoning = kwargs.pop("reasoning", None)
        _restore_head = None
        if flow_hit and _reasoning:
            # Pool parity: the reasoning head-swap is refused under a pinned
            # flow prefix — the flow head REPLACED the seat's whole content and
            # a whole-seq reasoning install would throw the pin away.
            log.debug(
                "completion reasoning=%s refused (flow prefix pinned)", _reasoning
            )
            _reasoning = None
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
                    prompt_tokens[_level_head.n_tokens :]
                )
                _restore_head = _persona_head
                self._h_reasoning_swaps += 1
                log.info(
                    "🧠 completion head-swap → %s (seat seq %d, batched)",
                    _level,
                    seat.seq,
                )
            else:
                log.debug(
                    "completion reasoning=%s refused (batched preconditions)", _level
                )

        if flow_hit:
            n_static = flow_prefix_len
        else:
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
        # Think-hold: request-level close-tag ban for the first N tokens
        # (inference/think_hold.py). Consumed by _build_sampling_params as a
        # per-stream CustomSampler.
        _think_hold = kwargs.pop("think_hold", None)
        if _think_hold:
            sampling_kwargs["think_hold"] = dict(_think_hold)
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
            flow_build=flow_build,
            flow_hit=flow_hit,
            flow_key=str(flow_key or "") if (flow_build or flow_hit) else "",
        )
        stream_id = self._engine.submit(req)
        log.info(
            "🔧 batched stream %s: dynamic=%d tok, kv_base=%d, max_gen=%d "
            "[seq %d, %s]",
            stream_id,
            len(dynamic_tokens),
            kv_base,
            max_tokens,
            seat.seq,
            req.persona,
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
            # quiet: under batched concurrency the shared status blends
            # streams — the ENGINE reports the truthful per-stream
            # completion at retirement (batched_engine._retire →
            # report_completion). This finish only closes the liveness meter.
            tracker.finish(quiet=True)

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
        available = (
            sum(q.qsize() for q in self._persona_queues.values())
            if (self._persona_queues)
            else (self._pool_queue.qsize() if self._pool_queue else 0)
        )
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
        # Real KV token budget for client-side admission gates (the swarm
        # pool-fit gate): the SHARED pool in batched mode, the per-instance
        # context in pool mode — a client treating it as shared under-admits
        # there, which is the safe direction. pool_size is the SEAT count;
        # without this field clients had to hardcode the geometry (the 48k
        # gemma pool was gated against gpt-oss's 131k).
        n_ctx = getattr(getattr(self.config, "model", None), "n_ctx", None)
        if n_ctx:
            info["kv_pool_tokens"] = int(n_ctx)
        # The OTHER context limit (the swarm/model split): the per-stream
        # trained ceiling every single stream is bounded by.
        stream_lim = self._stream_ctx_limit()
        if stream_lim:
            info["model_max_context"] = stream_lim
        if self._engine is not None:
            # Batched engine internals; a fatal latch flips overall status
            # so dashboards/soaks see the outage without new fields.
            engine_health = self._engine.health()
            info["batched_engine"] = engine_health
        stats = getattr(self, "_vision_batched_stats", None)
        if stats:
            # health() is the free dict — NEVER capacity_fields() (a frozen
            # snapshot; an unknown key silently kills every publish).
            info["vision_batched"] = dict(stats)
            if engine_health.get("engine_fatal"):
                info["status"] = "error"

        # ── Deep-health: long-run degradation signals ──────────────────
        # Memory residency/pressure — the Apple-Silicon unified-memory KV/weight
        # eviction failure mode shows here as falling RSS / rising system %
        # while a long run is active (cf. flow-cache fallbacks below).
        try:
            import psutil

            vm = psutil.virtual_memory()
            info["mem_process_rss_mb"] = round(
                psutil.Process().memory_info().rss / 1e6, 1
            )
            info["mem_system_used_percent"] = vm.percent
            info["mem_system_available_mb"] = round(vm.available / 1e6, 1)
            # Wired (unevictable) bytes — on macOS a drop here while the model is
            # loaded means weights/KV got evicted to the compressor.
            wired = getattr(vm, "wired", None)
            if wired is not None:
                info["mem_system_wired_mb"] = round(wired / 1e6, 1)
        except Exception:
            pass
        # Rebuild memory black box (2026-08-03 swarm kill). The scalars above
        # are a spot reading; this is the windowed record around each context
        # rebuild — the one interval where a multi-GB free and a multi-GB
        # allocation happen back to back, and the only place a fatal transient
        # can hide. `headroom_low_mb_trend` is the load-bearing field: a floor
        # walking downward across rebuilds is the shape that precedes a death
        # and is invisible in any single record.
        try:
            from core.mem_probe import health_block

            info["rebuild_memory"] = health_block(self)
        except Exception:  # noqa: BLE001 — health must never fail on telemetry
            pass
        # Flow/resident KV-cache health: live size + cumulative churn. A rising
        # fallback rate is the canary for KV-cache instability under pressure.
        # flow_cache_entries = live seq-ops pins (per-instance pool hot-sets +
        # the batched band); the M8 blob registry this used to count was
        # deleted 2026-07-30.
        info["flow_cache_entries"] = sum(
            len(getattr(inst, "_flow_seqs", {}) or {}) for inst in self._all_instances
        ) + len(getattr(getattr(self, "_engine", None), "_flow_pins", {}) or {})
        info["resident_active"] = self._resident_active
        # Session KV strategy, readable without parsing the load log. The probe
        # needs the EFFECTIVE strategy and the arch answer separately: "requested
        # but denied" and "never requested" both leave resident inactive, and
        # only the first is a bug.
        info["session_strategy"] = self._session_strategy()
        info["session_can_shift"] = self._session_can_shift
        info["resident_requested"] = self._resident_requested
        # The TRUE per-seq window (0 = undetermined). Under kv_unified:false a
        # fragmented context gives each seq n_ctx/n_seq_max cells; a consumer
        # sizing work against n_ctx alone repeats the hy3 sweep failure.
        #
        # READ THE CACHE, NEVER THE LIVE CONTEXT (2026-08-09). This line used
        # to call _seq_ctx_limit(_p._ctx, ...) → llama_n_ctx_seq through
        # ctypes. n_ctx_seq is immutable for a built context, so the probe
        # bought nothing — and a health query landing mid-rebuild (refresh or
        # latch heal, which free the context before recreating it)
        # dereferenced the freed pointer and killed the SERVER: three
        # identical crash reports on the hy3 run, all
        # `libllama!llama_n_ctx_seq, KERN_INVALID_ADDRESS at 0xc`, each
        # ending a multi-hour arm. The try/except below is powerless against
        # a native SIGSEGV — it never caught one. Every build and rebuild
        # site now caches _n_ctx_seq; this reads that int.
        try:
            _p = self._primary_instance
            info["n_ctx_seq"] = int(getattr(_p, "_n_ctx_seq", 0) or 0)
        except Exception:  # noqa: BLE001
            info["n_ctx_seq"] = 0
        _eng = getattr(self, "_engine", None)
        info["flow_builds"] = self._h_flow_builds + getattr(_eng, "h_flow_builds", 0)
        info["flow_hits"] = self._h_flow_hits + getattr(_eng, "h_flow_hits", 0)
        info["flow_evicts"] = self._h_flow_evicts + getattr(_eng, "h_flow_evicts", 0)
        info["flow_fallbacks"] = self._h_flow_fallbacks
        info["runaway_captures"] = self._h_runaway_captures
        info["decode_failures"] = self._h_decode_failures
        info["unhealed_decode_failures"] = self._consecutive_unhealed_decode_failures
        info["unservable"] = self._unservable
        info["latch_heals"] = self._h_latch_heals
        info["final_channel_stops"] = self._h_final_channel_stops
        info["context_refreshes"] = self._h_context_refreshes
        info["requests_since_refresh"] = self._h_requests_since_refresh
        # Starvation visibility (2026-08-07): a 45h run deferred every
        # refresh invisibly — this makes the deferral count queryable.
        info["refresh_deferred"] = self._h_refresh_deferred
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
