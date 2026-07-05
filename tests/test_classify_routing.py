"""In-graph task router (the `classify` flow) — full-local autonomy.

Pins the routing plumbing: `auto` resolves to the classify entry flow;
persist_routing writes the menu choices onto the mission (defaulting to
ops/plain, seeding pending_directive for code_core) and hands off on the picked
flow_set; the compiled classify flow has the two menu turns + tail-call handoffs
wired to the right targets; and the config validators accept `auto`.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mission(**cfg) -> MissionState:
    return MissionState(
        objective="the scripts fail; debug and fix them across the repo",
        status="active",
        config=MissionConfig(working_directory="/w", flow_set="auto", **cfg),
    )


def _si(mission, effects, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="classify", step_id="persist_routing"),
        effects=effects,
    )


# ── auto → classify entry ─────────────────────────────────────────────


def test_auto_flow_set_registered_and_entry_is_classify():
    from agent.flow_sets import FLOW_SETS, get_flow_set

    assert "auto" in FLOW_SETS
    assert get_flow_set("auto").entry_flow == "classify"
    # concrete sets are unchanged (explicit config still routes directly)
    assert get_flow_set("code_core").entry_flow == "mission_control"
    assert get_flow_set("ops").entry_flow == "ops_control"


def test_yaml_config_accepts_auto():
    from agent.mission_config import MissionYAMLConfig

    cfg = MissionYAMLConfig(objective="do the thing", flow_set="auto")  # must not raise
    assert cfg.flow_set == "auto"


# ── persist_routing ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_routing_writes_choices_and_seeds_directive():
    from agent.actions.mission_actions import action_persist_routing

    m = _mission()
    fx = MockEffects(mission=m)
    out = await action_persist_routing(
        _si(m, fx, routed_flow_set="code_core", routed_profile="repair")
    )
    assert m.config.flow_set == "code_core"
    assert m.config.task_profile == "repair"
    # code_core adopts the workspace → pending_directive seeded from objective
    assert m.pending_directive == m.objective
    assert out.result["flow_set"] == "code_core"  # handoff resolver reads this


@pytest.mark.asyncio
async def test_persist_routing_ops_does_not_seed_directive():
    from agent.actions.mission_actions import action_persist_routing

    m = _mission()
    fx = MockEffects(mission=m)
    out = await action_persist_routing(
        _si(m, fx, routed_flow_set="ops", routed_profile="answer")
    )
    assert m.config.flow_set == "ops"
    assert m.config.task_profile == "answer"
    assert not m.pending_directive
    assert out.result["flow_set"] == "ops"


@pytest.mark.asyncio
async def test_persist_routing_defaults_ops_plain_on_no_selection():
    # no_answer path: neither choice published → safe (ops, plain) default.
    from agent.actions.mission_actions import action_persist_routing

    m = _mission()
    fx = MockEffects(mission=m)
    out = await action_persist_routing(_si(m, fx))
    assert m.config.flow_set == "ops"
    assert m.config.task_profile == "plain"
    assert out.result["method"] == "default"


@pytest.mark.asyncio
async def test_persist_routing_rejects_invalid_labels():
    from agent.actions.mission_actions import action_persist_routing

    m = _mission()
    fx = MockEffects(mission=m)
    await action_persist_routing(
        _si(m, fx, routed_flow_set="banana", routed_profile="nonsense")
    )
    assert m.config.flow_set == "ops"
    assert m.config.task_profile == "plain"


# ── compiled classify flow wiring ─────────────────────────────────────


def _classify():
    steps = json.load(open(os.path.join(_ROOT, "flows", "compiled.json")))
    assert "classify" in steps, "classify flow missing from compiled.json"
    return steps["classify"]["steps"]


def test_classify_menu_turns_publish_the_routing_keys():
    s = _classify()
    fs = s["classify_flow_set"]["turn"]
    pr = s["classify_profile"]["turn"]
    assert fs["response"]["publish_selection"] == "routed_flow_set"
    assert pr["response"]["publish_selection"] == "routed_profile"
    # option keys match the routable label sets exactly
    assert set(fs["response"]["options"]) == {"ops", "code_core"}
    assert set(pr["response"]["options"]) == {
        "service", "data_transform", "invertible", "repair", "answer", "plain",
    }


def test_classify_handoffs_tail_call_the_right_controllers():
    s = _classify()
    rules = {
        r["condition"]: r["transition"]
        for r in s["persist_routing"]["resolver"]["rules"]
    }
    assert rules["result.flow_set == 'code_core'"] == "handoff_code_core"
    assert rules["true"] == "handoff_ops"
    assert s["handoff_code_core"]["tail_call"]["flow"] == "ingest_workspace"
    assert s["handoff_ops"]["tail_call"]["flow"] == "ops_control"


def test_classify_no_answer_distinct_from_default():
    # lint contract: default != no_answer for both menu turns
    s = _classify()
    for step in ("classify_flow_set", "classify_profile"):
        t = s[step]["turn"]["transitions"]
        assert t["default"] != t["no_answer"]
