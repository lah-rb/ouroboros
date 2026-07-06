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
async def test_persist_routing_writes_choices_findings_and_seeds_directive():
    from agent.actions.mission_actions import action_persist_routing

    m = _mission()
    fx = MockEffects(mission=m)
    out = await action_persist_routing(
        _si(m, fx, routed_flow_set="code_core", routed_profile="repair",
            router_findings="Diffuse fix across sql/compiler.py and query.py.")
    )
    assert m.config.flow_set == "code_core"
    assert m.config.task_profile == "repair"
    # code_core adopts the workspace → pending_directive seeded from objective
    assert m.pending_directive == m.objective
    # the router's findings persist for the routed flow's prompts (warm start)
    assert "Diffuse fix" in m.router_findings
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
async def test_no_repair_floor_ops_repair_stays_ops():
    # The deterministic repair floor is REMOVED — the exploratory conclude_route
    # already decided informed. A repair the router judged localized → ops stays
    # ops (langcodes: ops 3/3 vs code_core 1/3). No post-hoc override.
    from agent.actions.mission_actions import action_persist_routing

    m = _mission()
    fx = MockEffects(mission=m)
    out = await action_persist_routing(
        _si(m, fx, routed_flow_set="ops", routed_profile="repair")
    )
    assert m.config.flow_set == "ops"  # NOT forced to code_core
    assert m.config.task_profile == "repair"
    assert "repair_floor" not in out.result["method"]


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


# ── router session open / conclude retries (transient-busy + malformed) ─


class _SessionEffects(MockEffects):
    """Controls start_inference_session failures + session_inference texts."""

    def __init__(self, *, open_fails=0, conclude_texts=None, **kw):
        super().__init__(**kw)
        self._open_fails = open_fails
        self._open_calls = 0
        self._conclude_texts = list(conclude_texts or [])
        self.ended: list = []

    async def start_inference_session(self, *a, **k):
        self._open_calls += 1
        if self._open_calls <= self._open_fails:
            raise RuntimeError(
                "Start session failed: All inference instances are busy — "
                "try again later (active=1, limit=1)"
            )
        return "router-sess-1"

    async def session_inference(self, session_id, prompt, config=None, **k):
        text = (
            self._conclude_texts.pop(0)
            if self._conclude_texts
            else '```json\n{"flow_set":"ops","profile":"plain","findings":""}\n```'
        )

        class _R:
            pass

        r = _R()
        r.text = text
        return r

    async def end_inference_session(self, *a, **k):
        self.ended.append(a)


@pytest.mark.asyncio
async def test_open_router_session_no_client_retry_defaults_on_busy():
    # start_inference_session already WAITS backend_timeout server-side, so a
    # client retry just re-waits — NO client retry here. A busy open (leaked
    # session on the pool) falls to the (ops, plain) default; the leak itself is
    # fixed at the source by draining open sessions on mission exit.
    import agent.actions.router_actions as ra

    m = _mission()
    fx = _SessionEffects(open_fails=1, mission=m)  # one failure, no retry
    out = await ra.action_open_router_session(_si(m, fx))
    assert out.result["session_started"] is False
    assert fx._open_calls == 1  # single attempt — did NOT re-wait/re-try


@pytest.mark.asyncio
async def test_end_open_inference_sessions_drains_leaked_sessions():
    # The teardown drain: a session opened but never closed (parked/killed
    # mid-flow) is released so it doesn't strand the single-instance pool.
    from agent.effects.mock import MockEffects

    fx = MockEffects()
    s1 = await fx.start_inference_session()
    await fx.start_inference_session()  # s2: left open (the leak)
    await fx.end_inference_session(s1)  # one closed normally
    # the second session is left open → drain closes it
    closed = await fx.end_open_inference_sessions()
    assert closed == 1
    assert await fx.end_open_inference_sessions() == 0  # idempotent


@pytest.mark.asyncio
async def test_conclude_route_retries_malformed_then_parses():
    from agent.actions.router_actions import action_conclude_route

    m = _mission()
    fx = _SessionEffects(
        conclude_texts=[
            "sorry, thinking out loud, no json here",  # attempt 1: unparseable
            '```json\n{"flow_set":"code_core","profile":"repair","findings":"diffuse fix"}\n```',
        ],
        mission=m,
    )
    out = await action_conclude_route(_si(m, fx, router_session_id="s1"))
    assert out.context_updates["routed_flow_set"] == "code_core"  # recovered on retry
    assert out.context_updates["routed_profile"] == "repair"
    assert out.result["method"] == "llm"


# ── compiled classify flow wiring ─────────────────────────────────────


def _classify():
    steps = json.load(open(os.path.join(_ROOT, "flows", "compiled.json")))
    assert "classify" in steps, "classify flow missing from compiled.json"
    return steps["classify"]["steps"]


def test_classify_is_an_exploration_loop_not_blind_menus():
    # The blind menu turns are gone; classify now investigates first.
    s = _classify()
    assert "classify_flow_set" not in s and "classify_profile" not in s
    # explore = the read-only scout menu (run_command / read_file / conclude)
    ex = s["explore"]["turn"]
    assert ex["response_shape"] == "menu_compound"
    assert set(ex["response"]["options"]) == {"run_command", "read_file", "conclude"}
    assert ex["response"]["publish_selection"] == "router_choice"
    opt = ex["transitions"]["options"]
    assert opt["run_command"] == "do_run"
    assert opt["read_file"] == "do_read"
    assert opt["conclude"] == "conclude_route"
    # the loop is bounded and reads real effects (no write option in the router)
    assert s["do_run"]["action"] == "router_run"
    assert s["do_read"]["action"] == "router_read"
    assert "write_file" not in ex["response"]["options"]


def test_classify_conclude_publishes_route_and_findings():
    s = _classify()
    cr = s["conclude_route"]
    assert cr["action"] == "conclude_route"
    assert set(cr["publishes"]) >= {"routed_flow_set", "routed_profile", "router_findings"}


def test_classify_releases_the_router_session_before_handoff():
    # The router MUST free its memoryful session or the single-instance LLMVP
    # pool leaks and later missions default without exploring (swe-tb-router-3).
    s = _classify()
    assert s["conclude_route"]["resolver"]["rules"][0]["transition"] == "end_router_session"
    assert s["end_router_session"]["action"] == "end_inference_session"
    assert s["end_router_session"]["resolver"]["rules"][0]["transition"] == "persist_routing"


def test_classify_budget_gate_bounds_the_scout():
    s = _classify()
    rules = {r["condition"]: r["transition"] for r in s["check_budget"]["resolver"]["rules"]}
    assert rules["context.router_turn >= 5"] == "conclude_route"
    assert rules["true"] == "explore"


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


def test_router_findings_threaded_into_downstream_prompts():
    # the warm-start hand-off: replan (both decompose) + ops charter surface it
    flows = json.load(open(os.path.join(_ROOT, "flows", "compiled.json")))
    for flow, step in (("replan", "decompose_directive"), ("replan", "decompose_repair"),
                       ("ops_task", "plan_charter")):
        ck = flows[flow]["steps"][step]["prompt_template"]["context_keys"]
        assert "router_findings" in ck, f"{flow}.{step} missing router_findings"
