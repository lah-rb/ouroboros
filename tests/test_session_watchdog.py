"""Session-turn health watchdog + t* temperature resolution.

Regression guard for the qwen3-next-coder hang (live, ~79 min freeze): the
session-inference path — the *entire* diagnose_issue investigation + conclude +
systemic_scan + all AST-edit inferences — previously fired a raw `timeout=None`
POST with NO watchdog, while the completion path had one. So a stuck OR runaway
session turn (the Qwen3-Next long-context decay-clamp repetition loop generating
toward max_tokens) hung the agent until an external kill.

`session_turn` now routes through the same `_request_with_health_watchdog` as
completions, passing the session response key and a runaway token ceiling
(stall detection alone never fires on a runaway — tokens keep advancing).

Also locks in the conclude/scan temperature conversion from a flat overwrite to
`t*` (task-scaled off the model default), since a flat low temp lands in the
greedy regime that worsens repetition looping.
"""

from __future__ import annotations

import pytest

from agent.effects.inference import (
    COMPLETION_RUNAWAY_TOKEN_CEILING,
    SESSION_RUNAWAY_TOKEN_CEILING,
    InferenceEffect,
    resolve_temperature,
)
from agent.effects.protocol import InferenceResult


def test_resolve_temperature_relative_scales_off_model_default():
    """t* multiplies the model's base — the conclude (t*0.7) / scan (t*0.5)
    conversions depend on this, and the higher 0.8 base keeps them out of the
    low-temp looping regime."""
    assert resolve_temperature("t*0.7", model_default=0.8) == pytest.approx(0.56)
    assert resolve_temperature("t*0.5", model_default=0.8) == pytest.approx(0.40)
    # scales per model: same multiplier, different absolute temp
    assert resolve_temperature("t*0.7", model_default=1.0) == pytest.approx(0.70)
    assert resolve_temperature("t*0.7", model_default=0.6) == pytest.approx(0.42)
    # plain floats pass through; None stays None
    assert resolve_temperature(0.4, model_default=0.8) == 0.4
    assert resolve_temperature(None) is None


@pytest.mark.asyncio
async def test_session_turn_routes_through_watchdog_with_ceiling(monkeypatch):
    """The fix: session_turn must go through the health watchdog (not a raw
    post), pass the session payload key, and carry a runaway ceiling — closing
    the unprotected gap that let qwen3-next hang."""
    eff = InferenceEffect(model_default_temperature=0.8)
    captured: dict = {}

    async def fake_get_client():
        return object()  # unused — watchdog is stubbed

    async def fake_watchdog(
        client, request_body, response_key="completion", runaway_token_ceiling=None
    ):
        captured["response_key"] = response_key
        captured["ceiling"] = runaway_token_ceiling
        captured["request_vars"] = request_body["variables"]["request"]
        return InferenceResult(text="ok", tokens_generated=1, finished=True)

    monkeypatch.setattr(eff, "_get_client", fake_get_client)
    monkeypatch.setattr(eff, "_request_with_health_watchdog", fake_watchdog)

    result = await eff.session_turn("sess-1", "diagnose this", {"temperature": "t*0.7"})

    assert result.text == "ok"
    assert captured["response_key"] == "sessionCompletion"
    assert captured["ceiling"] == SESSION_RUNAWAY_TOKEN_CEILING
    # t* resolved against the model default (0.8) before the request is built
    assert captured["request_vars"]["temperature"] == pytest.approx(0.56)
    assert captured["request_vars"]["sessionId"] == "sess-1"


@pytest.mark.asyncio
async def test_run_inference_carries_completion_ceiling(monkeypatch):
    """The completion path (whole-file rewrites / scaffolds) must ALSO carry a
    runaway ceiling — the live gap that let a qwen3-next rewrite generate 47k+
    tokens unbounded (the stall watchdog can't catch an advancing-token loop)."""
    eff = InferenceEffect(model_default_temperature=0.8)
    captured: dict = {}

    async def fake_get_client():
        return object()

    async def fake_watchdog(
        client, request_body, response_key="completion", runaway_token_ceiling=None
    ):
        captured["response_key"] = response_key
        captured["ceiling"] = runaway_token_ceiling
        return InferenceResult(text="ok", tokens_generated=1, finished=True)

    monkeypatch.setattr(eff, "_get_client", fake_get_client)
    monkeypatch.setattr(eff, "_request_with_health_watchdog", fake_watchdog)

    result = await eff.run_inference("write the whole file")

    assert result.text == "ok"
    assert captured["response_key"] == "completion"
    assert captured["ceiling"] == COMPLETION_RUNAWAY_TOKEN_CEILING


def test_ceilings_bounded_below_max_context_completion_above_session():
    """Both ceilings catch a runaway far below the 262k max_tokens default;
    the completion ceiling sits higher (whole-file rewrites are legitimately
    larger than session turns) but still well under the runaway range."""
    assert 8192 <= SESSION_RUNAWAY_TOKEN_CEILING <= 65536
    assert SESSION_RUNAWAY_TOKEN_CEILING < COMPLETION_RUNAWAY_TOKEN_CEILING < 131072
