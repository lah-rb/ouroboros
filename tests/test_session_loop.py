"""The escalation and router tool loops share one budget gate and one
bookkeeping module (2026-09-26): each loop's two limits are declared once in
its flow, enforced by ``_templates.tool_loop_gate``, and stated to the model
by the seed from the same value (``params.tool_budget``)."""

from __future__ import annotations

import json
import os

import pytest

from agent.actions.escalation_actions import action_open_escalation_session
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.resolvers.rule import resolve_rule
from agent.session_loop import correction, observe, tool_budget

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _compiled(flow: str) -> dict:
    with open(os.path.join(_ROOT, "flows", "compiled.json")) as f:
        return json.load(f)[flow]["steps"]


def _si(params=None, **ctx) -> StepInput:
    return StepInput(
        context=ctx,
        inputs={
            "invoking_flow": "file_ops",
            "failure_evidence": "[FAIL] syntax: x.py",
            "expected_outcome": "The checks pass.",
        },
        params=params or {},
        meta=FlowMeta(flow_name="escalate", step_id="start_session"),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_the_seed_states_the_budget_the_gate_enforces():
    steps = _compiled("escalate")
    params = {"tool_budget": steps["start_session"]["params"]["tool_budget"]}
    out = await action_open_escalation_session(_si(params))
    seed = "\n".join(out.context_updates["session_injections"])
    assert f"You have up to {params['tool_budget']} tool actions." in seed

    class _Out:
        result: dict = {}

    gate = steps["check_budget"]["resolver"]
    n = params["tool_budget"]

    def route(ctx):
        return resolve_rule(gate, step_output=_Out(), context=ctx, meta={})

    assert route({"escalation_turn": n - 1}) == "work"
    assert route({"escalation_turn": n}) == "conclude"


@pytest.mark.asyncio
async def test_without_a_declared_budget_the_seed_states_no_number():
    out = await action_open_escalation_session(_si())
    seed = "\n".join(out.context_updates["session_injections"])
    assert "tool actions" not in seed


def test_the_helpers_count_and_never_judge():
    si = _si(escalation_turn=2, escalation_corrections=1)
    obs = observe(si, "Observation: ok", turn_key="escalation_turn", label="escalation")
    assert obs.result == {"action_ok": True}
    assert obs.context_updates["escalation_turn"] == 3
    cor = correction(
        si, "no such file", corrections_key="escalation_corrections", label="escalation"
    )
    assert cor.result == {"action_ok": False}  # no exhausted flag: the gate decides
    assert cor.context_updates["escalation_corrections"] == 2
    assert (
        "Action failed — no such file" in cor.context_updates["session_injections"][-1]
    )
    assert tool_budget(_si({"tool_budget": "5"})) == 5 and tool_budget(_si()) == 0
