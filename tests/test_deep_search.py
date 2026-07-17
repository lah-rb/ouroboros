"""Deep-search loop v1 — the reusable reflect-and-refine web-research primitive.

Pins: the session gates on web_research (declines cleanly when off); search runs
one Exa query and hands hits to condense; condense distills the hits to the
answer-bearing fact and folds ONLY that into the session (raw pages never enter
it) at low temperature (the reasoning-head-swap seam); conclude parses the
grounded summary; and the compiled wiring — deep_search's loop + escalate's
web_search → deep_search sub-flow dispatch.
"""

from __future__ import annotations

import json
import os

import pytest

from agent.actions.deep_search_actions import (
    MAX_SEARCH_ROUNDS,
    action_conclude_search,
    action_condense_results,
    action_open_search_session,
    action_search_run,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState


def _si(fx=None, inputs=None, **ctx) -> StepInput:
    return StepInput(
        context=ctx,
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="deep_search", step_id="x"),
        effects=fx if fx is not None else MockEffects(),
    )


def _queued(out) -> str:
    return "\n\n".join(out.context_updates.get("session_injections", []) or [])


def _mission(web_research=True) -> MissionState:
    return MissionState(
        objective="o",
        config=MissionConfig(working_directory="/tmp/x", web_research=web_research),
    )


# ── open session (+ web_research gate) ────────────────────────────────


@pytest.mark.asyncio
async def test_open_session_declines_when_web_research_off():
    fx = MockEffects(mission=_mission(web_research=False))
    out = await action_open_search_session(
        _si(fx, inputs={"brief": "how does X work?"})
    )
    assert out.result["session_started"] is False
    assert out.context_updates["research_summary"] == ""
    assert out.context_updates["search_sufficient"] is False


@pytest.mark.asyncio
async def test_open_session_declines_without_brief():
    fx = MockEffects(mission=_mission(web_research=True))
    out = await action_open_search_session(_si(fx, inputs={"brief": ""}))
    assert out.result["session_started"] is False


@pytest.mark.asyncio
async def test_open_session_opens_and_seeds_brief():
    fx = MockEffects(mission=_mission(web_research=True))
    out = await action_open_search_session(
        _si(fx, inputs={"brief": "what exception does flask raise on empty separator?"})
    )
    assert out.result["session_started"] is True
    cu = out.context_updates
    assert cu["search_session_id"] and cu["search_round"] == 0
    assert cu["search_queries_run"] == []
    assert "empty separator" in _queued(out)  # brief is in the seed


# ── search ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_run_returns_hits():
    fx = MockEffects()
    fx._state["mcp_tool_responses"] = {
        "web_search_exa": {
            "results": [
                {
                    "url": "https://docs/x",
                    "title": "X docs",
                    "content": "X raises ValueError.",
                }
            ]
        }
    }
    out = await action_search_run(
        _si(fx, search_choice_arg="what does X raise", search_session_id="s")
    )
    assert out.result.get("has_hits") is True
    assert out.context_updates["raw_search_results"][0]["url"] == "https://docs/x"
    assert out.context_updates["search_queries_run"] == ["what does X raise"]
    assert out.context_updates["last_query"] == "what does X raise"


@pytest.mark.asyncio
async def test_search_run_missing_query_is_correction():
    out = await action_search_run(_si(search_choice_arg="", search_session_id="s"))
    assert out.result["action_ok"] is False
    assert "search_corrections" in out.context_updates


@pytest.mark.asyncio
async def test_search_run_no_hits_is_correction_and_records_query():
    fx = MockEffects()
    fx._state["mcp_tool_responses"] = {"web_search_exa": {"results": []}}
    out = await action_search_run(
        _si(fx, search_choice_arg="obscure query", search_session_id="s")
    )
    assert (
        out.result["action_ok"] is False
    )  # no hits → refine (correction, not a round)
    assert out.context_updates["search_queries_run"] == ["obscure query"]
    assert out.context_updates["raw_search_results"] == []


# ── condense (the context-discipline guarantee) ───────────────────────


@pytest.mark.asyncio
async def test_condense_folds_only_the_fact_not_raw_docs():
    fx = MockEffects(
        inference_responses=["X raises ValueError on empty input (https://docs/x)."]
    )
    raw = [
        {
            "url": "https://docs/x",
            "title": "X",
            "content": "RAW_DOC_BODY_should_not_leak " * 20,
        }
    ]
    out = await action_condense_results(
        _si(fx, raw_search_results=raw, last_query="what does X raise", search_round=0)
    )
    folded = _queued(out)
    assert "ValueError on empty input" in folded  # the distilled fact IS folded
    assert (
        "RAW_DOC_BODY_should_not_leak" not in folded
    )  # raw pages do NOT enter the session
    assert out.context_updates["search_round"] == 1  # spends one round
    # Distilled in an ephemeral session steered LOW via the reasoning head-swap.
    calls = fx.calls_to("session_inference")
    assert calls and calls[0].args["config_overrides"].get("reasoning") == "low"
    assert calls[0].args["config_overrides"].get("temperature") == "t*0.2"


@pytest.mark.asyncio
async def test_condense_no_hits_still_advances_round():
    out = await action_condense_results(
        _si(raw_search_results=[], last_query="q", search_round=1)
    )
    assert out.context_updates["search_round"] == 2


# ── conclude (synthesize) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_conclude_search_parses_summary_and_sufficient():
    fx = MockEffects(
        inference_responses=[
            '```json\n{"research_summary": "X raises ValueError (https://docs/x).", '
            '"sufficient": true}\n```'
        ]
    )
    out = await action_conclude_search(
        _si(fx, search_session_id="s", search_queries_run=["what does X raise"])
    )
    assert out.result["sufficient"] is True
    assert "ValueError" in out.context_updates["research_summary"]
    assert out.context_updates["queries_run"] == ["what does X raise"]


@pytest.mark.asyncio
async def test_conclude_search_failsafe_on_unparseable():
    fx = MockEffects(inference_responses=["not json at all"])
    out = await action_conclude_search(
        _si(fx, search_session_id="s", search_queries_run=[])
    )
    assert out.result["sufficient"] is False
    assert out.context_updates["research_summary"] == ""


# ── compiled wiring ───────────────────────────────────────────────────


def _compiled() -> dict:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "flows", "compiled.json")) as f:
        return json.load(f)


def test_deep_search_flow_wiring():
    steps = _compiled()["deep_search"]["steps"]
    # reflect menu: search → do_search, done → synthesize
    assert steps["reflect"]["turn"]["transitions"]["options"] == {
        "search": "do_search",
        "done": "synthesize",
    }
    # do_search routes hits → condense, exhausted → synthesize, else → budget
    targets = [r.get("transition") for r in steps["do_search"]["resolver"]["rules"]]
    assert targets == ["condense", "synthesize", "check_budget"]
    # condense loops back to the budget gate; budget caps at MAX_SEARCH_ROUNDS
    assert steps["condense"]["resolver"]["rules"][0]["transition"] == "check_budget"
    budget_rule = steps["check_budget"]["resolver"]["rules"][0]["condition"]
    assert str(MAX_SEARCH_ROUNDS) in budget_rule
    assert steps["done"]["terminal"] is True


def test_escalate_web_search_dispatches_deep_search():
    steps = _compiled()["escalate"]["steps"]
    assert (
        steps["work"]["turn"]["transitions"]["options"]["web_search"] == "do_web_search"
    )
    assert steps["do_web_search"]["action"] == "flow"
    assert steps["do_web_search"]["flow"] == "deep_search"
    assert steps["fold_search"]["action"] == "escalation_fold_search"
