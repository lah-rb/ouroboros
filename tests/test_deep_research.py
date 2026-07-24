"""Deep-research sweep v1 — the parallel, stateless deep_search generalization.

Pins: decompose gates on web_research and parses angles (degrading to the
brief on a parse miss); the wave fans out every angle (search + stateless
condense), appends findings to the explicit ledger, and sizes inference
concurrency via the shared pool-fit gate (server kvPoolTokens beats the
static fallback — recorded in the perf sidecar); merge_reflect emits
sufficiency or gap questions; synthesize grounds the final summary. Plus
unit pins for the shared fanout helper itself.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.deep_research_actions import (
    action_research_decompose,
    action_research_merge_reflect,
    action_research_synthesize,
    action_research_wave,
)
from agent.actions.fanout import GateDecision, estimate_draw, pool_fit_width
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState


def _si(fx=None, inputs=None, params=None, **ctx) -> StepInput:
    return StepInput(
        context=ctx,
        inputs=inputs or {},
        params=params or {},
        meta=FlowMeta(flow_name="deep_research", step_id="x"),
        effects=fx if fx is not None else MockEffects(),
    )


def _mission(web_research=True) -> MissionState:
    return MissionState(
        objective="o",
        config=MissionConfig(working_directory="/tmp/x", web_research=web_research),
    )


def _exa(fx: MockEffects, hits: list[dict]) -> None:
    fx._state["mcp_tool_responses"] = {"web_search_exa": {"results": hits}}


_HITS = [
    {"url": "https://a.example", "title": "A", "content": "alpha content"},
    {"url": "https://b.example", "title": "B", "content": "beta content"},
]


# ── decompose ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_decompose_declines_when_web_research_off():
    fx = MockEffects(mission=_mission(web_research=False))
    out = await action_research_decompose(_si(fx, inputs={"brief": "how?"}))
    assert out.result["research_started"] is False
    assert out.context_updates["research_summary"] == ""


@pytest.mark.asyncio
async def test_decompose_parses_angles():
    fx = MockEffects(
        mission=_mission(),
        inference_responses=['```json\n{"angles": ["q1", "q2", "q3"]}\n```'],
    )
    out = await action_research_decompose(_si(fx, inputs={"brief": "the brief"}))
    assert out.result["research_started"] is True
    assert out.context_updates["research_angles"] == ["q1", "q2", "q3"]
    assert out.context_updates["research_ledger"] == []
    assert out.context_updates["research_wave_n"] == 0


@pytest.mark.asyncio
async def test_decompose_degrades_to_brief_on_parse_miss():
    fx = MockEffects(mission=_mission(), inference_responses=["not json at all"])
    out = await action_research_decompose(_si(fx, inputs={"brief": "solve X"}))
    assert out.result["research_started"] is True
    assert out.context_updates["research_angles"] == ["solve X"]


# ── wave (the fan-out) ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wave_fans_out_all_angles_and_appends_ledger(tmp_path):
    fx = MockEffects(
        mission=_mission(),
        inference_responses=[
            "Fact one (https://a.example).",
            "INSUFFICIENT",
        ],
    )
    _exa(fx, _HITS)
    out = await action_research_wave(
        _si(
            fx,
            inputs={"working_directory": str(tmp_path)},
            research_angles=["q1", "q2"],
            research_ledger=[],
            research_wave_n=0,
        )
    )
    assert out.result["wave_ok"] is True and out.result["n_findings"] == 2
    ledger = out.context_updates["research_ledger"]
    assert len(ledger) == 2 and ledger[0]["wave"] == 1
    by_query = {f["query"]: f for f in ledger}
    assert by_query["q1"]["ok"] is True and "Fact one" in by_query["q1"]["fact"]
    assert by_query["q2"]["ok"] is False  # INSUFFICIENT condense
    assert out.context_updates["research_wave_n"] == 1
    assert out.context_updates["research_queries_run"] == ["q1", "q2"]
    # one condense inference per angle — the fan-out unit
    n_inference = sum(1 for c in fx.calls if c.method == "run_inference")
    assert n_inference == 2


@pytest.mark.asyncio
async def test_wave_records_server_budget_in_perf_sidecar(tmp_path):
    # kvPoolTokens=4096: budget80=3276 < one condense draw → 1-wide waves,
    # budget_source=server in the burst row (fix-B provenance, live shape).
    fx = MockEffects(
        mission=_mission(),
        inference_responses=["Fact (https://a.example)."] * 2,
        pool_health={"kvPoolTokens": 4096, "decodeMode": "batched"},
    )
    _exa(fx, _HITS)
    out = await action_research_wave(
        _si(
            fx,
            inputs={"working_directory": str(tmp_path)},
            research_angles=["q1", "q2"],
        )
    )
    assert out.result["n_findings"] == 2
    rows = [
        json.loads(line) for line in (tmp_path / ".agent" / "swarm_perf.jsonl").open()
    ]
    burst = next(r for r in rows if r["event"] == "burst")
    assert burst["budget_source"] == "server"
    assert burst["pool_budget"] == 4096
    assert burst["gate"] == "waved" and burst["sem"] == 1
    assert sum(1 for r in rows if r["event"] == "worker") == 2


@pytest.mark.asyncio
async def test_wave_degrades_when_search_unavailable():
    class _NoMcp(MockEffects):
        async def mcp_connect(self, server_name, server_command=None):
            raise FileNotFoundError("~/.exa_key missing")

    fx = _NoMcp(mission=_mission())
    out = await action_research_wave(_si(fx, research_angles=["q1"]))
    assert out.result["wave_ok"] is False
    assert out.context_updates["research_wave_n"] == 1


# ── merge_reflect ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_reflect_emits_gaps():
    fx = MockEffects(
        inference_responses=[
            '```json\n{"sufficient": false, "gaps": ["g1", "g2"]}\n```'
        ]
    )
    out = await action_research_merge_reflect(
        _si(
            fx,
            research_brief="b",
            research_ledger=[{"query": "q1", "fact": "f1", "ok": True}],
        )
    )
    assert out.result["sufficient"] is False and out.result["n_gaps"] == 2
    assert out.context_updates["research_angles"] == ["g1", "g2"]


@pytest.mark.asyncio
async def test_merge_reflect_sufficient_clears_gaps():
    fx = MockEffects(
        inference_responses=['```json\n{"sufficient": true, "gaps": ["stale"]}\n```']
    )
    out = await action_research_merge_reflect(
        _si(fx, research_brief="b", research_ledger=[{"query": "q", "fact": "f"}])
    )
    assert out.result["sufficient"] is True
    assert out.context_updates["research_angles"] == []
    assert out.context_updates["research_sufficient"] is True


# ── synthesize ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_grounds_summary():
    fx = MockEffects(
        inference_responses=[
            '```json\n{"research_summary": "S (https://a.example)", '
            '"sufficient": true}\n```'
        ]
    )
    out = await action_research_synthesize(
        _si(
            fx,
            research_brief="b",
            research_ledger=[{"query": "q", "fact": "f (https://a.example)"}],
            research_queries_run=["q"],
        )
    )
    assert out.result["sufficient"] is True
    assert "https://a.example" in out.context_updates["research_summary"]
    assert out.context_updates["queries_run"] == ["q"]


# ── shared fanout helper ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pool_fit_width_server_budget_overrides_fallback():
    fx = MockEffects(pool_health={"kvPoolTokens": 4096, "decodeMode": "batched"})
    d = await pool_fit_width(fx, [3000, 3000], pool_budget_fallback=131072)
    assert d.budget_source == "server" and d.pool_budget == 4096
    assert d.gate == "waved" and d.width == 1


@pytest.mark.asyncio
async def test_pool_fit_width_falls_back_loudly():
    fx = MockEffects()  # pool_health {} = old server
    d = await pool_fit_width(fx, [3000, 3000], pool_budget_fallback=131072)
    assert d.budget_source == "fallback" and d.pool_budget == 131072
    assert d.gate == "full" and d.width == 2


@pytest.mark.asyncio
async def test_pool_fit_width_midrange_waves():
    fx = MockEffects(pool_health={"kvPoolTokens": 49152})
    draws = [5000] * 30  # 150k demand vs 39k budget80 — the gemma regime
    d = await pool_fit_width(fx, draws, max_workers=32)
    assert d.gate == "waved" and 1 < d.width < 30
    assert d.width == (49152 * 4 // 5) // 5000


@pytest.mark.asyncio
async def test_pool_fit_width_handles_bare_double_and_empty():
    class _Bare:
        pass

    d = await pool_fit_width(_Bare(), [1000], pool_budget_fallback=8192)
    assert d.budget_source == "fallback" and d.width == 1
    d2 = await pool_fit_width(_Bare(), [], pool_budget_fallback=8192)
    assert d2.width == 0 and d2.gate == "full"


def test_estimate_draw_and_perf_fields():
    assert estimate_draw("x" * 40, gen_margin=100) == 13 + 100
    gd = GateDecision(4, "full", 131072, "server")
    assert gd.perf_fields() == {
        "sem": 4,
        "gate": "full",
        "pool_budget": 131072,
        "budget_source": "server",
    }
