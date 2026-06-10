"""rewrite_symbol_turn consumes pending session injections.

The edit-session seed (file outline, contracts, dependency signatures) is
queued by start_edit_session as a session injection. Before this fix the
patch flow had no consumer — the seed never reached the model. The first
inference of each rewrite turn now prepends pending injections and clears
the queue on delivered turns.
"""

from __future__ import annotations

import pytest

from agent.actions.ast_actions import action_rewrite_symbol_turn
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

_BODY = "def f(x):\n    return x\n"
_RESPONSE = "```python\ndef f(x):\n    return x + 1\n```"


def _si(effects, session_id, **ctx) -> StepInput:
    context = {
        "edit_session_id": session_id,
        "current_symbol": {
            "name": "f",
            "kind": "function",
            "body": _BODY,
            "line": 1,
            "end_line": 2,
        },
        "file_content": _BODY,
        **ctx,
    }
    return StepInput(
        context=context,
        params={"file_path": "mod.py"},
        meta=FlowMeta(flow_name="patch", step_id="rewrite_symbol"),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_pending_injection_prefixes_prompt_and_clears():
    effects = MockEffects(inference_responses=[_RESPONSE])
    session_id = await effects.start_inference_session()
    si = _si(effects, session_id, session_injections=["[Seed: file outline]"])
    out = await action_rewrite_symbol_turn(si)
    assert out.result["rewrite_success"] is True
    turns = effects._mock_sessions[session_id]
    assert turns[0]["prompt"].startswith("[Seed: file outline]")
    # Queue cleared so later turns don't replay the seed.
    assert out.context_updates["session_injections"] == []


@pytest.mark.asyncio
async def test_no_injection_leaves_queue_key_untouched():
    effects = MockEffects(inference_responses=[_RESPONSE])
    session_id = await effects.start_inference_session()
    si = _si(effects, session_id)
    out = await action_rewrite_symbol_turn(si)
    assert out.result["rewrite_success"] is True
    assert "session_injections" not in out.context_updates


@pytest.mark.asyncio
async def test_injection_cleared_on_failed_rewrite_too():
    # Empty model response → rewrite fails, but the injection was
    # delivered with the prompt, so the queue must still clear.
    effects = MockEffects(inference_responses=[""])
    session_id = await effects.start_inference_session()
    si = _si(effects, session_id, session_injections=["[Seed: file outline]"])
    out = await action_rewrite_symbol_turn(si)
    assert out.result["rewrite_success"] is False
    assert out.context_updates["session_injections"] == []
