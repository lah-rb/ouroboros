"""Bookkeeping for a memoryful tool loop — the escalation and router sessions.

A tool action that ran is a TURN; one that failed (a missing file, a
malformed argument) is a CORRECTION, queued back into the session so the
model recovers without spending its budget. Neither is counted against a
limit here: the loop's flow declares both numbers once and its
``_templates.tool_loop_gate`` step (flows/shared/templates.cue) routes every
lap to conclude when either is reached. The seed states the action budget to
the model from ``params.tool_budget``, the same value the gate reads.
"""

from __future__ import annotations

from agent.models import StepInput, StepOutput
from agent.session_injections import queue as queue_injection


def tool_budget(step_input: StepInput) -> int:
    """The loop's action budget as its flow declared it (0 when not passed)."""
    try:
        return int(step_input.params.get("tool_budget") or 0)
    except (TypeError, ValueError):
        return 0


def correction(
    step_input: StepInput, msg: str, *, corrections_key: str, label: str
) -> StepOutput:
    """Queue a correction and bump the corrections counter — NOT the turn
    budget (honest mistakes recover without pressure)."""
    corrections = int(step_input.context.get(corrections_key, 0) or 0) + 1
    updates: dict = {corrections_key: corrections}
    queue_injection(updates, step_input.context, f"Action failed — {msg}")
    return StepOutput(
        result={"action_ok": False},
        observations=f"{label} correction ({corrections}): {msg[:120]}",
        context_updates=updates,
    )


def observe(
    step_input: StepInput,
    message: str,
    *,
    turn_key: str,
    label: str,
    extra: dict | None = None,
) -> StepOutput:
    """Queue an observation and bump the turn count."""
    turn = int(step_input.context.get(turn_key, 0) or 0) + 1
    updates: dict = {turn_key: turn}
    if extra:
        updates.update(extra)
    queue_injection(updates, step_input.context, message)
    return StepOutput(
        result={"action_ok": True},
        observations=f"{label} turn {turn}",
        context_updates=updates,
    )
