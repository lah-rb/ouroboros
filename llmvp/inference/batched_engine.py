"""Batched single-context decode engine (decode_mode: "batched").

ONE llama_context, N working sequences, one llama_decode per step carrying
one token per active generation stream plus chunked prefill for joining
streams — the llama-server ``update_slots`` pattern in Python. This is the
only shape that raises aggregate decode throughput on Metal: all contexts
on a device share a single MTLCommandQueue, so the pool's N-context
"simultaneous" decode serializes on the GPU anyway (and concurrent
submission trips the driver's command-buffer accounting — see
dev/archive/docs/CACHE_STATE.md). Batching reads the weights once per step for every
stream instead of once per stream.

Threading contract: the dedicated decode thread owns ALL context
operations — decode, sampling, and every memory_seq_* call. Everything
else talks to it through three inboxes drained between steps:

- ``submit()``   — join queue for new generation streams
- ``control()``  — KV surgery on idle seqs (purge / window / head-swap /
                   seat acquire+release), returns a concurrent Future
- ``pause()``    — step-boundary drain for context refresh/rebuild

Output flows through per-stream ``OutputBridge``s
(loop.call_soon_threadsafe -> asyncio.Queue), so GraphQL consumers stay
pure-async and never touch the context.

Seq map: ``[0..W)`` working seats, ``[W..W+P)`` persona static heads,
``[W+P..W+P+R)`` reasoning heads. Any seat serves any persona — a seat is
prepared by forking the persona head (memory_seq_cp), so there is no
per-persona slot starvation.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from inference.decode_constants import BUFFER_MODE_MAX_TOKENS
from inference.token_pipeline import (  # noqa: F401 — Verdict re-exported
    TokenPipeline,
    Verdict,
    build_capture_meta,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Seq map
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SeqMap:
    """Sequence-id layout for the single batched context."""

    n_working: int
    persona_seqs: Dict[str, int]  # persona name -> pinned static-head seq
    reasoning_seqs: Dict[str, int]  # reasoning level -> pinned head seq
    n_seq_max: int

    def working_seqs(self) -> range:
        return range(self.n_working)


def plan_seq_map(
    n_working: int,
    personas: List[str],
    reasoning_levels: List[str],
) -> SeqMap:
    """Lay out the seq bands: working seats, persona heads, reasoning heads.

    Pure function so the arithmetic is unit-testable without a model.
    "default" is always a persona (the legacy prompt/knowledge head).
    """
    if n_working < 1:
        raise ValueError(f"n_working must be >= 1, got {n_working}")
    names = list(dict.fromkeys(["default", *personas]))  # ordered, deduped
    persona_seqs = {name: n_working + i for i, name in enumerate(names)}
    base = n_working + len(names)
    reasoning_seqs = {lvl: base + i for i, lvl in enumerate(reasoning_levels)}
    return SeqMap(
        n_working=n_working,
        persona_seqs=persona_seqs,
        reasoning_seqs=reasoning_seqs,
        n_seq_max=base + len(reasoning_levels),
    )


@dataclass
class PersonaHead:
    """A pinned persona static head: its seq id and the tokens it holds."""

    name: str
    seq: int
    tokens: List[int]

    @property
    def n_tokens(self) -> int:
        return len(self.tokens)


# ----------------------------------------------------------------------
# Stream plumbing
# ----------------------------------------------------------------------


class StreamPhase(Enum):
    PREFILL = "prefill"
    DECODING = "decoding"
    DONE = "done"


class RetriableEngineError(RuntimeError):
    """A shared-context failure killed this stream through no fault of its
    own (latch rebuild, KV-pressure eviction). Safe to retry once the
    engine reports healthy."""

    retriable = True


class OutputBridge:
    """Decode-thread -> asyncio consumer channel for one stream.

    The decode thread only ever calls the producers (which hop through
    ``loop.call_soon_threadsafe``); the consumer side is a plain async
    iterator. ``_END`` is the stream-end sentinel; an exception instance
    is a terminal error."""

    _END = object()

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self.closed = False  # set by the consumer side on abandonment

    # -- producer side (decode thread) ---------------------------------
    def emit(self, text: str) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, text)

    def finish(self, error: Optional[BaseException] = None) -> None:
        item = error if error is not None else self._END
        self._loop.call_soon_threadsafe(self._queue.put_nowait, item)

    # -- consumer side (event loop) -------------------------------------
    async def __aiter__(self):
        while True:
            item = await self._queue.get()
            if item is self._END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item


@dataclass
class SeqSlot:
    """One working seat: a seq id plus the per-seq state that a pool
    instance used to carry for its whole context.

    Duck-types the narrow surface the session layer reads off a pinned
    pool instance (n_tokens, _n_ctx, input_ids, _last_* telemetry,
    _needs_context_refresh) so session_manager needs only the explicit
    KV-surgery touchpoints changed.
    """

    seq: int
    persona: str = "default"
    n_tokens: int = 0  # decoded position (== static head len when fresh)
    static_len: int = 0
    input_ids: List[int] = field(default_factory=list)
    pinned: bool = False  # held by a session between turns
    dead: bool = False  # context rebuilt underneath this seat
    # Lease stamp (monotonic) set at acquire, cleared at release — read by
    # the backend's seat reaper to reclaim seats leaked by cancelled
    # consumers whose release never ran.
    _leased_at: Optional[float] = None
    _n_ctx: int = 0
    _needs_context_refresh: bool = False
    # Per-request telemetry contract (read by core/inference.py and
    # session_manager after each generate call).
    _last_completion_tokens: Optional[List[int]] = None
    _last_gen_start_pos: int = 0
    _last_kv_base: int = 0
    _last_dynamic_len: int = 0
    _last_flow_hit: bool = False
    _last_flow_key: str = ""
    _last_cache_hit: bool = False
    # Why the last stream ended. "" for an ordinary stop; the KV-pressure
    # force-window sets it so the caller can mark the response truncated —
    # that cut lands BELOW max_tokens, so the derived truncation test misses it.
    _last_end_reason: str = ""
    # Per-stream wall spans (seconds) — concurrency-accurate replacements
    # for the global tracker's eval/generation durations.
    _last_prefill_s: float = 0.0
    _last_decode_s: float = 0.0
    # Engine backref (set at seat creation) — lets session-layer KV surgery
    # route through the decode thread without knowing about the engine.
    _engine_ref: Any = field(default=None, repr=False)

    def purge_to(self, pos: int) -> None:
        """Drop this seat's KV from ``pos`` to the end and rewind the
        position — the per-seq degenerate-turn purge (session layer calls
        this instead of the pool's seq-0 memory_seq_rm). Blocking; callers
        run it in a threadpool."""
        slot = self

        def _do() -> None:
            slot._engine_ref._llama._ctx.memory_seq_rm(slot.seq, pos, -1)
            del slot.input_ids[pos:]
            slot.n_tokens = pos

        slot._engine_ref.control(_do).result(timeout=30)


@dataclass
class StreamRequest:
    """A generation request as submitted to the engine (event-loop side).

    ``prompt_tokens`` are the DYNAMIC tokens only — the persona static
    head (stateless) or the live session KV (seat-attached) is already
    resident on the seq the stream will decode into.
    """

    prompt_tokens: List[int]
    max_tokens: int
    sampling_kwargs: Dict[str, Any]  # _build_generate_kwargs shape (no "reset")
    out: OutputBridge
    persona: str = "default"
    slot: Optional[SeqSlot] = None  # pre-acquired seat (None never valid in v1)
    stop_texts: List[str] = field(default_factory=list)
    session_mode: bool = False
    grammar: Any = None
    seed: Optional[int] = None
    request_id: str = ""
    temperature: float = 0.0
    kv_base: int = 0  # KV skipped (static head / restored session occupancy)


def build_sampling_params(
    sampling_kwargs: Dict[str, Any],
    *,
    seed: Optional[int],
    fallback_seed: int,
    grammar: Any = None,
) -> Any:
    """Mirror of what ``Llama.generate`` builds internally
    (llama.py:1692-1748): a ``LlamaSamplingParams`` carrying OUR knobs
    (the _build_generate_kwargs surface) with the binding's defaults for
    everything else — the defaults of LlamaSamplingParams and of
    generate()'s signature are identical, verified against the fork.
    """
    from llama_cpp._internals import LlamaSamplingParams

    params = LlamaSamplingParams(
        temp=float(sampling_kwargs.get("temp", 0.80)),
        top_p=float(sampling_kwargs.get("top_p", 0.95)),
        top_k=int(sampling_kwargs.get("top_k", 40)),
        min_p=float(sampling_kwargs.get("min_p", 0.05)),
        penalty_present=float(sampling_kwargs.get("present_penalty", 0.0)),
        penalty_repeat=float(sampling_kwargs.get("repeat_penalty", 1.0)),
        grammar=grammar._grammar if grammar else "",
        seed=seed if seed is not None else fallback_seed,
    )

    # OPT-IN KNOBS. Everything above is always set; everything below is applied
    # only when the config asked for it, so an untouched model's sampler chain
    # is byte-identical to before.
    #
    # These were being DROPPED in batched mode. `_build_generate_kwargs` has
    # emitted penalty_last_n and the DRY keys since the qwen loop work, and the
    # alternating-pool path forwards them to Llama.generate() — but this builder
    # only ever read 8 fields, so under `decode_mode: batched` (the default) the
    # loop mitigations were silently inert. Found while asking whether
    # repeat_penalty affects laguna: some of them could not, because they never
    # reached the sampler (2026-07-26).
    for key, cast in (
        ("penalty_last_n", int),
        ("penalty_freq", float),
        ("dry_multiplier", float),
        ("dry_base", float),
        ("dry_allowed_length", int),
        ("dry_penalty_last_n", int),
    ):
        if sampling_kwargs.get(key) is not None:
            setattr(params, key, cast(sampling_kwargs[key]))

    # Reasoning budget — hard cap on thinking length. On overrun the sampler
    # FORCES reasoning_end, so an answer still follows instead of the model
    # reasoning to max_tokens and returning nothing (laguna's failure mode).
    # -1 = unrestricted (library default), 0 = end immediately, N > 0 = budget.
    rb = sampling_kwargs.get("reasoning_budget")
    if rb is not None:
        params.reasoning_budget = int(rb)
        for key in ("reasoning_start", "reasoning_end"):
            if sampling_kwargs.get(key):
                setattr(params, key, str(sampling_kwargs[key]))

    # Logit bias — {token_id: bias}; -inf effectively bans a token. Used to
    # suppress native tool-call tokens on models that emit them unprompted.
    bias = sampling_kwargs.get("logit_bias")
    if bias:
        import llama_cpp as _lc

        params.logit_bias = [
            _lc.llama_logit_bias(token=int(tok), bias=float(val))
            for tok, val in dict(bias).items()
        ]

    return params


@dataclass
class StreamState:
    """Live engine-side state of one stream (decode thread only)."""

    stream_id: str
    req: StreamRequest
    slot: SeqSlot
    pipeline: TokenPipeline
    sampling: Any  # LlamaSamplingContext
    has_grammar: bool
    effective_max: int
    phase: StreamPhase = StreamPhase.PREFILL
    n_past: int = 0
    prompt_pos: int = 0
    last_token: Optional[int] = None
    i_batch: int = -1
    completion_tokens: List[int] = field(default_factory=list)
    gen_start_pos: int = 0
    mark: tuple = (0, 0)  # (n_past, prompt_pos) rewind point per step
    t_submit: float = 0.0
    t_first_token: Optional[float] = None
    t_prefill_done: Optional[float] = None
    end_reason: Optional[str] = None


# ----------------------------------------------------------------------
# Engine
# ----------------------------------------------------------------------

_MIN_PREFILL_BUDGET = 16

# Smallest generation worth admitting. Below this the turn burns a seat, a
# prefill and a round-trip to produce nothing usable, so the request waits for
# capacity instead.
_MIN_ADMIT_BUDGET = 512
# End reason for a stream ended early by KV pressure. Callers MUST be able to
# tell this from a natural stop: the response carries real content and stops
# BELOW max_tokens, so the derived "tokens_generated >= max_tokens" truncation
# test does not fire for it.
_END_KV_PRESSURE = "kv_pressure_truncated"
# Held back from the pool when sizing admissions: the batch being decoded, the
# transient cells of a stream mid-join, and ordinary accounting drift. The
# reactive ladder (_relieve_pressure) remains the backstop — this is a margin,
# not a guarantee.
_POOL_SLACK = 256


class _AdmitVerdict(Enum):
    ADMIT = "admit"
    QUEUE = "queue"  # no room now; retry when a stream retires
    IMPOSSIBLE = "impossible"  # cannot fit even against pinned-only occupancy


class BatchedEngine:
    """Owns the single context's decode thread and all seq lifecycle."""

    def __init__(
        self,
        llama: Any,
        seq_map: SeqMap,
        *,
        n_batch: int,
        prefill_chunk: Optional[int] = None,
        persona_heads: Optional[Dict[str, PersonaHead]] = None,
        capture_dir: str = "./logs",
        sampler_factory: Optional[Callable[[StreamRequest], Any]] = None,
        is_eog: Optional[Callable[[int], bool]] = None,
        seats: Optional[List[SeqSlot]] = None,
        rebuild_fn: Optional[Callable[[], None]] = None,
    ):
        self._llama = llama  # the primary Llama instance (owns ctx/model)
        self._seq_map = seq_map
        self._n_batch = n_batch
        self._prefill_chunk = min(prefill_chunk or n_batch, n_batch)
        self._live_prefill_budget = self._prefill_chunk
        self._persona_heads: Dict[str, PersonaHead] = dict(persona_heads or {})
        self._reasoning_heads: Dict[str, PersonaHead] = {}  # level -> head
        self._capture_dir = capture_dir
        # Injectable llama_cpp touchpoints (unit tests run without the
        # native lib; production uses the defaults below).
        self._sampler_factory = sampler_factory or self._default_sampler_factory
        self._is_eog = is_eog or self._default_is_eog
        # All working seats (for fatal-latch flagging) + the backend's
        # context-rebuild hook (runs ON the decode thread after a latch).
        self._seats: List[SeqSlot] = list(seats or [])
        self._rebuild_fn = rebuild_fn

        self._streams: Dict[str, StreamState] = {}
        self._waiting: List[StreamRequest] = []  # admitted when a seat frees
        self._batch: Any = None  # own LlamaBatch, lazily allocated

        self._join_inbox: List[StreamRequest] = []
        self._control_inbox: List[tuple] = []  # (fn, Future)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)

        self._paused = False
        self._drained = threading.Event()
        self._shutdown = False
        self._fatal: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None

        # Health counters (read by the backend's get_health_status)
        self._h_steps = 0
        self._h_kv_pressure_events = 0
        self._h_forced_windows = 0
        self._h_evictions = 0
        self.h_runaway_captures = 0
        self.h_final_channel_stops = 0
        self.h_decode_failures = 0
        self.h_latch_heals = 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="llmvp-decode", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        with self._wake:
            self._shutdown = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    # -- inboxes (any thread) ---------------------------------------------

    def submit(self, req: StreamRequest) -> str:
        stream_id = uuid.uuid4().hex[:12]
        req.request_id = req.request_id or stream_id
        req._stream_id = stream_id  # type: ignore[attr-defined]
        with self._wake:
            if self._shutdown or self._fatal is not None:
                raise RetriableEngineError(
                    f"engine unavailable ({self._fatal or 'shut down'})"
                )
            self._join_inbox.append(req)
            self._wake.notify_all()
        return stream_id

    def cancel(self, stream_id: str) -> None:
        """Consumer-side abandonment: retire the stream at the next step
        boundary (never blocks the caller)."""

        def _do_cancel() -> None:
            s = self._streams.get(stream_id)
            if s is not None and s.phase is not StreamPhase.DONE:
                self._retire_abandoned(s)

        try:
            self.control(_do_cancel)
        except RuntimeError:
            pass  # engine already down — stream is dead anyway

    def control(self, fn: Callable[[], Any]) -> "concurrent.futures.Future":
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._wake:
            if self._shutdown:
                raise RuntimeError("engine is shut down")
            self._control_inbox.append((fn, fut))
            self._wake.notify_all()
        return fut

    def evict_all_streams(self, reason: str) -> int:
        """Retire EVERY live stream with a retriable error (context-refresh
        drain deadline). Runs on the decode thread via the control inbox —
        the same retire path as KV-pressure eviction, so clients recover
        through their existing retry logic. Returns the victim count."""

        def _do() -> int:
            victims = [
                s
                for s in list(self._streams.values())
                if s.phase is not StreamPhase.DONE
            ]
            for s in victims:
                self._retire(s, error=RetriableEngineError(reason))
            return len(victims)

        return self.control(_do).result(timeout=30.0)

    def pause(self, timeout: float = 60.0) -> None:
        """Park the decode thread at the next step boundary."""
        self._drained.clear()
        with self._wake:
            self._paused = True
            self._wake.notify_all()
        if not self._drained.wait(timeout=timeout):
            raise TimeoutError("decode thread did not reach a step boundary")

    def resume(self) -> None:
        with self._wake:
            self._paused = False
            self._wake.notify_all()

    # -- decode thread ----------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._wake:
                while not (
                    self._shutdown
                    or self._paused
                    or self._join_inbox
                    or self._control_inbox
                    or self._has_active_streams()
                    # A queued admission must not sleep here: with no active
                    # streams the pool is at its emptiest, so _drain_waiting
                    # will succeed on the next pass. Without this the engine
                    # can idle forever holding work it is able to run.
                    or self._waiting
                ):
                    self._wake.wait()
                if self._shutdown:
                    self._fail_all(RetriableEngineError("engine shut down"))
                    return
                if self._paused:
                    self._drained.set()
                    while self._paused and not self._shutdown:
                        self._wake.wait()
                    continue
                controls = self._control_inbox
                self._control_inbox = []
                joiners = self._join_inbox
                self._join_inbox = []

            for fn, fut in controls:
                if fut.set_running_or_notify_cancel():
                    try:
                        fut.set_result(fn())
                    except BaseException as exc:  # surfaced via the Future
                        fut.set_exception(exc)
            for req in joiners:
                self._admit(req)
            self._sweep_closed_bridges()
            # Capacity may have freed since the last pass (a stream retired, a
            # session released its seat) — retry anything parked for room.
            self._drain_waiting()
            if self._has_active_streams():
                try:
                    self._step()
                except RuntimeError as exc:
                    self._on_fatal(exc)

    def _has_active_streams(self) -> bool:
        return any(s.phase is not StreamPhase.DONE for s in self._streams.values())

    def _sweep_closed_bridges(self) -> None:
        for s in list(self._streams.values()):
            if s.req.out.closed and s.phase is not StreamPhase.DONE:
                self._retire_abandoned(s)

    # -- admission ----------------------------------------------------------

    def _pinned_occupancy(self) -> int:
        """Cells that will still be held after every evictable stream is gone.

        Pinned session seats keep their KV between turns by design, and each
        persona's static head is resident. This is the irreducible floor — a
        request that cannot fit above it can never be admitted, however long it
        waits, so it is failed rather than queued forever.
        """
        held = 0
        for seat in self._seats:
            if seat.pinned:
                held += max(int(seat.n_tokens), int(seat.static_len))
            else:
                held += int(seat.static_len)
        return held

    def _live_occupancy(self, exclude: Optional[StreamRequest] = None) -> int:
        """Cells currently spoken for by live streams, counting what each is
        ENTITLED to rather than what it has decoded so far.

        Counting `n_past` would under-count: a stream 500 tokens into a 40k
        budget will take those 40k, and admitting against its current position
        is exactly how the pool oversubscribes.
        """
        held = 0
        for s in self._streams.values():
            if s.phase is StreamPhase.DONE or (exclude is not None and s.req is exclude):
                continue
            held += int(s.gen_start_pos) + int(s.effective_max)
        return held

    def _free_cells(self, n_ctx: int, exclude: Optional[StreamRequest] = None) -> int:
        """Cells available to a new stream right now."""
        if not n_ctx:
            return 0
        # Pinned seats are already counted inside _live_occupancy when they have
        # a live stream; take the larger of the two views rather than summing,
        # which would double-count a pinned seat mid-turn.
        occupied = max(self._live_occupancy(exclude), self._pinned_occupancy())
        return max(0, n_ctx - occupied - _POOL_SLACK)

    def _size_against_pool(
        self, req: StreamRequest, n_ctx: int, total: int
    ) -> "tuple[int, _AdmitVerdict]":
        """(effective_max, verdict) under the shrink-to-fit / queue-below-floor
        policy. Falls back to today's per-stream bound when the pool size is
        unknown."""
        per_stream = min(req.max_tokens, (n_ctx - total) if n_ctx else req.max_tokens)
        if not n_ctx:
            return per_stream, _AdmitVerdict.ADMIT

        free = self._free_cells(n_ctx, exclude=req) - len(req.prompt_tokens)
        if free >= per_stream:
            return per_stream, _AdmitVerdict.ADMIT
        if free >= _MIN_ADMIT_BUDGET:
            # Shrink: a smaller generation that COMPLETES beats a larger one
            # that gets evicted, because eviction discards everything.
            logger.info(
                "✂️ stream admitted at %d tokens (asked %d) — %d free cells",
                free,
                per_stream,
                free,
            )
            return free, _AdmitVerdict.ADMIT
        # Can it ever fit? Compare against the irreducible floor.
        headroom = n_ctx - self._pinned_occupancy() - _POOL_SLACK
        if headroom - len(req.prompt_tokens) < _MIN_ADMIT_BUDGET:
            return 0, _AdmitVerdict.IMPOSSIBLE
        return 0, _AdmitVerdict.QUEUE

    def _drain_waiting(self) -> None:
        """Retry queued admissions once capacity may have freed. FIFO, and it
        stops at the first request that still does not fit so a large job is
        not starved by a queue of small ones behind it."""
        while self._waiting:
            req = self._waiting[0]
            if req.out.closed:
                self._waiting.pop(0)
                continue
            slot = req.slot
            n_ctx = (slot._n_ctx if slot else 0) or getattr(self._llama, "_n_ctx", 0)
            total = (int(slot.n_tokens) if slot else 0) + len(req.prompt_tokens)
            _, verdict = self._size_against_pool(req, n_ctx, total)
            if verdict is _AdmitVerdict.QUEUE:
                return  # still no room — leave it (and the rest) queued
            self._waiting.pop(0)
            self._admit(req)

    def _admit(self, req: StreamRequest) -> None:
        """Register a stream on its (pre-acquired) seat and build its
        per-stream sampler + pipeline. The seat's KV — persona head or live
        session — is already resident; only the dynamic prompt prefills."""
        slot = req.slot
        if slot is None:
            # v1 contract: callers acquire a seat first (acquire_instance),
            # exactly like the pool. Reaching here is a wiring bug.
            req.out.finish(RuntimeError("StreamRequest without a seat"))
            return

        n_ctx = slot._n_ctx or getattr(self._llama, "_n_ctx", 0)
        total = slot.n_tokens + len(req.prompt_tokens)
        if n_ctx and total >= n_ctx:
            req.out.finish(
                ValueError(f"Prompt ({total} tokens) exceeds context window ({n_ctx})")
            )
            return
        # Size against FREE cells, not the whole window. The line below used to
        # be `min(req.max_tokens, n_ctx - total)`, which is a per-STREAM bound —
        # but under batched decode n_ctx budgets the SUM of live streams, so
        # every stream can pass its own check while their total does not. That
        # is how a 48,318-token generation carrying 15 complete files reached
        # `KV cell pool exhausted` and was thrown away (2026-07-27 APEX arm).
        effective_max, verdict = self._size_against_pool(req, n_ctx, total)
        if verdict is _AdmitVerdict.QUEUE:
            # Not enough free cells for a useful generation. Park it; the decode
            # loop retries whenever capacity frees (a stream retires).
            self._waiting.append(req)
            logger.info(
                "⏳ stream queued: %d free cells < floor %d (pool %d, prompt %d)",
                self._free_cells(n_ctx, exclude=req),
                _MIN_ADMIT_BUDGET,
                n_ctx,
                total,
            )
            return
        if verdict is _AdmitVerdict.IMPOSSIBLE:
            req.out.finish(
                ValueError(
                    f"Request cannot fit the KV pool: prompt {total} tokens + "
                    f"a minimum {_MIN_ADMIT_BUDGET}-token generation exceeds "
                    f"the {n_ctx}-cell pool even with every evictable stream "
                    f"gone (resident/pinned occupancy "
                    f"{self._pinned_occupancy()} cells)."
                )
            )
            return
        buffer_mode = effective_max <= BUFFER_MODE_MAX_TOKENS

        try:
            sampling = self._sampler_factory(req)
        except Exception as exc:  # noqa: BLE001 — bad grammar etc.
            req.out.finish(exc)
            return

        guard = self._make_repetition_guard()
        final_stop = None
        if req.session_mode and self._family() == "harmony":
            from inference.final_channel_stop import FinalChannelStop

            final_stop = FinalChannelStop()

        # Lazy capture meta: the 768-token prompt-tail detok is paid at DUMP
        # time (rare), not per admission.
        def _meta(r=req):
            return build_capture_meta(
                self._llama, r.request_id, r.temperature, r.prompt_tokens
            )

        pipeline = TokenPipeline(
            self._llama,
            req.stop_texts,
            guard=guard,
            final_stop=final_stop,
            long_cycle_on=self._long_cycle_on(),
            buffer_mode=buffer_mode,
            capture_dir=self._capture_dir,
            capture_meta=_meta,
            initial_prior_tokens=req.prompt_tokens,
        )

        stream_id = getattr(req, "_stream_id", None) or uuid.uuid4().hex[:12]
        s = StreamState(
            stream_id=stream_id,
            req=req,
            slot=slot,
            pipeline=pipeline,
            sampling=sampling,
            has_grammar=req.grammar is not None,
            effective_max=effective_max,
            n_past=slot.n_tokens,
            gen_start_pos=slot.n_tokens + len(req.prompt_tokens),
            t_submit=time.monotonic(),
        )
        if not req.prompt_tokens:
            req.out.finish(ValueError("empty dynamic prompt"))
            return
        self._streams[stream_id] = s
        logger.info(
            "🧵 stream %s admitted [seq %d, %s]: dynamic=%d tok, kv_base=%d, "
            "max_gen=%d%s",
            stream_id,
            slot.seq,
            slot.persona,
            len(req.prompt_tokens),
            slot.n_tokens,
            effective_max,
            " (buffered)" if buffer_mode else "",
        )

    # -- the step loop -------------------------------------------------------

    def _step(self) -> None:
        batch = self._ensure_batch()
        batch.reset()
        rows = 0

        active = [s for s in self._streams.values() if s.phase is not StreamPhase.DONE]
        for s in active:
            s.mark = (s.n_past, s.prompt_pos)
            s.i_batch = -1

        # 1. Generation rows — one token per DECODING stream.
        for s in active:
            if s.phase is StreamPhase.DECODING and s.last_token is not None:
                batch.add_token(s.last_token, s.n_past, [s.slot.seq], True)
                s.i_batch = rows
                rows += 1
                s.slot.input_ids.append(s.last_token)
                s.n_past += 1

        # 2. Chunked prefill interleave (join order).
        budget = min(self._live_prefill_budget, self._n_batch - rows)
        for s in active:
            if s.phase is not StreamPhase.PREFILL or budget <= 0:
                continue
            take = min(budget, len(s.req.prompt_tokens) - s.prompt_pos)
            for _ in range(take):
                tok = s.req.prompt_tokens[s.prompt_pos]
                is_last = s.prompt_pos == len(s.req.prompt_tokens) - 1
                batch.add_token(tok, s.n_past, [s.slot.seq], is_last)
                if is_last:
                    s.i_batch = rows
                    s.phase = StreamPhase.DECODING
                    s.t_prefill_done = time.monotonic()
                rows += 1
                s.slot.input_ids.append(tok)
                s.n_past += 1
                s.prompt_pos += 1
            budget -= take

        if rows == 0:
            return

        # 3. Decode — fatal errors propagate to _run's handler; ret==1 is
        # KV pressure (recoverable): physically truncate every touched seq
        # back to its mark (sound even if the batch was PARTIALLY applied
        # across ubatches), rewind bookkeeping, and relieve pressure.
        ret = self._llama._ctx.decode(batch)
        self._h_steps += 1
        if ret == 1:
            self._h_kv_pressure_events += 1
            for s in active:
                mark_past, mark_pos = s.mark
                if s.n_past != mark_past:
                    self._llama._ctx.memory_seq_rm(s.slot.seq, mark_past, -1)
                    del s.slot.input_ids[
                        len(s.slot.input_ids) - (s.n_past - mark_past) :
                    ]
                    s.n_past, s.prompt_pos = mark_past, mark_pos
                    if s.prompt_pos < len(s.req.prompt_tokens):
                        s.phase = StreamPhase.PREFILL  # roll back mid-step join
                s.i_batch = -1
            self._relieve_pressure(active)
            return

        # 4. Sample + per-stream hooks.
        for s in sorted((x for x in active if x.i_batch >= 0), key=lambda x: x.i_batch):
            if s.phase is StreamPhase.DONE:
                continue
            tok = s.sampling.sample(self._llama._ctx, idx=s.i_batch)
            s.sampling.accept(tok, s.has_grammar)
            s.i_batch = -1

            if self._is_eog(tok):
                self._retire(s, reason="completed")
                continue

            if s.t_first_token is None:
                s.t_first_token = time.monotonic()
                self._tracker_call("mark_first_token")
            s.completion_tokens.append(tok)
            s.last_token = tok
            self._tracker_call("record_token")

            verdict = s.pipeline.feed(tok)
            if verdict.degenerate:
                self.h_runaway_captures += 1
                s.pipeline.dump_capture(verdict.degenerate)
                self._retire(
                    s,
                    error=_degenerate_error(
                        verdict.degenerate, len(s.completion_tokens)
                    ),
                )
                continue
            if verdict.end_reason == "final_channel_close":
                self.h_final_channel_stops += 1

            text = s.pipeline.pop_text()
            if text:
                s.req.out.emit(text)

            if verdict.stop or len(s.completion_tokens) >= s.effective_max:
                self._retire(s, reason=verdict.end_reason or "completed")

    def _relieve_pressure(self, active: List[StreamState]) -> None:
        """KV-pressure ladder: shrink prefill first; if there is nothing
        left to shrink, evict the largest stateless stream."""
        if self._live_prefill_budget > _MIN_PREFILL_BUDGET and any(
            s.phase is StreamPhase.PREFILL for s in active
        ):
            self._live_prefill_budget = max(
                _MIN_PREFILL_BUDGET, self._live_prefill_budget // 2
            )
            logger.warning(
                "⚠️ KV pressure: prefill budget halved to %d",
                self._live_prefill_budget,
            )
            return
        # FORCE-WINDOW, don't discard. This used to retire the victim with a
        # RetriableEngineError, which routes through `out.finish(error)` and
        # drops every token it had produced. On 2026-07-27 that threw away a
        # 48,318-token generation carrying 15 COMPLETE files, and the agent got
        # an exception instead of the work. Ending the stream early delivers
        # what exists; `truncated` on the response is what tells the caller it
        # was cut (see _retire → slot._last_end_reason).
        #
        # Pinned sessions are windowed too, and last. A pinned seat holds its KV
        # between turns by design, so leaving it untouched is what produced the
        # old dead end below — "decode cannot proceed" with no action taken.
        candidates = [s for s in active if s.phase is not StreamPhase.DONE]
        victims = [s for s in candidates if not s.slot.pinned] or candidates
        if victims:
            victim = max(victims, key=lambda s: s.n_past)
            self._h_evictions += 1
            logger.warning(
                "⚠️ KV pressure: force-windowing stream %s at %d tokens "
                "(%d resident)%s",
                victim.stream_id,
                len(victim.completion_tokens),
                victim.n_past,
                " [pinned session]" if victim.slot.pinned else "",
            )
            # Same partial-capture courtesy the abandonment path already pays,
            # so the text is inspectable afterwards rather than only inferable.
            from inference import runaway_capture

            if len(victim.completion_tokens) >= runaway_capture.CHECK_INTERVAL:
                self.h_runaway_captures += 1
                victim.pipeline.dump_capture("force-windowed under KV pressure")
            self._retire(victim, reason=_END_KV_PRESSURE)
        else:
            # Nothing decoding at all — pressure with an empty active set means
            # the pool is held entirely by resident KV outside this step.
            logger.error(
                "⚠️ KV pressure with no active stream to window — the pool is "
                "held by resident KV; a session must end or release its seat"
            )

    # -- retirement -----------------------------------------------------------

    def _retire(
        self,
        s: StreamState,
        reason: Optional[str] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        s.phase = StreamPhase.DONE
        s.end_reason = reason or (str(error) if error else "completed")
        self._streams.pop(s.stream_id, None)

        # Telemetry contract (read by core/inference + session_manager).
        slot = s.slot
        slot._last_completion_tokens = list(s.completion_tokens)
        slot._last_gen_start_pos = s.gen_start_pos
        slot._last_kv_base = int(s.req.kv_base)
        slot._last_dynamic_len = len(s.req.prompt_tokens)
        slot._last_flow_hit = False
        slot._last_flow_key = ""
        slot._last_end_reason = s.end_reason or ""
        static_head = self._persona_heads.get(slot.persona)
        slot._last_cache_hit = bool(
            static_head and s.req.kv_base > static_head.n_tokens
        )
        # Per-stream wall spans (concurrency-accurate, unlike the global
        # tracker): prefill = submit -> last prompt token decoded; decode =
        # prefill end -> retire. core/inference prefers these when present.
        now = time.monotonic()
        prefill_end = s.t_prefill_done or now
        slot._last_prefill_s = max(0.0, prefill_end - s.t_submit)
        slot._last_decode_s = max(0.0, now - prefill_end)

        # Truthful per-stream completion record: the wrapper's tracker
        # finish() is quiet in batched mode (its shared status blends
        # concurrent streams), so the engine — which has the exact spans
        # and counts — owns the "Generation complete" log line, the
        # post-mortem snapshot, and the health throughput-trend fold.
        try:
            from core.generation_tracker import get_tracker

            get_tracker().report_completion(
                request_id=str(getattr(s.req, "request_id", "") or ""),
                prompt_tokens=int(s.req.kv_base) + len(s.req.prompt_tokens),
                generated_tokens=len(s.completion_tokens),
                eval_s=slot._last_prefill_s,
                gen_s=slot._last_decode_s,
                total_s=max(0.0, now - s.t_submit),
            )
        except Exception:  # noqa: BLE001 — telemetry must never break decode
            pass

        if error is None:
            tail = s.pipeline.flush()
            if tail:
                s.req.out.emit(tail)
            s.req.out.finish()
        else:
            s.req.out.finish(error)

        if slot.pinned:
            # Session seat: KV stays live; position advances to what was
            # actually decoded (the last sampled token was never fed —
            # pool parity with Llama.generate's abandonment semantics).
            slot.n_tokens = s.n_past
        else:
            # Stateless borrow: seat clears at release (release_instance
            # → clear_seat control op); nothing to do here — the KV is
            # dropped when the seat is re-prepared or released.
            slot.n_tokens = s.n_past
        logger.info(
            "🧵 stream %s retired (%s): %d tokens",
            s.stream_id,
            s.end_reason,
            len(s.completion_tokens),
        )

    def _retire_abandoned(self, s: StreamState) -> None:
        """Consumer abandoned the stream (watchdog cancel / disconnect):
        capture the partial text the cancellation would otherwise discard
        (pool parity: the finally-block capture)."""
        from inference import runaway_capture

        if len(s.completion_tokens) >= runaway_capture.CHECK_INTERVAL:
            self.h_runaway_captures += 1
            s.pipeline.dump_capture(
                "abandoned by consumer (watchdog cancel or disconnect)"
            )
        self._retire(s, reason="abandoned")

    def _on_fatal(self, exc: BaseException) -> None:
        """A fatal decode on the shared context (Metal error latch): every
        stream dies by design (single-context blast radius, accepted). All
        seats are flagged — pinned sessions see _needs_context_refresh and
        run their proven deferred-teardown — then the context is rebuilt
        IN PLACE on this thread (fresh Metal backend, heads re-pinned) and
        the engine resumes accepting work."""
        self.h_decode_failures += 1
        logger.error(
            "💥 fatal decode failure on the batched context: %s — all %d "
            "stream(s) failed (Metal error latch; rebuilding the shared "
            "context; check preceding ggml lines for the root cause)",
            exc,
            len(self._streams),
        )
        for seat in self._seats:
            seat._needs_context_refresh = True
            seat.dead = True
        self._fail_all(RetriableEngineError(f"shared context failed: {exc}"))
        if self._rebuild_fn is None:
            self._fatal = exc
            return
        try:
            self._rebuild_fn()
            self._batch = None  # re-allocate against the fresh context
            for seat in self._seats:
                if not seat.pinned:
                    # Idle/stateless seats are clean immediately (their next
                    # prepare_seat re-forks from the re-pinned heads). Pinned
                    # session seats stay flagged: their KV is gone — the
                    # session layer tears them down and frees the seat.
                    seat.dead = False
                    seat._needs_context_refresh = False
            self.h_latch_heals += 1
            self._fatal = None
            logger.info(
                "🩹 batched latch heal #%d complete — context rebuilt, heads "
                "re-pinned, engine accepting work",
                self.h_latch_heals,
            )
        except Exception as rexc:  # noqa: BLE001 — park unavailable
            self._fatal = rexc
            logger.exception(
                "💥 batched context rebuild FAILED — engine parked "
                "(restart required)"
            )

    def _fail_all(self, error: BaseException) -> None:
        for s in list(self._streams.values()):
            if s.phase is not StreamPhase.DONE:
                s.phase = StreamPhase.DONE
                s.req.out.finish(error)
        self._streams.clear()

    # -- seat operations (call via control()) ----------------------------------

    def prepare_seat(self, slot: SeqSlot, persona: str) -> None:
        """Clear a seat's seq and fork the persona head onto it — the
        per-seq generalization of _resident_restore_static."""
        head = self._persona_heads.get(persona)
        if head is None:
            raise KeyError(
                f"unknown persona '{persona}' "
                f"(warmed: {sorted(self._persona_heads)})"
            )
        ctx = self._llama._ctx
        ctx.memory_seq_rm(slot.seq, 0, -1)
        if head.n_tokens > 0:
            ctx.memory_seq_cp(head.seq, slot.seq, -1, -1)
        slot.persona = persona
        slot.static_len = head.n_tokens
        slot.n_tokens = head.n_tokens
        slot.input_ids = list(head.tokens)
        slot._needs_context_refresh = False
        slot._last_completion_tokens = None

    def clear_seat(self, slot: SeqSlot) -> None:
        self._llama._ctx.memory_seq_rm(slot.seq, 0, -1)
        slot.n_tokens = 0
        slot.static_len = 0
        slot.input_ids = []
        slot.pinned = False

    def window_seat_sync(self, slot: SeqSlot, n_keep: int) -> int:
        """Slide a pinned session seat's window: drop the oldest ~half of
        the conversation beyond the static head and shift the tail down —
        the per-seq port of _window_resident_seq. Blocking (control op);
        callers run it in a threadpool. Returns the new n_tokens."""

        def _do() -> int:
            ctx = self._llama._ctx
            n_tokens = int(slot.n_tokens)
            n_discard = (n_tokens - n_keep) // 2
            if n_discard <= 0:
                return n_tokens
            ctx.memory_seq_rm(slot.seq, n_keep, n_keep + n_discard)
            ctx.memory_seq_add(slot.seq, n_keep + n_discard, n_tokens, -n_discard)
            slot.input_ids[n_keep : n_tokens - n_discard] = slot.input_ids[
                n_keep + n_discard : n_tokens
            ]
            del slot.input_ids[n_tokens - n_discard :]
            slot.n_tokens = n_tokens - n_discard
            self._h_forced_windows += 1
            logger.warning(
                "🪟 windowed seat seq %d: dropped %d oldest tokens "
                "(kept %d static head + %d recent)",
                slot.seq,
                n_discard,
                n_keep,
                slot.n_tokens - n_keep,
            )
            return slot.n_tokens

        return self.control(_do).result(timeout=60)

    def install_head_sync(self, slot: SeqSlot, head: PersonaHead) -> None:
        """Whole-seq replace of a seat's content with a pinned head (persona
        or reasoning level) — sound only at turn 0, before anything sits
        above the head. Blocking (control op)."""

        def _do() -> None:
            ctx = self._llama._ctx
            ctx.memory_seq_rm(slot.seq, 0, -1)
            if head.n_tokens > 0:
                ctx.memory_seq_cp(head.seq, slot.seq, -1, -1)
            slot.n_tokens = head.n_tokens
            slot.static_len = head.n_tokens
            slot.input_ids = list(head.tokens)
            # Whole-seq install = the head IS whatever we just put there; the
            # backend re-stamps the level right after a reasoning install.
            slot._reasoning_current = None

        self.control(_do).result(timeout=30)

    def splice_head_sync(self, slot: SeqSlot, head: PersonaHead) -> bool:
        """Mid-session head SPLICE: replace ONLY the head span [0, head_len)
        of a seat's seq with a pinned head, leaving the conversation above it
        intact — the per-seat port of the pool path's _splice_reasoning_head
        (2026-06-validated mechanism: 237 swaps, 0 corruption). Sound iff the
        target head's length equals the seat's current static head length, so
        body positions stay aligned. Blocking (control op)."""

        def _do() -> bool:
            ctx = self._llama._ctx
            hlen = head.n_tokens
            if hlen <= 0 or hlen != int(slot.static_len) or hlen > int(slot.n_tokens):
                logger.warning(
                    "🧠 seat head-splice refused: head %d vs static %d (n=%d, seq %d)",
                    hlen,
                    slot.static_len,
                    slot.n_tokens,
                    slot.seq,
                )
                return False
            ctx.memory_seq_rm(slot.seq, 0, hlen)
            ctx.memory_seq_cp(head.seq, slot.seq, -1, -1)
            slot.input_ids[:hlen] = list(head.tokens)
            return True

        return self.control(_do).result(timeout=30)

    # -- helpers ---------------------------------------------------------------

    def _tracker_call(self, method: str) -> None:
        """Feed the process-global GenerationTracker's progress meter
        (health's tokens_generated / seconds_since_last_token). With N
        concurrent streams the meter aggregates across streams — the right
        semantics for 'are tokens flowing'; per-request timing comes from
        the per-stream _last_* stash instead."""
        try:
            from core.generation_tracker import get_tracker

            getattr(get_tracker(), method)()
        except Exception:  # noqa: BLE001 — telemetry must never break decode
            pass

    def _default_sampler_factory(self, req: StreamRequest) -> Any:
        from llama_cpp._internals import LlamaSamplingContext

        params = build_sampling_params(
            req.sampling_kwargs,
            seed=req.seed,
            fallback_seed=getattr(self._llama, "_seed", 0xFFFFFFFF),
            grammar=req.grammar,
        )
        return LlamaSamplingContext(params, self._llama._model)

    def _default_is_eog(self, token: int) -> bool:
        import llama_cpp

        return bool(llama_cpp.llama_token_is_eog(self._llama._model.vocab, token))

    def _ensure_batch(self) -> Any:
        if self._batch is None:
            from llama_cpp import internals

            self._batch = internals.LlamaBatch(
                n_tokens=self._n_batch, embd=0, n_seq_max=self._seq_map.n_seq_max
            )
        # Prefill budget decays back toward the configured chunk after
        # pressure events (one doubling per step, upstream-style).
        if self._live_prefill_budget < self._prefill_chunk:
            self._live_prefill_budget = min(
                self._prefill_chunk, self._live_prefill_budget * 2
            )
        return self._batch

    def _family(self) -> str:
        return getattr(self, "family", "") or getattr(
            getattr(self._llama, "config", None), "family", ""
        )

    # The backend injects these two policy hooks at construction time (they
    # read config.generation); defaults keep the engine testable standalone.
    _repetition_guard_factory: Optional[Callable[[], Any]] = None
    _long_cycle_enabled: bool = True
    family: str = ""

    def _make_repetition_guard(self) -> Any:
        if self._repetition_guard_factory is not None:
            return self._repetition_guard_factory()
        try:
            from inference.repetition import RepetitionGuard

            return RepetitionGuard()
        except Exception:  # noqa: BLE001 — engine must work in bare tests
            return None

    def _long_cycle_on(self) -> bool:
        return self._long_cycle_enabled

    # -- health -----------------------------------------------------------

    def health(self) -> dict:
        active = sum(
            1 for s in self._streams.values() if s.phase is not StreamPhase.DONE
        )
        return {
            "decode_mode": "batched",
            "active_streams": active,
            "engine_steps": self._h_steps,
            "prefill_budget": self._live_prefill_budget,
            "kv_pressure_events": self._h_kv_pressure_events,
            "kv_forced_windows": self._h_forced_windows,
            "kv_evictions": self._h_evictions,
            "decode_failures": self.h_decode_failures,
            "latch_heals": self.h_latch_heals,
            "engine_fatal": str(self._fatal) if self._fatal else None,
        }


def _degenerate_error(reason: str, tokens_generated: int) -> BaseException:
    from inference.repetition import DegenerateGenerationError

    return DegenerateGenerationError(reason, tokens_generated=tokens_generated)
