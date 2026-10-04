"""Runtime trace event dataclasses and token counting.

Lightweight, always-on trace instrumentation.
All events share a common base with event_type, timestamps, and flow context.
Events are emitted via effects.emit_trace(); the history store (agent/history)
records each one as a parquet row under .agent/history/ and the finite-time
ledger below folds it live, so the ledger and the tables never disagree.

These are plain Python dataclasses (not Pydantic — this is instrumentation, not runtime).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def trace_enabled(effects) -> bool:
    """Whether an effects object can receive trace events.

    The single guard for the emit_trace capability check that was
    previously re-derived inline at every emission site.
    """
    return effects is not None and hasattr(effects, "emit_trace")


def count_tokens(text: str) -> int:
    """Approximate token count via whitespace splitting.

    Not accurate to any specific tokenizer, but precise and consistent.
    Suitable for detecting context bloat/starvation — relative magnitudes
    matter, not absolutes.
    """
    return len(text.split())


# ── Step context propagation ─────────────────────────────────────────
#
# The runtime sets this context var around each action dispatch. Effects
# (notably ``session_inference``) read it to emit trace events with the
# correct flow/step attribution without every call site having to thread
# a parameter through. This is the Implementation §4.7 contextvars shape:
# set-on-dispatch, read-by-effects, reset-on-exit. Memoryful session
# turns fired from inside an action automatically inherit the context.

_step_context: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ouroboros_step_context", default=None
)


@contextlib.contextmanager
def step_context(
    mission_id: str,
    cycle: int,
    flow: str,
    step: str,
    *,
    attempt: int = 1,
    goal_id: str = "",
    flow_directive: str = "",
):
    """Bind step metadata for the duration of an action's execution.

    Args:
        mission_id: Current mission identifier (may be empty string).
        cycle: Current cycle number (may be 0).
        flow: CUE flow name (e.g. "diagnose_issue").
        step: Step name within the flow (e.g. "start_session").
        attempt: The step's visit count within this flow run (FlowMeta.attempt).
        goal_id / flow_directive: The dispatched goal, when the flow has one —
            so a turn row can be found by the goal it served.

    Yields:
        None. Effects called inside the block can read the bound
        context via :func:`get_step_context`.

    Uses contextvars.Token so concurrent flow executions — if they ever
    happen — don't clobber each other's state.

    INVARIANT (telemetry soundness): every ``emit_trace`` call site must run
    on the asyncio side, NOT inside a ``run_in_threadpool`` worker. contextvars
    propagate into ``asyncio`` tasks but NOT into threadpool threads (unless
    ``copy_context()`` is used). Backend generation runs in a threadpool, but
    the effects read this context and emit the trace back on the awaiting
    coroutine — so attribution is correct today. If a future change moves an
    ``emit_trace`` into a worker thread, the bound context silently becomes
    None and the event loses its flow/step (and its ledger contribution lands
    in the wrong bucket). Keep emits on the asyncio side.
    """
    token = _step_context.set(
        {
            "mission_id": mission_id,
            "cycle": cycle,
            "flow": flow,
            "step": step,
            "attempt": int(attempt or 1),
            "goal_id": goal_id or "",
            "flow_directive": flow_directive or "",
        }
    )
    try:
        yield
    finally:
        _step_context.reset(token)


def get_step_context() -> dict | None:
    """Return the currently bound step context, or None if outside a step.

    Effects use this to attribute opportunistic trace events (e.g.
    session_inference turns fired from inside an action) to the step
    that's currently executing.
    """
    return _step_context.get()


# ── Branch and turn annotations ──────────────────────────────────────
#
# Two more contextvars in the same shape. ``branch_context`` is set by
# ChildEffects around every delegated inference so a row the PARENT effect
# emits still names the lane/branch it ran for (ChildEffects.emit_trace only
# stamps events the runtime emits — an effect-emitted row bypassed it).
# ``annotate_turn`` carries what the CALLER knows about an inference the
# effect is about to record and cannot see from the result alone: the full
# rendered prompt when only its dynamic tail was sent, the render/injection/
# pre_compute time spent before the clock started, the retry index, and the
# purpose. The effect reads both when it builds the InferenceCall, so every
# path — runtime steps, llm_menu, actions calling run_inference directly —
# produces exactly one row through the same builder.

_current_branch: contextvars.ContextVar[str] = contextvars.ContextVar(
    "ouroboros_branch", default=""
)
_turn_annotations: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "ouroboros_turn_annotations", default=None
)


@contextlib.contextmanager
def branch_context(branch: str):
    """Name the parallel branch/lane every inference inside runs for."""
    token = _current_branch.set(branch or "")
    try:
        yield
    finally:
        _current_branch.reset(token)


def get_current_branch() -> str:
    return _current_branch.get()


@contextlib.contextmanager
def annotate_turn(**fields):
    """Attach caller-side knowledge to the inference call(s) made inside.

    Recognised keys: ``purpose``, ``prompt_full``, ``prompt_static``,
    ``prompt_dynamic``, ``prompt_render_ms``, ``injection_ms``,
    ``pre_compute_ms``, ``call_attempt``. Unknown keys are ignored.
    """
    token = _turn_annotations.set(dict(fields))
    try:
        yield
    finally:
        _turn_annotations.reset(token)


def get_turn_annotations() -> dict:
    return dict(_turn_annotations.get() or {})


# ── Base Event ────────────────────────────────────────────────────────


@dataclass
class TraceEvent:
    """Base trace event. All events include these fields."""

    event_type: str = ""
    timestamp: float = field(default_factory=time.monotonic)
    wall_time: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    mission_id: str = ""
    # Parallel-branch attribution: "" outside a parallel step; the branch
    # flow's name inside one (stamped by effects/child.ChildEffects).
    branch: str = ""
    cycle: int = 0
    flow: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Cycle Events (emitted by loop.py) ────────────────────────────────


@dataclass
class CycleStart(TraceEvent):
    """Emitted by loop.py when a new cycle begins."""

    event_type: str = "cycle_start"
    entry_inputs: list[str] = field(default_factory=list)  # Key names only


@dataclass
class CycleEnd(TraceEvent):
    """Emitted by loop.py when a cycle completes."""

    event_type: str = "cycle_end"
    outcome: str = ""  # "tail_call" | "termination"
    target_flow: str | None = None  # If tail_call
    status: str | None = None  # If termination
    cycle_duration_ms: float = 0.0
    # Finite-time breakdown: cycle-level work outside any step. Both carried on
    # CycleEnd (CycleStart fires before projections run). projection =
    # _materialize_projections (before the flow); tail_resolution =
    # _resolve_tail_call ($ref + input_map, after the flow returns).
    projection_ms: float = 0.0
    tail_resolution_ms: float = 0.0


# ── Step Events (emitted by runtime.py) ──────────────────────────────


@dataclass
class StepStart(TraceEvent):
    """Emitted by runtime.py before action execution."""

    event_type: str = "step_start"
    step: str = ""
    action_type: str = ""  # "action" | "inference" | "flow" | "noop"
    action: str = ""  # Action name
    context_consumed: list[str] = field(default_factory=list)
    context_required: list[str] = field(default_factory=list)
    # Finite-time breakdown: _build_step_input (context filter + $ref resolve),
    # measured before this StepStart is emitted. (pre_compute runs inside the
    # action and is carried on InferenceCall.)
    input_build_ms: float = 0.0


@dataclass
class StepEnd(TraceEvent):
    """Emitted by runtime.py after resolver returns."""

    event_type: str = "step_end"
    step: str = ""
    published: list[str] = field(default_factory=list)
    resolver_type: str = ""
    resolver_decision: str = ""  # Transition chosen
    options_available: list[str] = field(default_factory=list)
    step_duration_ms: float = 0.0
    # Transition-resolver wall time (rule eval or llm_menu) — the span after
    # the action returns and before this StepEnd. (An llm_menu resolve also
    # emits its own InferenceCall; this is the resolver's own overhead.)
    resolver_ms: float = 0.0
    # Bounded preview of the step's observations — the human-readable line an
    # action writes about what it just decided. It was captured NOWHERE:
    # step_end had no field for it, InferenceCall.response_content is empty
    # unless --trace-prompts is set, and the server log is truncated on each
    # boot. So the quality gate's PASSING conclusion on the 2026-08-21
    # qwen3.8 run — the last verification before that artifact was frozen for
    # judging — could not be recovered afterwards at all. Capped hard: this
    # rides every step of every run, so it is a review aid, not a transcript.
    observations_preview: str = ""


# ── Inference Events ──────────────────────────────────────────────────


@dataclass
class InferenceCall(TraceEvent):
    """Emitted by runtime.py when an inference call completes.

    ``truncated`` mirrors InferenceResult.truncated — set when the
    generation hit its max_tokens budget rather than ending on EOS
    or a stop sequence. Surfaced here so trace viewers and post-run
    analyses can flag budget-truncation cases without having to
    re-derive from raw_length heuristics. See
    llmvp/core/session_manager.py::session_turn_complete for the
    detection point.
    """

    event_type: str = "inference_call"
    step: str = ""
    tokens_in: int = 0  # Real input tokens when available, else whitespace-split
    tokens_out: int = 0  # Real generated tokens when available, else whitespace
    wall_ms: float = 0.0  # Wall clock for the inference round-trip only
    # The temperature the model ACTUALLY sampled at — LLMVP's report (it owns
    # the model's parameters), else what the client sent. None = unknown.
    temperature: float | None = None
    # What the step asked for, verbatim ("t*0.1", "0.3"); "" = nothing asked.
    temperature_requested: str = ""
    max_tokens: int = 0
    purpose: str = ""  # "step_inference" | "llm_menu_resolve"
    thinking_content: str = ""  # Chain-of-thought from thinking models
    prompt_content: str = ""  # Full rendered prompt (when --trace-prompts)
    response_content: str = ""  # Raw model response (when --trace-prompts)
    truncated: bool = False  # Generation cut off by max_tokens budget
    # Finite-time breakdown: pre-inference work excluded from wall_ms (the
    # round-trip clock starts after these). prompt_render = template render +
    # cache-split; injection = consume_injections (turn path); pre_compute =
    # run_pre_compute formatters for this inference step.
    prompt_render_ms: float = 0.0
    injection_ms: float = 0.0
    pre_compute_ms: float = 0.0
    # Cache-aware token accounting (real backend counts; 0 when the server
    # doesn't report them — then tokens_in/out fall back to whitespace).
    # cached_prefix = tokens the model SKIPPED prefilling (KV reuse: static
    # prefix for stateless, full restored occupancy for sessions);
    # fresh_prefill = tokens actually prefilled this call; generated = real
    # completion token count. cache_hit = flow_kv_cache / resident-seq HIT.
    cached_prefix_tokens: int = 0
    fresh_prefill_tokens: int = 0
    generated_tokens: int = 0
    # Of generated_tokens, how many were chain-of-thought (server-tokenized
    # from the FSM-extracted thinking span). content = generated - reasoning.
    # 0 = UNKNOWN, not "did not reason" — a non-thinking model and a failed
    # count are indistinguishable here, which is why the summary reports
    # coverage alongside the ratio.
    reasoning_tokens: int = 0
    cache_hit: bool = False
    flow_key: str = ""
    # Server-measured phase split of wall_ms: prefill (prompt eval) vs decode
    # (generation). The remainder (wall_ms − prefill − decode) is queue/network.
    prefill_ms: float = 0.0
    decode_ms: float = 0.0
    # Resolved reasoning level for this call ("" = server default). Set from
    # config_overrides["reasoning"] (cue-authored or adaptive router) so token
    # breakdowns can attribute decode cost to reasoning effort per step.
    reasoning: str = ""
    # ── Identity and outcome (history store columns) ──────────────────
    # Where in the run this call sat: the step's visit count, the retry
    # index within the step, and the goal it served.
    step_attempt: int = 1
    call_attempt: int = 1
    goal_id: str = ""
    flow_directive: str = ""
    # What the server said about it: the correlation id (client-minted
    # "ouro-<hex>" for stateless calls, the session id for session turns),
    # the session and its turn id, whether the turn entered the session's
    # context, how the generation ended, and the degeneration verdict.
    request_id: str = ""
    session_id: str = ""
    session_turn_id: int | None = None
    turn_committed: bool | None = None
    end_reason: str = ""
    error: str = ""
    finished: bool = True
    degenerate: bool | None = None
    degenerate_reason: str = ""
    degenerate_tokens: int | None = None
    prompt_tokens: int = 0
    # Where it went: the model override (if any), the endpoint and the
    # routing domain; the flow-KV split when one was used (prompt_content is
    # then static + dynamic — the prompt as the model saw it).
    model: str = ""
    endpoint: str = ""
    domain: str = ""
    static_prefix_hash: str = ""
    prompt_static: str = ""
    prompt_dynamic: str = ""


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _turn_temperature(result, cfg: dict) -> float | None:
    """The temperature a turn actually sampled at: LLMVP's report when it
    gives one, else the number this client sent, else a plain number in the
    request. A relative spec ("t*0.1") is never coerced: float() on it
    recorded 0.0 for every such turn while the server floored them at 0.7
    (tier_20260924-191710)."""
    for v in (
        getattr(result, "temperature", None),
        getattr(result, "temperature_sent", None),
        cfg.get("temperature"),
    ):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def build_inference_call(
    result,
    *,
    prompt: str,
    response_text: str,
    thinking: str,
    wall_ms: float,
    config_overrides: dict | None,
    purpose: str,
    session_id: str = "",
    endpoint: str = "",
    domain: str = "",
    static_prefix: str | None = None,
    flow_key: str | None = None,
) -> InferenceCall:
    """The one builder every inference path records through.

    Reads the bound step context (attribution), the current branch, and the
    caller's turn annotations; everything else comes off the InferenceResult
    (duck-typed — the mock's minimal result works too). Real backend token
    counts win; the whitespace count is the fallback, exactly as before.
    """
    ctx = get_step_context() or {}
    ann = get_turn_annotations()
    cfg = config_overrides or {}
    static = str(ann.get("prompt_static") or static_prefix or "")
    dynamic = str(ann.get("prompt_dynamic") or (prompt if static else ""))
    full = str(ann.get("prompt_full") or ((static + prompt) if static else prompt))
    cp = _safe_int(getattr(result, "cached_prefix_tokens", 0))
    fp = _safe_int(getattr(result, "fresh_prefill_tokens", 0))
    gen = _safe_int(getattr(result, "generated_tokens", 0))
    ws_in = count_tokens(full)
    ws_out = count_tokens(response_text) if response_text else 0
    return InferenceCall(
        mission_id=str(ctx.get("mission_id", "") or ""),
        cycle=int(ctx.get("cycle", 0) or 0),
        flow=str(ctx.get("flow", "") or ""),
        step=str(ctx.get("step", "") or ""),
        branch=get_current_branch(),
        step_attempt=int(ctx.get("attempt", 1) or 1),
        call_attempt=int(ann.get("call_attempt", 1) or 1),
        goal_id=str(ctx.get("goal_id", "") or ""),
        flow_directive=str(ctx.get("flow_directive", "") or ""),
        tokens_in=(cp + fp) if (cp or fp) else ws_in,
        tokens_out=gen if gen else ws_out,
        wall_ms=float(wall_ms or 0.0),
        temperature=_turn_temperature(result, cfg),
        temperature_requested=(
            "" if cfg.get("temperature") is None else str(cfg.get("temperature"))
        ),
        max_tokens=_safe_int(cfg.get("max_tokens", 0)),
        purpose=str(ann.get("purpose") or purpose),
        thinking_content=thinking or "",
        prompt_content=full,
        response_content=response_text or "",
        truncated=bool(getattr(result, "truncated", False)),
        prompt_render_ms=_safe_float(ann.get("prompt_render_ms", 0.0)),
        injection_ms=_safe_float(ann.get("injection_ms", 0.0)),
        pre_compute_ms=_safe_float(ann.get("pre_compute_ms", 0.0)),
        cached_prefix_tokens=cp,
        fresh_prefill_tokens=fp,
        generated_tokens=gen,
        reasoning_tokens=_safe_int(getattr(result, "reasoning_tokens", 0)),
        cache_hit=bool(getattr(result, "cache_hit", False)),
        flow_key=str(flow_key or getattr(result, "flow_key", "") or ""),
        prefill_ms=_safe_float(getattr(result, "prefill_ms", 0.0)),
        decode_ms=_safe_float(getattr(result, "decode_ms", 0.0)),
        reasoning=str(cfg.get("reasoning", "") or ""),
        request_id=str(getattr(result, "request_id", "") or ""),
        session_id=session_id or "",
        session_turn_id=getattr(result, "session_turn_id", None),
        turn_committed=getattr(result, "turn_committed", None),
        end_reason=str(getattr(result, "end_reason", "") or ""),
        error=str(getattr(result, "error", "") or ""),
        finished=bool(getattr(result, "finished", True)),
        degenerate=getattr(result, "degenerate", None),
        degenerate_reason=str(getattr(result, "degenerate_reason", "") or ""),
        degenerate_tokens=getattr(result, "degenerate_tokens", None),
        prompt_tokens=_safe_int(getattr(result, "prompt_tokens", 0)),
        model=str(cfg.get("model", "") or ""),
        endpoint=endpoint or "",
        domain=domain or "",
        static_prefix_hash=(
            hashlib.md5(static.encode("utf-8")).hexdigest()[:10] if static else ""
        ),
        prompt_static=static,
        prompt_dynamic=dynamic,
    )


# ── Sub-flow Events ──────────────────────────────────────────────────


@dataclass
class FlowInvoke(TraceEvent):
    """Emitted by runtime.py when a sub-flow is invoked."""

    event_type: str = "flow_invoke"
    step: str = ""
    child_flow: str = ""
    child_inputs: list[str] = field(default_factory=list)


@dataclass
class FlowReturn(TraceEvent):
    """Emitted by runtime.py when a sub-flow returns."""

    event_type: str = "flow_return"
    child_flow: str = ""
    return_status: str = ""
    child_duration_ms: float = 0.0


# ── Session Lifecycle ────────────────────────────────────────────────
#
# Fired by the inference effect when a memoryful session starts or ends.
# Pair the trace renderer: diagnosis sessions tend to end at the bottom
# of their flow (all prior turns forgotten on the next dispatch), while
# edit sessions span a patch flow's whole lifetime. Surfacing these
# answers "does the model remember anything across diagnose cycles?" at
# a glance. Like InferenceCall, these are gated on a bound step context
# — calls outside a flow (tests, tooling) don't emit.


@dataclass
class SessionStart(TraceEvent):
    """Emitted when a memoryful inference session begins."""

    event_type: str = "session_start"
    step: str = ""
    session_id: str = ""
    config: dict = field(default_factory=dict)
    # Semi-permanent snapshot this session forked from ("" = fresh). A
    # forked session starts with the snapshot's whole context at ~zero
    # prefill — the ledger must not read its cheap first turn as a
    # short prompt.
    from_snapshot: str = ""


@dataclass
class SessionSnapshot(TraceEvent):
    """Emitted when a session pins a semi-permanent snapshot.

    The snapshot outlives the session (freed only by an explicit purge)
    — the (SessionSnapshot, later purge) pair is how the ledger
    attributes the ingest-once prefill saving.
    """

    event_type: str = "session_snapshot"
    step: str = ""
    session_id: str = ""
    key: str = ""
    tokens: int = 0
    resident: bool = True  # False = replay fallback (forks re-prefill)


@dataclass
class SessionEnd(TraceEvent):
    """Emitted when a memoryful inference session closes."""

    event_type: str = "session_end"
    step: str = ""
    session_id: str = ""
    success: bool = True
    wall_ms: float = 0.0  # close-RPC time only (end_inference_session call)
    # Full session SPAN: start_inference_session → end. wall_ms above is just
    # the teardown RPC; span_ms is the lifetime the session was live (across
    # all its turns), the honest "session_lifecycle" time-ledger contribution.
    span_ms: float = 0.0


# ── Subprocess / MCP ─────────────────────────────────────────────────
#
# ``interact`` flow's 12 cycles of interactive exploration were almost
# entirely a trace black box in a12 — we saw the inference count per
# flow but not which commands the model actually issued or what the
# terminal replied. These events close that gap. Output is truncated
# to a cap so a single chatty subprocess cannot flood the trace.


# Per-event truncation cap (characters) for stdout/stderr and tool
# result content. Kept conservative so a single chatty command can't
# flood the trace buffer; the full output still goes through the
# effects log and the action's own return value regardless.
_OUTPUT_PREVIEW_CHARS = 1200


def _truncate_preview(text: str, cap: int = _OUTPUT_PREVIEW_CHARS) -> str:
    """Truncate a string for inclusion in a trace event.

    Adds a ``[…truncated N chars]`` suffix when shortened so downstream
    readers know the preview isn't the full content. A single-source
    helper keeps the truncation format consistent across events.
    """
    if len(text) <= cap:
        return text
    trimmed = len(text) - cap
    return text[:cap] + f"\n[...truncated {trimmed} chars]"


@dataclass
class CommandRun(TraceEvent):
    """Emitted when a subprocess command completes via run_command."""

    event_type: str = "command_run"
    step: str = ""
    command: str = ""  # joined argv for display
    return_code: int = 0
    timed_out: bool = False
    stdout_preview: str = ""  # truncated per _OUTPUT_PREVIEW_CHARS
    stderr_preview: str = ""
    wall_ms: float = 0.0


@dataclass
class McpToolCall(TraceEvent):
    """Emitted when an MCP server tool call completes."""

    event_type: str = "mcp_tool_call"
    step: str = ""
    server: str = ""  # resolved from connection_id when possible
    tool: str = ""
    arg_keys: list[str] = field(default_factory=list)
    error: str = ""  # empty on success
    result_preview: str = ""
    wall_ms: float = 0.0


# ── Notes ────────────────────────────────────────────────────────────
#
# ``push_note`` is how the agent persists learnings into mission state.
# Without these events we can't tell whether notes-that-should-have-been-
# guiding-the-model were actually present, nor see the stream of
# observations the agent is accumulating. The content preview caps
# separately from command output because notes are typically short
# prose, not subprocess dumps.


_NOTE_PREVIEW_CHARS = 400


@dataclass
class HealthSample(TraceEvent):
    """A snapshot of the server's cache/feature register.

    A REAL dataclass, not a dict smuggled through a `payload=` kwarg —
    TraceEvent has no such field, so the first version raised TypeError on
    construction and the emit site's broad `except Exception` swallowed it
    silently on every call. The result looked exactly like "the server does
    not report these fields": zero events, no error, nothing to grep. The
    unit test missed it because it fed fold_event a hand-built dict and never
    exercised the emission path.
    """

    event_type: str = "health_sample"
    health: dict = field(default_factory=dict)


@dataclass
class PromptBackstop(TraceEvent):
    """The last-resort prompt guard fired (Guard G1): a prompt reached
    inference too large for what the serving window had free. Every
    firing means an upstream fit missed, so it is recorded as a row, not
    only a log line. ``bounded`` False: nothing could be done (the context
    was already full) and the prompt was sent as it was."""

    event_type: str = "prompt_backstop"
    step: str = ""
    session_id: str = ""
    window: int = 0
    used: int = 0
    reserve: int = 0
    static_tokens: int = 0
    prompt_tokens: int = 0
    prompt_chars: int = 0
    kept_chars: int = 0
    bounded: bool = True
    how: str = ""  # "exact" (server tokenizer) | "estimated"


@dataclass
class CapacitySample(TraceEvent):
    """Pool-level capacity at one lane-report tick.

    HealthSample covers the CACHE register; this covers the ADMISSION
    register, and the two answer different questions. The question this
    exists for is "why is a lane idle" — and the honest answer is usually
    not the one the seat count suggests.

    Measured 2026-08-19 on the live v2 run: seats_total=4, seats_free=2,
    free_cells=0, live_occupancy=67,045 against a 65,536 pool. Two streams
    had ENTITLED more than the whole pool, so two seats sat idle with
    nothing wrong with them. Without this event that state is invisible —
    the lane report went to the log as one line and was never persisted,
    so no post-run analysis could distinguish "no work pending" from
    "refused admission every time".

    `occupancy_ratio` is stored rather than derived because free_cells
    clamps at 0: once entitlement exceeds the pool the overshoot is
    exactly the number that matters and exactly the one the clamp
    destroys.
    """

    event_type: str = "capacity_sample"
    seats_total: int = 0
    seats_free: int = 0
    kv_pool_tokens: int = 0
    free_cells: int = 0
    live_occupancy: int = 0
    pinned_occupancy: int = 0
    pool_slack: int = 0
    active_streams: int = 0
    waiting: int = 0
    serving: bool = True
    source: str = ""  # "ws" | "poll" | "legacy" | "none"
    seq: int = 0
    # Entitled cells / pool. >1.0 means oversubscribed by entitlement,
    # which is the state that idles seats while nothing is wrong.
    occupancy_ratio: float = 0.0
    # lane name -> "{done}d/{idle}i/{failed}f[/{inflight} live]"
    lanes: dict = field(default_factory=dict)
    # The most recent admission refusal, verbatim from the capacity model.
    last_refusal: str = ""


@dataclass
class NotePushed(TraceEvent):
    """Emitted when a note is appended to mission state."""

    event_type: str = "note_pushed"
    step: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    source_flow: str = ""
    content_preview: str = ""
    success: bool = True


# ── Finite time + token ledger ────────────────────────────────────────
#
# The finite breakdown: a run's total wall-clock decomposed into
# NON-OVERLAPPING leaf categories that sum to the total minus an explicit
# `residual`. Categories are disjoint by construction — e.g. prompt_render
# is the time BEFORE an inference's wall_ms clock starts, so it never
# double-counts inference; session span is reported as info, not summed
# (it contains the session's inference calls, which already count under
# `inference`). step/cycle durations are deliberately NOT summed (they are
# containers overlapping the leaves) — they drive the per-flow rollup. The
# residual (total − Σ leaves) captures async glue and any not-yet-
# instrumented work; residual/total is the "accounting completeness"
# metric, targeted toward 0.
#
# `flush` and `persistence` are accumulated directly on a live ledger via
# ledger_add_ms (they are effects-internal, not trace events) — so batch
# recompute from a complete trace file folds them into the residual.

# Leaf categories that partition wall-clock. Order is display order.
TIME_CATEGORIES = (
    "inference",
    "terminal",
    "mcp",  # MCP tool calls incl. PTY send_input (settle/deferral time lands here)
    "prompt_render",
    "injection",
    "pre_compute",
    "input_build",
    "resolver",
    "projection",
    "tail_resolution",
    "session_rpc",
    "persistence",
    "flush",
)


@dataclass
class RunSummary(TraceEvent):
    """Terminal trace record: the finalized finite time + token breakdown.

    Written as the last JSONL line on final flush and as the canonical
    ``<trace>.summary.json`` companion. Old readers ignore the unknown
    event_type; the markdown head (trace_cli) renders from it directly.
    """

    event_type: str = "run_summary"
    total_wall_ms: float = 0.0
    summary: dict = field(default_factory=dict)


def new_ledger() -> dict:
    """A fresh accumulator for the finite-time + token breakdown.

    Plain dicts (no defaultdict) so it serializes cleanly and is safe on a
    long-lived effects instance. Fold trace events with ``fold_event``; add
    effects-internal time with ``ledger_add_ms``; finalize with
    ``finalize_ledger``."""
    return {
        "time_ms": {c: 0.0 for c in TIME_CATEGORIES},
        "tokens": {
            "cached_prefix": 0,
            "fresh_prefill": 0,
            "generated": 0,
            # CoT vs agent-turn split of `generated`. reasoning_calls counts
            # how many calls reported a non-zero split, so the ratio can be
            # read with its coverage instead of being taken on faith.
            "reasoning": 0,
            "reasoning_calls": 0,
            "ws_in": 0,
            "ws_out": 0,
            "real_calls": 0,
            "ws_calls": 0,
        },
        "cache": {"hit": 0, "miss": 0},  # counted only for real-token calls
        # Server-side cache/feature register, sampled from health. FIRST and
        # LAST only: the counters are monotonic, so last-minus-first is the
        # run delta, and the strategy triple is a constant we want recorded
        # once. Absent when the server does not report it — never zeroed,
        # because a zero would be indistinguishable from "never fired".
        "server": {"first": None, "last": None, "samples": 0},
        # Server-measured phase split of the inference bucket (a sub-attribution
        # of `inference` time, not a separate partition category).
        "inf_phase": {"prefill_ms": 0.0, "decode_ms": 0.0},
        "counts": {
            "cycles": 0,
            "steps": 0,
            "inferences": 0,
            "commands": 0,
            "mcp": 0,
            "sessions": 0,
            "notes": 0,
        },
        "session_span_ms": 0.0,
        "flows": {},  # flow -> rollup
    }


def _flow_bucket(ledger: dict, flow: str) -> dict:
    b = ledger["flows"].get(flow)
    if b is None:
        b = {
            "cycles": 0,
            "inferences": 0,
            "inference_ms": 0.0,
            "cached_prefix": 0,
            "fresh_prefill": 0,
            "generated": 0,
            # CoT vs agent-turn split of `generated`. reasoning_calls counts
            # how many calls reported a non-zero split, so the ratio can be
            # read with its coverage instead of being taken on faith.
            "reasoning": 0,
            "reasoning_calls": 0,
        }
        ledger["flows"][flow] = b
    return b


def fold_event(ledger: dict, e: dict) -> None:
    """Fold one trace-event dict's numeric fields into the ledger.

    Disjoint-by-construction: each timing field lands in exactly one leaf
    category. Used both live (emit_trace) and in batch recompute (trace_cli).
    """
    et = e.get("event_type", "")
    t = ledger["time_ms"]
    flow = e.get("flow", "")
    if et == "cycle_start":
        ledger["counts"]["cycles"] += 1
        _flow_bucket(ledger, flow)["cycles"] += 1
    elif et == "cycle_end":
        t["projection"] += e.get("projection_ms", 0.0) or 0.0
        t["tail_resolution"] += e.get("tail_resolution_ms", 0.0) or 0.0
    elif et == "step_start":
        ledger["counts"]["steps"] += 1
        t["input_build"] += e.get("input_build_ms", 0.0) or 0.0
    elif et == "step_end":
        t["resolver"] += e.get("resolver_ms", 0.0) or 0.0
    elif et == "inference_call":
        ledger["counts"]["inferences"] += 1
        t["inference"] += e.get("wall_ms", 0.0) or 0.0
        t["prompt_render"] += e.get("prompt_render_ms", 0.0) or 0.0
        t["injection"] += e.get("injection_ms", 0.0) or 0.0
        t["pre_compute"] += e.get("pre_compute_ms", 0.0) or 0.0
        ledger["inf_phase"]["prefill_ms"] += e.get("prefill_ms", 0.0) or 0.0
        ledger["inf_phase"]["decode_ms"] += e.get("decode_ms", 0.0) or 0.0
        fb = _flow_bucket(ledger, flow)
        fb["inferences"] += 1
        fb["inference_ms"] += e.get("wall_ms", 0.0) or 0.0
        tok = ledger["tokens"]
        gen = int(e.get("generated_tokens", 0) or 0)
        cp = int(e.get("cached_prefix_tokens", 0) or 0)
        fp = int(e.get("fresh_prefill_tokens", 0) or 0)
        if gen or cp or fp:  # real backend counts present
            tok["cached_prefix"] += cp
            tok["fresh_prefill"] += fp
            tok["generated"] += gen
            tok["real_calls"] += 1
            rz = int(e.get("reasoning_tokens", 0) or 0)
            if rz:
                tok["reasoning"] += rz
                tok["reasoning_calls"] += 1
                fb["reasoning"] += rz
                fb["reasoning_calls"] += 1
            fb["cached_prefix"] += cp
            fb["fresh_prefill"] += fp
            fb["generated"] += gen
            if e.get("cache_hit"):
                ledger["cache"]["hit"] += 1
            else:
                ledger["cache"]["miss"] += 1
        else:  # whitespace fallback (server didn't report real counts)
            tok["ws_in"] += int(e.get("tokens_in", 0) or 0)
            tok["ws_out"] += int(e.get("tokens_out", 0) or 0)
            tok["ws_calls"] += 1
    elif et == "command_run":
        ledger["counts"]["commands"] += 1
        t["terminal"] += e.get("wall_ms", 0.0) or 0.0
    elif et == "mcp_tool_call":
        ledger["counts"]["mcp"] += 1
        t["mcp"] += e.get("wall_ms", 0.0) or 0.0
    elif et == "session_start":
        ledger["counts"]["sessions"] += 1
    elif et == "session_end":
        t["session_rpc"] += e.get("wall_ms", 0.0) or 0.0
        ledger["session_span_ms"] += e.get("span_ms", 0.0) or 0.0
    elif et == "note_pushed":
        ledger["counts"]["notes"] += 1
    elif et == "health_sample":
        snap = e.get("health") or {}
        if snap:
            srv = ledger.setdefault(
                "server", {"first": None, "last": None, "samples": 0}
            )
            if srv.get("first") is None:
                srv["first"] = snap
            srv["last"] = snap
            srv["samples"] = srv.get("samples", 0) + 1


def ledger_add_ms(ledger: dict, category: str, ms: float) -> None:
    """Add directly-measured time not carried by a trace event (flush,
    persistence). No-op for unknown categories."""
    if category in ledger["time_ms"]:
        ledger["time_ms"][category] += ms


# Counters that accumulate over a run; everything else in the register is a
# constant we record once (the strategy triple) or a gauge we take as-of-end.
_SERVER_COUNTERS = (
    "flowBuilds",
    "flowHits",
    "flowEvicts",
    "flowFallbacks",
    "contextRefreshes",
    "runawayCaptures",
)


def _server_block(srv: dict) -> dict | None:
    """Start/end/delta for the server cache register, or None if unsampled.

    The DELTA is the point: `flowHits` is cumulative across the server's whole
    lifetime, so the absolute value says nothing about THIS run. A run that
    shows flow_hits delta 0 while the flag is on is the finding — the band is
    allocated and never used.

    `contextRefreshes > 0` is a comparability warning, not a metric: a refresh
    wipes the flow band and demotes hot snapshots, so a run that took one is
    not measuring the same machine as a run that did not.
    """
    first, last = srv.get("first"), srv.get("last")
    if not first or not last:
        return None
    # A SINGLE sample makes first and last the same snapshot, so every counter
    # differences to 0 — which would read as "the flow cache never fired" when
    # the truth is "we never measured twice". The strategy triple IS knowable
    # from one sample (it is a constant); the delta is not. Report the first,
    # withhold the second.
    samples = int(srv.get("samples") or 0)
    delta: dict | None = None
    if samples >= 2:
        delta = {}
        for k in _SERVER_COUNTERS:
            a, b = first.get(k), last.get(k)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                delta[k] = b - a
    return {
        # The substrate this run actually ran on (OPEN_TASKS §12) — read from
        # health, never from config: resident is a request the arch can refuse.
        "strategy": last.get("sessionStrategy") or "",
        "can_shift": last.get("sessionCanShift"),
        "resident_requested": last.get("residentRequested"),
        "resident_active": last.get("residentActive"),
        "decode_mode": last.get("decodeMode") or "",
        "n_ctx_seq": last.get("nCtxSeq"),
        "kv_pool_tokens": last.get("kvPoolTokens"),
        "samples": samples,
        "delta": delta,
        "cache_wiped_mid_run": (
            bool(delta.get("contextRefreshes", 0)) if delta is not None else None
        ),
        "end": {
            "flow_cache_entries": last.get("flowCacheEntries"),
            "decode_tps_recent": last.get("decodeTpsRecent"),
            "prefill_tps_recent": last.get("prefillTpsRecent"),
            "mem_system_wired_mb": last.get("memSystemWiredMb"),
        },
    }


def finalize_ledger(ledger: dict, total_wall_ms: float) -> dict:
    """Compute the residual + ratios; return the JSON-able summary dict.

    ``residual_ms = total_wall_ms − Σ(leaf categories)``. By construction the
    leaves are disjoint sub-spans of the run, so residual ≥ 0 up to clock
    jitter; ``completeness_pct`` is the fraction of wall-clock attributed.
    The in/out ratio is reported BOTH ways: ``fresh`` (compute actually done
    = fresh_prefill : generated) and ``context`` (full prompt incl. cache =
    (cached+fresh) : generated) — the gap between them is the cache payoff.
    """
    t = ledger["time_ms"]
    accounted = sum(t.values())
    residual = total_wall_ms - accounted
    tok = ledger["tokens"]
    real_in = tok["cached_prefix"] + tok["fresh_prefill"]
    cache_total = ledger["cache"]["hit"] + ledger["cache"]["miss"]
    prefix_total = tok["cached_prefix"] + tok["fresh_prefill"]

    def pct(x: float) -> float:
        return round(100.0 * x / total_wall_ms, 2) if total_wall_ms > 0 else 0.0

    def ratio(a: int, b: int) -> float | None:
        return round(a / b, 3) if b else None

    return {
        "total_wall_ms": round(total_wall_ms, 1),
        "accounted_ms": round(accounted, 1),
        "residual_ms": round(residual, 1),
        "completeness_pct": (
            round(100.0 * accounted / total_wall_ms, 2) if total_wall_ms > 0 else 0.0
        ),
        "time_ms": {k: round(v, 1) for k, v in t.items()},
        "time_pct": {k: pct(v) for k, v in t.items()},
        "residual_pct": pct(residual),
        # Sub-attribution of the `inference` bucket into prefill vs decode
        # (server-measured). server_other = the rest (queue/network/overhead).
        "inference_phase": {
            "prefill_ms": round(ledger["inf_phase"]["prefill_ms"], 1),
            "decode_ms": round(ledger["inf_phase"]["decode_ms"], 1),
            "server_other_ms": round(
                max(
                    0.0,
                    t["inference"]
                    - ledger["inf_phase"]["prefill_ms"]
                    - ledger["inf_phase"]["decode_ms"],
                ),
                1,
            ),
            "prefill_pct": (
                round(100 * ledger["inf_phase"]["prefill_ms"] / t["inference"], 1)
                if t["inference"] > 0
                else 0.0
            ),
            "decode_pct": (
                round(100 * ledger["inf_phase"]["decode_ms"] / t["inference"], 1)
                if t["inference"] > 0
                else 0.0
            ),
        },
        "counts": dict(ledger["counts"]),
        "session_span_ms": round(ledger["session_span_ms"], 1),
        "tokens": {
            "cached_prefix": tok["cached_prefix"],
            "fresh_prefill": tok["fresh_prefill"],
            "generated": tok["generated"],
            "real_input_total": real_in,
            "whitespace_in": tok["ws_in"],
            "whitespace_out": tok["ws_out"],
            "real_calls": tok["real_calls"],
            "whitespace_calls": tok["ws_calls"],
            # ── CoT vs AGENT-TURN SPLIT of `generated` ─────────────────
            # `generated` alone cannot tell a model that reasoned 12k tokens
            # and answered in 300 from one that wrote 12k of answer, which on
            # a heavy-thinking fleet is the distinction that decides whether
            # more wall clock would help. cot_pct is reported WITH its
            # coverage: reasoning_calls/real_calls says how much of the run
            # the ratio actually describes, because a non-thinking model and a
            # failed count both look like 0 here.
            "reasoning": tok["reasoning"],
            "content": max(0, tok["generated"] - tok["reasoning"]),
            "reasoning_calls": tok["reasoning_calls"],
            "cot_pct": (
                round(100.0 * tok["reasoning"] / tok["generated"], 1)
                if tok["generated"]
                else None
            ),
            "cot_coverage": (
                round(tok["reasoning_calls"] / tok["real_calls"], 3)
                if tok["real_calls"]
                else None
            ),
        },
        "server": _server_block(ledger.get("server") or {}),
        "cache": {
            "hit": ledger["cache"]["hit"],
            "miss": ledger["cache"]["miss"],
            "hit_rate": (
                round(ledger["cache"]["hit"] / cache_total, 4) if cache_total else None
            ),
            "prefix_reuse_rate": (
                round(tok["cached_prefix"] / prefix_total, 4) if prefix_total else None
            ),
        },
        "io_ratio": {
            "fresh": ratio(tok["fresh_prefill"], tok["generated"]),
            "context": ratio(real_in, tok["generated"]),
        },
        # Derived throughput from the REAL registers (fresh tokens / phase
        # time) — the figures otherwise hand-computed from server logs every
        # time a run is analyzed. prefill_tps uses fresh_prefill only:
        # cached-prefix tokens were skipped, so counting them would report
        # effective (cache-flattered) rather than true prefill speed.
        "rates": {
            "prefill_tps": (
                round(
                    tok["fresh_prefill"] / (ledger["inf_phase"]["prefill_ms"] / 1000), 1
                )
                if ledger["inf_phase"]["prefill_ms"] > 0
                else None
            ),
            "decode_tps": (
                round(tok["generated"] / (ledger["inf_phase"]["decode_ms"] / 1000), 1)
                if ledger["inf_phase"]["decode_ms"] > 0
                else None
            ),
        },
        "flows": ledger["flows"],
    }


def summarize_events(events: list[dict], total_wall_ms: float | None = None) -> dict:
    """Batch path: compute the finite summary from a complete event list.

    Used by trace_cli to render an old/complete trace when no companion
    summary.json exists. ``total_wall_ms`` falls back to Σ cycle_duration_ms
    (the legacy denominator) — flush/persistence aren't in events, so they
    fold into the residual here (the live summary.json is authoritative)."""
    ledger = new_ledger()
    for e in events:
        fold_event(ledger, e)
    if total_wall_ms is None:
        total_wall_ms = sum(
            e.get("cycle_duration_ms", 0.0) or 0.0
            for e in events
            if e.get("event_type") == "cycle_end"
        )
    return finalize_ledger(ledger, total_wall_ms)
