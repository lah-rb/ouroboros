"""Runtime trace event dataclasses and token counting.

Phase 2 of the Blueprint Design — lightweight, always-on trace instrumentation.
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
    tokens_in: int = 0  # Whitespace-split count of prompt
    tokens_out: int = 0  # Whitespace-split count of response
    wall_ms: float = 0.0  # Wall clock for this call
    temperature: float = 0.0
    max_tokens: int = 0
    purpose: str = ""  # "step_inference" | "llm_menu_resolve"
    thinking_content: str = ""  # Chain-of-thought from thinking models
    prompt_content: str = ""  # Full rendered prompt (when --trace-prompts)
    response_content: str = ""  # Raw model response (when --trace-prompts)
    truncated: bool = False  # Generation cut off by max_tokens budget


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


@dataclass
class SessionEnd(TraceEvent):
    """Emitted when a memoryful inference session closes."""

    event_type: str = "session_end"
    step: str = ""
    session_id: str = ""
    success: bool = True
    wall_ms: float = 0.0


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
