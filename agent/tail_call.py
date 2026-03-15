"""Tail-call mechanics — FlowTailCall, FlowTermination, FlowOutcome.

A tail call is a special terminal state that, instead of returning to a caller,
triggers a new flow execution. The current flow's context is fully released —
no stack accumulation.

Terminal steps WITHOUT tail_call produce FlowTermination (true termination).
Terminal steps WITH tail_call produce FlowTailCall (dispatch to next flow).

The outer loop (loop.py) follows tail calls until a FlowTermination is reached.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.models import FlowResult


@dataclass
class FlowTailCall:
    """Signals that the flow wants to dispatch to another flow.

    The current flow's context is fully released. The outer loop
    loads the target flow, passes the inputs, and continues.
    """

    target_flow: str
    inputs: dict[str, Any] = field(default_factory=dict)
    delay_seconds: float | None = None
    # Metadata from the completing flow
    source_flow: str = ""
    source_status: str = ""


@dataclass
class FlowTermination:
    """Signals true termination — the outermost loop should stop."""

    result: FlowResult


# FlowOutcome is what execute_flow returns
FlowOutcome = FlowTailCall | FlowTermination
