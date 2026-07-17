"""Multi-model Phase 4: the model override from flow config to the wire.

Three layers pinned: InferenceEffect puts `model` on the GraphQL request
(per-call override beats effect default beats absent), the runtime's
inference executor routes a model-overridden step STATELESS (sessions
and the reasoning head-swap are resident-local machinery) with the model
in config_overrides, and the compiled escalate flow carries the
consult_boss wiring end to end."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.effects.inference import InferenceEffect
from agent.effects.protocol import InferenceResult
from agent.loop import _load_flows
from agent.models import FlowMeta, StepInput
from agent.runtime import _execute_inference_action

_REPO = Path(__file__).parent.parent


# ── InferenceEffect wire contract ────────────────────────────────────


class _CaptureEffect(InferenceEffect):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.bodies: list[dict] = []

    async def _request_with_health_watchdog(self, client, request_body, **kw):
        self.bodies.append(request_body)
        return InferenceResult(text="ok", tokens_generated=1, finished=True)


@pytest.mark.asyncio
async def test_effect_model_override_beats_default_beats_absent():
    eff = _CaptureEffect(model="default-model")
    await eff.run_inference("hi")
    await eff.run_inference("hi", config_overrides={"model": "boss-sonnet"})

    bare = _CaptureEffect()
    await bare.run_inference("hi")

    assert eff.bodies[0]["variables"]["request"]["model"] == "default-model"
    assert eff.bodies[1]["variables"]["request"]["model"] == "boss-sonnet"
    assert "model" not in bare.bodies[0]["variables"]["request"]


# ── Runtime: model-overridden step runs stateless ────────────────────


class _ScriptedEffects:
    def __init__(self):
        self.run_calls: list[dict] = []
        self.session_calls: list[dict] = []

    async def run_inference(self, prompt, config_overrides=None, **kw):
        self.run_calls.append({"prompt": prompt, "overrides": config_overrides or {}})
        return InferenceResult(text="direction", tokens_generated=3, finished=True)

    async def session_inference(self, session_id, prompt, config_overrides=None):
        self.session_calls.append({"session_id": session_id})
        return InferenceResult(text="direction", tokens_generated=3, finished=True)


def _escalate_flow():
    return _load_flows(str(_REPO / "flows"))["escalate"]


@pytest.mark.asyncio
async def test_do_consult_routes_stateless_with_model(monkeypatch):
    monkeypatch.setenv("OURO_ADAPTIVE_REASONING", "1")  # would add reasoning if buggy
    flow = _escalate_flow()
    step_def = flow.steps["do_consult"]
    effects = _ScriptedEffects()
    step_input = StepInput(
        # A live session handle is present — the model override must
        # still route the call STATELESS, never through the session.
        context={
            "inference_session_id": "sess-1",
            "escalation_session_id": "sess-1",
            "escalation_choice_arg": "Am I fighting a stale workaround?",
        },
        params={},
        config=dict(step_def.config or {}),
        meta=FlowMeta(flow_name="escalate", step_id="do_consult"),
        effects=effects,
    )
    out = await _execute_inference_action(
        step_def,
        step_input,
        flow,
        inputs={
            "failure_evidence": "lint E402 fails every fix attempt",
            "expected_outcome": "engine.py passes its gates",
        },
        effects=effects,
    )

    assert effects.session_calls == []  # never the session path
    assert len(effects.run_calls) == 1
    overrides = effects.run_calls[0]["overrides"]
    assert overrides.get("model") == "boss-sonnet"
    assert "reasoning" not in overrides  # head-swap is resident-local only
    prompt = effects.run_calls[0]["prompt"]
    assert "lint E402" in prompt and "stale workaround" in prompt
    assert out.context_updates.get("inference_response") == "direction"


# ── Compiled-flow wiring pin ─────────────────────────────────────────


def test_compiled_escalate_carries_consult_wiring():
    raw = json.loads((_REPO / "flows" / "compiled.json").read_text())["escalate"]
    steps = raw["steps"]
    assert steps["do_consult"]["config"]["model"] == "boss-sonnet"
    work_transitions = steps["work"]["turn"]["transitions"]["options"]
    assert work_transitions["consult_boss"] == "do_consult"
    assert steps["fold_consult"]["action"] == "escalation_fold_consult"

    from agent.actions.registry import build_action_registry

    assert "escalation_fold_consult" in build_action_registry().registered_actions
