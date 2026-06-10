"""Regression tests for orphaned-session cleanup on flow failure.

Motivating incident: the e75 challenge run leaked 5 diagnose_issue
sessions. Each leak followed the same pattern — the start_session step
created an LLMVP session, then an uncaught exception raised inside a
later step, propagated up through execute_flow without the flow's
end_session step ever running. LLMVP's session manager kept the session
alive indefinitely, eating into the single-instance capacity.

These tests verify that execute_flow's try/except wrapper around the
main loop calls _cleanup_orphaned_sessions before re-raising, so the
session is released at the runtime layer regardless of flow structure.
"""

from __future__ import annotations

import pytest

from agent.models import (
    FlowDefinition,
    StepDefinition,
    StepInput,
    StepOutput,
)
from agent.runtime import execute_flow, MaxStepsExceeded
from agent.effects.mock import MockEffects


def _make_flow_start_session_then_boom() -> FlowDefinition:
    """Two-step flow: start_session (mock) → explode.

    start_session populates inference_session_id in context.
    explode raises an uncaught exception.
    There is no end_session step — simulates the pathological case
    where a flow bails before cleanup.
    """
    return FlowDefinition(
        flow="test_boom",
        entry="start_session",
        steps={
            "start_session": StepDefinition(
                action="mock_start_session",
                description="seed a session id",
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "explode"}],
                },
                publishes=["inference_session_id"],
            ),
            "explode": StepDefinition(
                action="mock_explode",
                description="raise mid-flow",
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "never"}],
                },
            ),
            "never": StepDefinition(
                action="noop",
                description="terminal — never reached",
                resolver={"type": "rule", "rules": []},
                status="done",
            ),
        },
        returns={},
    )


@pytest.mark.asyncio
async def test_uncaught_exception_in_flow_cleans_up_session():
    """When a step raises mid-flow, the runtime should call
    end_inference_session on any session_id in the accumulator before
    the exception propagates out of execute_flow."""

    async def mock_start_session(step_input: StepInput) -> StepOutput:
        # Kick off a session via the mock effects — MockEffects
        # start_inference_session returns a synthetic id.
        sid = await step_input.effects.start_inference_session({"ttl_seconds": 600})
        return StepOutput(
            result={"session_started": True},
            context_updates={"inference_session_id": sid},
        )

    async def mock_explode(step_input: StepInput) -> StepOutput:
        raise RuntimeError("simulated mid-flow failure")

    async def noop(step_input: StepInput) -> StepOutput:
        return StepOutput(result={})

    flow = _make_flow_start_session_then_boom()
    effects = MockEffects()

    from agent.errors import FlowRuntimeError

    # The runtime wraps per-action exceptions in FlowRuntimeError with
    # the original cause chained via `from e` — we catch the wrapper.
    with pytest.raises(FlowRuntimeError, match="simulated mid-flow failure"):
        await execute_flow(
            flow_def=flow,
            inputs={},
            action_registry={
                "mock_start_session": mock_start_session,
                "mock_explode": mock_explode,
                "noop": noop,
            },
            effects=effects,
        )

    # The key assertion: the session that was created must be closed.
    # MockEffects records every effect call in _calls.
    session_starts = effects.call_count("start_inference_session")
    session_ends = effects.call_count("end_inference_session")
    assert session_starts == 1, "expected exactly one session start"
    assert session_ends == 1, (
        "expected session to be ended via cleanup; "
        f"calls: {[c.method for c in effects.calls]}"
    )


@pytest.mark.asyncio
async def test_max_steps_exceeded_still_cleans_up_session():
    """The MaxStepsExceeded path — an infinite loop of back-to-back
    step executions — must also release orphaned sessions. This was
    already covered before the refactor; verifying we didn't regress
    when we factored the cleanup into a shared helper."""

    async def mock_start_session(step_input: StepInput) -> StepOutput:
        sid = await step_input.effects.start_inference_session()
        return StepOutput(
            result={},
            context_updates={"inference_session_id": sid},
        )

    async def noop(step_input: StepInput) -> StepOutput:
        return StepOutput(result={})

    # Flow that loops forever: start → loop → loop → loop → ...
    flow = FlowDefinition(
        flow="test_loop",
        entry="start_session",
        steps={
            "start_session": StepDefinition(
                action="mock_start_session",
                description="seed session",
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "loop"}],
                },
                publishes=["inference_session_id"],
            ),
            "loop": StepDefinition(
                action="noop",
                description="loops to itself",
                resolver={
                    "type": "rule",
                    "rules": [{"condition": "true", "transition": "loop"}],
                },
            ),
        },
        returns={},
    )

    effects = MockEffects()
    with pytest.raises(MaxStepsExceeded):
        await execute_flow(
            flow_def=flow,
            inputs={},
            action_registry={
                "mock_start_session": mock_start_session,
                "noop": noop,
            },
            effects=effects,
            max_steps=10,  # small, to terminate quickly
        )

    session_ended = effects.call_count("end_inference_session")
    assert session_ended == 1, (
        "expected session to be released on MaxStepsExceeded; "
        f"calls: {[c.method for c in effects.calls]}"
    )
