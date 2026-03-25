"""Runtime trace event dataclasses and token counting.

Phase 2 of the Blueprint Design — lightweight, always-on trace instrumentation.
All events share a common base with event_type, timestamps, and flow context.
Events are emitted via effects.emit_trace() and flushed to JSONL at cycle boundaries.

These are plain Python dataclasses (not Pydantic — this is instrumentation, not runtime).
"""

from __future__ import annotations

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
    """Emitted by runtime.py when an inference call completes."""

    event_type: str = "inference_call"
    step: str = ""
    tokens_in: int = 0  # Whitespace-split count of prompt
    tokens_out: int = 0  # Whitespace-split count of response
    wall_ms: float = 0.0  # Wall clock for this call
    temperature: float = 0.0
    max_tokens: int = 0
    purpose: str = ""  # "step_inference" | "llm_menu_resolve"


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
