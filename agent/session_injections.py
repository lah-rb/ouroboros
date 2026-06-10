"""Deferred session injection — inject context into a memoryful
session without wasting an inference call.

Memoryful sessions in Ouroboros are plumbed through the effects
protocol as ``effects.session_inference(session_id, prompt, ...)``.
Every call sends a user turn and generates an assistant turn.
There's no protocol for "add a user turn without generating" — if
a flow step needs to update the session's conversation history
(say, correct a previous parse error, acknowledge a file read,
or seed starting context), it has two choices:

1. **Old, broken approach:** call ``session_inference`` with
   ``max_tokens=20`` (or some other small cap) and discard the
   response. This wastes an inference call, and on reasoning-model
   families (Nemotron-3-super, qwen3.5 thinking mode) the small
   cap cuts the model off mid-reasoning, polluting conversation
   history with a truncated fragment like
   ``"We need to respond with 'eady' as per instr"``.

2. **New approach (this module):** QUEUE the message in context
   and let the next *real* inference (the one where we actually
   want a response) prepend all queued items to its prompt.
   Zero wasted inferences, zero max_tokens traps, identical
   behavior for thinking and non-thinking models.

## API

Two functions — ``queue`` and ``consume`` — and a context key
``session_injections`` (a list of strings). Producers append with
``queue``, consumers prepend with ``consume``.

## Context plumbing: ``session_injections`` is ambient

``session_injections`` is on the ambient-context whitelist in
``agent.runtime._AMBIENT_CONTEXT_KEYS``. It flows through every step
of a flow execution automatically — flows must NOT declare it in
``context.required`` / ``context.optional`` or ``publishes``, and step
authors do not need to think about carrying it forward. The runtime
handles the hand-off between steps.

This is deliberate: the queue is a per-session implementation detail
of the memoryful-session protocol, not application data the flow
author reasons about. Declaring it would create drift between flows
that remembered and flows that forgot, with the failure mode being
silent (the queue would simply not reach the consumer, as happened
before the ambient mechanism was introduced).

Ambient scope is within a single flow execution. The queue does NOT
cross tail-call or sub-flow boundaries — a session is owned by the
flow that started it, and tail-call contracts pass structured
`last_result` rather than raw session state. If an action opens a
session inside a sub-flow, the queue lives for that sub-flow and is
gone when it returns.

### Producer (a step that wants to inject context):

    from agent.session_injections import queue

    updates = {}
    queue(updates, context, "File could not be read — select a different file")
    return StepOutput(..., context_updates=updates)

### Consumer (a step about to call ``session_inference``):

    from agent.session_injections import consume

    prompt, clear = consume(context, menu_prompt)
    result = await effects.session_inference(session_id, prompt, {"temperature": 0.1})
    return StepOutput(
        ...,
        context_updates={**clear, ...other updates...},
    )

## When to use which pattern

**Use session injection (this module) when** the step's purpose
is to update model context but the response is thrown away —
seed context at session start, error notices ("couldn't parse
your answer, defaulting to X"), acknowledgments of completed
tool calls, etc.

**Use direct ``session_inference`` when** you actually need a
response — menu selections, generated code, reasoning output.
Combine with this module: a consumer always does
``prompt, clear = consume(...)`` before calling ``session_inference``
so any pending injections ride along.

The result is that EVERY session_inference call is one that
produces useful output, and no call needs a defensive
``max_tokens`` cap.
"""

from __future__ import annotations

from typing import Any

_KEY = "session_injections"


def queue(
    context_updates: dict[str, Any],
    current_context: dict[str, Any],
    message: str,
) -> None:
    """Append a message to the session-injection queue.

    Reads the current queue from ``current_context`` (the step's
    input context), appends ``message``, and stores the new list
    in ``context_updates`` (the dict the step will return). This
    two-dict pattern preserves the queue across steps — each step
    carries forward the current queue plus any new items.

    Args:
        context_updates: The step's outgoing ``context_updates``
            dict (mutated in place).
        current_context: The step's incoming ``step_input.context``.
        message: The message to queue. Typically a short sentence
            or paragraph. Longer seed-style injections are fine too.
    """
    existing = current_context.get(_KEY, [])
    if not isinstance(existing, list):
        existing = []
    # Also carry forward any queue entries another step has already
    # added to context_updates (rare, but safe).
    pending = context_updates.get(_KEY, existing)
    if not isinstance(pending, list):
        pending = list(existing)
    elif pending is existing:
        # Copy to avoid aliasing the incoming list when we append
        pending = list(pending)
    pending.append(message)
    context_updates[_KEY] = pending


def consume(
    current_context: dict[str, Any],
    prompt: str,
    separator: str = "\n\n",
) -> tuple[str, dict[str, Any]]:
    """Prepend any queued injections to ``prompt`` and clear the queue.

    Returns a tuple of ``(combined_prompt, context_updates_to_clear)``.
    The caller should merge ``context_updates_to_clear`` into their
    own ``context_updates`` before returning from the step so the
    queue doesn't get replayed on subsequent inferences.

    If the queue is empty, returns ``(prompt, {})`` — safe to always
    call at every ``session_inference`` site.

    Args:
        current_context: The step's incoming ``step_input.context``.
        prompt: The prompt the step was going to send.
        separator: String joining injections to each other and to
            the prompt. Default is a blank line — injections read
            as separate messages rather than running together.

    Returns:
        ``(prompt_to_send, {"session_injections": []})`` if there
        were pending injections; ``(prompt, {})`` otherwise.
    """
    pending = current_context.get(_KEY, [])
    if not isinstance(pending, list) or not pending:
        return prompt, {}
    combined = separator.join(str(p) for p in pending) + separator + prompt
    return combined, {_KEY: []}


def peek(current_context: dict[str, Any]) -> list[str]:
    """Return a copy of the currently-queued injections.

    Useful for logging and diagnostics. Not typically used in
    production flow code.
    """
    pending = current_context.get(_KEY, [])
    if not isinstance(pending, list):
        return []
    return list(pending)
