"""Runtime trace event dataclasses and token counting.

Lightweight, always-on trace instrumentation.
All events share a common base with event_type, timestamps, and flow context.
Events are emitted via effects.emit_trace() and flushed to JSONL at cycle boundaries.

These are plain Python dataclasses (not Pydantic — this is instrumentation, not runtime).
"""

from __future__ import annotations

import contextlib
import contextvars
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
):
    """Bind step metadata for the duration of an action's execution.

    Args:
        mission_id: Current mission identifier (may be empty string).
        cycle: Current cycle number (may be 0).
        flow: CUE flow name (e.g. "diagnose_issue").
        step: Step name within the flow (e.g. "start_session").

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
    temperature: float = 0.0
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
            "ws_in": 0,
            "ws_out": 0,
            "real_calls": 0,
            "ws_calls": 0,
        },
        "cache": {"hit": 0, "miss": 0},  # counted only for real-token calls
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


def ledger_add_ms(ledger: dict, category: str, ms: float) -> None:
    """Add directly-measured time not carried by a trace event (flush,
    persistence). No-op for unknown categories."""
    if category in ledger["time_ms"]:
        ledger["time_ms"][category] += ms


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
        },
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
