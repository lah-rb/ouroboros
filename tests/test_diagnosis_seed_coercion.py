"""Regression tests for action_start_diagnosis_session string coercion.

Motivating incident: a live run crashed with the cryptic error
``sequence item 7: expected str instance, list found``. Root cause:
``flows/cue/file_ops.cue`` routed ``context.validation_results`` (a
list of check dicts) into ``error_output`` when escalating to
diagnose_issue. The diagnosis seed builder then did ``"\\n".join(parts)``
with a list buried in parts, and str.join crashed the whole flow.

The upstream fix (publishing a new ``validation_output`` string and
rerouting the CUE input_map) eliminates that specific leak. But the
diagnosis action also got a defensive layer: any context field it
interpolates as a text block is now normalized through ``_as_text()``
before append. These tests lock that behaviour in so a future upstream
mistake surfaces as "thinner prompt" rather than "flow crashes".
"""

from __future__ import annotations

import pytest

from agent.actions.diagnosis_session_actions import action_start_diagnosis_session
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _build_step_input(effects: MockEffects, **context_overrides) -> StepInput:
    """Minimal StepInput suitable for this action."""
    return StepInput(
        context=dict(context_overrides),
        params={},
        meta=FlowMeta(
            flow_name="diagnose_issue",
            step_id="start_session",
            attempt=1,
        ),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_start_diagnosis_session_coerces_list_error_output():
    """error_output arriving as a list of dicts (the file_ops
    validation_results shape that caused the original crash) must not
    crash the seed builder. It should be normalized to text and the
    session started successfully.

    Also locks in the deferred-injection contract: the action must
    NOT call session_inference anymore (the old "Acknowledge with
    'ready'" ceremony was removed) — the seed is queued for the
    next real inference to consume via session_injections.consume().
    """
    effects = MockEffects()
    # This is the exact shape that crashed the flow in production:
    # a list of check dicts from run_validation_checks_from_env.
    list_shaped_error = [
        {
            "name": "syntax: parser.py",
            "passed": False,
            "stdout": "",
            "stderr": "SyntaxError: invalid syntax on line 42",
        },
        {
            "name": "lint: parser.py",
            "passed": False,
            "stdout": "",
            "stderr": "E501 line too long",
        },
    ]

    step_input = _build_step_input(
        effects,
        flow_directive="Diagnose why validation keeps failing",
        error_output=list_shaped_error,
    )

    # The key assertion: this call must not raise.
    output = await action_start_diagnosis_session(step_input)

    assert output.result.get("session_started") is True
    # Session was created…
    assert effects.call_count("start_inference_session") == 1
    # …but NO session_inference call is made here. The old pattern
    # sent a dummy ack turn with max_tokens=20; that was removed
    # because it was catastrophic for reasoning-model families
    # (Nemotron-3-super got cut mid-reasoning). See
    # agent/session_injections.py for the replacement pattern.
    assert effects.call_count("session_inference") == 0
    # Seed is queued for the next real inference to consume.
    injections = output.context_updates.get("session_injections", [])
    assert (
        isinstance(injections, list) and len(injections) == 1
    ), f"expected one queued session injection, got {injections!r}"
    # The queued seed must have absorbed the list-shaped error_output
    # without crashing — the stderr content should appear in the text.
    seed_text = injections[0]
    assert "SyntaxError" in seed_text
    assert "E501" in seed_text


@pytest.mark.asyncio
async def test_start_diagnosis_session_coerces_none_values():
    """None values (e.g. a publish that returned without setting the
    field) must pass through as empty strings, not crash join().
    """
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        flow_directive=None,
        error_output=None,
    )

    output = await action_start_diagnosis_session(step_input)
    assert output.result.get("session_started") is True


@pytest.mark.asyncio
async def test_start_diagnosis_session_coerces_dict_error_output():
    """A dict-shaped error_output (unexpected, but defensive) must
    also not crash — it gets str()'d into a readable form."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        flow_directive="test",
        error_output={"error": "something went wrong", "code": 42},
    )

    output = await action_start_diagnosis_session(step_input)
    assert output.result.get("session_started") is True


@pytest.mark.asyncio
async def test_start_diagnosis_session_happy_path_strings():
    """Baseline: the normal case (both fields as strings) still works."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        flow_directive="Investigate failing test",
        error_output="Traceback: ImportError on line 3",
    )

    output = await action_start_diagnosis_session(step_input)
    assert output.result.get("session_started") is True
    assert "diagnosis_session_id" in output.context_updates
    assert "inference_session_id" in output.context_updates


@pytest.mark.asyncio
async def test_start_diagnosis_session_does_not_call_inference():
    """Explicit regression guard: the 'Acknowledge with ready' call
    is gone and must stay gone. The seed is queued in
    session_injections for the next real inference to consume.
    """
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        flow_directive="test",
        error_output="test",
    )

    output = await action_start_diagnosis_session(step_input)

    # Session is created, not pumped
    assert effects.call_count("start_inference_session") == 1
    assert effects.call_count("session_inference") == 0

    # Seed is deferred, not sent
    queued = output.context_updates.get("session_injections", [])
    assert len(queued) == 1
    assert "test" in queued[0]


@pytest.mark.asyncio
async def test_start_diagnosis_session_preserves_existing_injections():
    """If upstream already queued an injection in context, the seed
    must APPEND (not overwrite) so the consumer sees both."""
    effects = MockEffects()
    step_input = _build_step_input(
        effects,
        flow_directive="test",
        error_output="test",
        # Upstream already has something queued (rare but possible)
        session_injections=["upstream notice"],
    )

    output = await action_start_diagnosis_session(step_input)

    queued = output.context_updates.get("session_injections", [])
    assert len(queued) == 2
    assert queued[0] == "upstream notice"
    # Our seed is the second item
    assert "test" in queued[1]
