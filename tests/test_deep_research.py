"""Deep-research sweep v2 — select panel, per-hit extraction, adversarial verify.

Pins: decompose gates on web_research and seeds the CANDIDATE pool
(degrading to the brief on a parse miss); select spends proposer + panel
completions to pick the search budget's worth of queries (approval voting,
graceful first-B degrade); the wave dispatches only the selected searches
and fans out one extract completion PER HIT (single-source attribution,
transient raw-hit publishing); verify runs one grounded skeptic per fresh
finding (three-way verdict, vacuous-skip, contradicted excluded from
synthesis, raw hits cleared); merge_reflect feeds the next candidate pool;
synthesize weights verdicts. Plus unit pins for the shared fanout helper.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.deep_research_actions import (
    action_research_decompose,
    action_research_merge_reflect,
    action_research_select,
    action_research_synthesize,
    action_research_verify,
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
async def test_decompose_seeds_candidate_pool():
    fx = MockEffects(
        mission=_mission(),
        inference_responses=['```json\n{"angles": ["q1", "q2", "q3"]}\n```'],
    )
    out = await action_research_decompose(_si(fx, inputs={"brief": "the brief"}))
    assert out.result["research_started"] is True
    assert out.context_updates["research_candidates"] == ["q1", "q2", "q3"]
    assert out.context_updates["research_angles"] == []
    assert out.context_updates["research_ledger"] == []


@pytest.mark.asyncio
async def test_decompose_degrades_to_brief_on_parse_miss():
    fx = MockEffects(mission=_mission(), inference_responses=["not json at all"])
    out = await action_research_decompose(_si(fx, inputs={"brief": "solve X"}))
    assert out.context_updates["research_candidates"] == ["solve X"]


# ── select (proposers + panel) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_select_panel_votes_within_budget():
    # 4 carried candidates, budget 2; proposers add nothing; all three
    # voters approve #1 and #3 → those two selected.
    fx = MockEffects(
        inference_responses=[
            '```json\n{"queries": []}\n```',
            '```json\n{"queries": []}\n```',
            '```json\n{"queries": []}\n```',
            '```json\n{"picks": [1, 3]}\n```',
            '```json\n{"picks": [1, 3]}\n```',
            '```json\n{"picks": [3, 1]}\n```',
        ]
    )
    out = await action_research_select(
        _si(
            fx,
            params={"search_budget": 2},
            research_brief="b",
            research_candidates=["c1", "c2", "c3", "c4"],
        )
    )
    assert out.result["n_selected"] == 2
    assert sorted(out.context_updates["research_angles"]) == ["c1", "c3"]
    assert out.context_updates["research_candidates"] == []


@pytest.mark.asyncio
async def test_select_proposers_fill_empty_pool_and_dedup():
    # No carried candidates; proposers supply queries (one repeats an
    # already-run search — deduped); under budget → no panel needed.
    fx = MockEffects(
        inference_responses=[
            '```json\n{"queries": ["fresh one", "already ran"]}\n```',
            '```json\n{"queries": ["fresh two"]}\n```',
            '```json\n{"queries": []}\n```',
        ]
    )
    out = await action_research_select(
        _si(
            fx,
            params={"search_budget": 4},
            research_brief="b",
            research_candidates=[],
            research_queries_run=["already ran"],
        )
    )
    assert sorted(out.context_updates["research_angles"]) == [
        "fresh one",
        "fresh two",
    ]


@pytest.mark.asyncio
async def test_select_degrades_to_first_budget_candidates():
    fx = MockEffects(inference_responses=["junk"] * 5)  # every call unparseable
    out = await action_research_select(
        _si(
            fx,
            params={"search_budget": 2},
            research_brief="b",
            research_candidates=["c1", "c2", "c3"],
        )
    )
    assert out.context_updates["research_angles"] == ["c1", "c2"]


# ── wave (per-hit extraction) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_wave_extracts_per_hit_and_appends_ledger(tmp_path):
    # 2 angles x 2 hits = 4 extract completions; one returns INSUFFICIENT.
    fx = MockEffects(
        mission=_mission(),
        inference_responses=[
            "Fact A (https://a.example).",
            "INSUFFICIENT",
            "Fact B (https://b.example).",
            "Fact C (https://a.example).",
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
    assert out.result["n_findings"] == 4
    ledger = out.context_updates["research_ledger"]
    assert len(ledger) == 4
    assert {f["url"] for f in ledger} == {"https://a.example", "https://b.example"}
    assert sum(1 for f in ledger if f["ok"]) == 3
    assert out.context_updates["research_wave_n"] == 1
    assert out.context_updates["research_queries_run"] == ["q1", "q2"]
    # raw hit text published transiently for the verify pass (ok findings)
    assert len(out.context_updates["research_wave_hits"]) == 3
    n_inference = sum(1 for c in fx.calls if c.method == "run_inference")
    assert n_inference == 4  # one per hit, not per query


@pytest.mark.asyncio
async def test_wave_records_server_budget_in_perf_sidecar(tmp_path):
    fx = MockEffects(
        mission=_mission(),
        inference_responses=["Fact (https://a.example)."] * 4,
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
    assert out.result["n_findings"] == 4
    rows = [
        json.loads(line) for line in (tmp_path / ".agent" / "swarm_perf.jsonl").open()
    ]
    burst = next(r for r in rows if r["event"] == "burst")
    assert burst["budget_source"] == "server"
    assert burst["pool_budget"] == 4096
    # 4 x _EXTRACT_DRAW_EST (1229) > budget80 (3276) → waved to 3276//1229 = 2
    assert burst["gate"] == "waved" and burst["sem"] == 2
    assert burst["hits"] == 4
    assert sum(1 for r in rows if r["event"] == "worker") == 4


@pytest.mark.asyncio
async def test_wave_degrades_when_search_unavailable():
    class _NoMcp(MockEffects):
        async def mcp_connect(self, server_name, server_command=None):
            raise FileNotFoundError("~/.exa_key missing")

    fx = _NoMcp(mission=_mission())
    out = await action_research_wave(_si(fx, research_angles=["q1"]))
    assert out.result["wave_ok"] is False
    assert out.context_updates["research_wave_n"] == 1
    assert out.context_updates["research_wave_hits"] == {}


# ── verify (the adversarial pass) ─────────────────────────────────────


def _finding(query, url, fact, ok=True, wave=1, verdict=""):
    return {
        "query": query,
        "url": url,
        "fact": fact,
        "ok": ok,
        "wave": wave,
        "verdict": verdict,
    }


@pytest.mark.asyncio
async def test_verify_three_way_verdicts_update_ledger(tmp_path):
    ledger = [
        _finding("q1", "https://a.example", "claim A"),
        _finding("q1", "https://b.example", "claim B"),
        _finding("q2", "https://a.example", "claim C"),
    ]
    hits = {
        "q1\nhttps://a.example": "text supporting claim A",
        "q1\nhttps://b.example": "unrelated text",
        "q2\nhttps://a.example": "text saying the opposite of claim C",
    }
    fx = MockEffects(
        inference_responses=[
            '```json\n{"verdict": "supported", "note": "quote"}\n```',
            '```json\n{"verdict": "unsupported", "note": "not stated"}\n```',
            '```json\n{"verdict": "contradicted", "note": "opposite"}\n```',
        ]
    )
    out = await action_research_verify(
        _si(
            fx,
            inputs={"working_directory": str(tmp_path)},
            research_ledger=ledger,
            research_wave_n=1,
            research_wave_hits=hits,
        )
    )
    assert out.result["n_verified"] == 3
    assert out.result["n_contradicted"] == 1
    verdicts = {f["url"]: f["verdict"] for f in ledger if f["query"] == "q1"}
    assert set(f["verdict"] for f in ledger) == {
        "supported",
        "unsupported",
        "contradicted",
    }
    assert verdicts  # per-entry verdicts landed in place
    assert out.context_updates["research_wave_hits"] == {}  # raw text dropped


@pytest.mark.asyncio
async def test_verify_vacuous_skip_and_prior_wave_untouched():
    ledger = [
        _finding("q1", "", "INSUFFICIENT (no usable hits)", ok=False),
        _finding("q0", "https://a.example", "old claim", wave=0, verdict="supported"),
    ]
    fx = MockEffects(inference_responses=['{"verdict": "contradicted"}'])
    out = await action_research_verify(
        _si(
            fx,
            research_ledger=ledger,
            research_wave_n=1,
            research_wave_hits={"q1\n": "irrelevant"},
        )
    )
    # nothing checkable this wave: no inference spent, nothing relabeled
    assert out.result["n_verified"] == 0
    assert ledger[0]["verdict"] == ""
    assert ledger[1]["verdict"] == "supported"
    assert sum(1 for c in fx.calls if c.method == "run_inference") == 0


# ── merge_reflect / synthesize ────────────────────────────────────────


@pytest.mark.asyncio
async def test_merge_reflect_feeds_next_candidate_pool():
    fx = MockEffects(
        inference_responses=[
            '```json\n{"sufficient": false, "gaps": ["g1", "g2"]}\n```'
        ]
    )
    out = await action_research_merge_reflect(
        _si(
            fx,
            research_brief="b",
            research_ledger=[_finding("q1", "https://a.example", "f1")],
        )
    )
    assert out.result["n_gaps"] == 2
    assert out.context_updates["research_candidates"] == ["g1", "g2"]
    assert out.context_updates["research_angles"] == []


@pytest.mark.asyncio
async def test_merge_reflect_sufficient_clears_gaps():
    fx = MockEffects(
        inference_responses=['```json\n{"sufficient": true, "gaps": ["stale"]}\n```']
    )
    out = await action_research_merge_reflect(
        _si(
            fx,
            research_brief="b",
            research_ledger=[_finding("q", "https://a.example", "f")],
        )
    )
    assert out.result["sufficient"] is True
    assert out.context_updates["research_candidates"] == []
    assert out.context_updates["research_sufficient"] is True


@pytest.mark.asyncio
async def test_synthesize_excludes_contradicted_findings():
    captured = {}

    class _Capture(MockEffects):
        async def run_inference(self, prompt, config_overrides=None, **kw):
            captured["prompt"] = prompt
            return await super().run_inference(prompt, config_overrides, **kw)

    fx = _Capture(
        inference_responses=[
            '```json\n{"research_summary": "S (https://a.example)", '
            '"sufficient": true}\n```'
        ]
    )
    out = await action_research_synthesize(
        _si(
            fx,
            research_brief="b",
            research_ledger=[
                _finding("q1", "https://a.example", "good claim", verdict="supported"),
                _finding(
                    "q2", "https://b.example", "bogus claim", verdict="contradicted"
                ),
                _finding(
                    "q3", "https://c.example", "shaky claim", verdict="unsupported"
                ),
            ],
            research_queries_run=["q1", "q2", "q3"],
        )
    )
    assert out.result["sufficient"] is True
    assert "bogus claim" not in captured["prompt"]
    assert "good claim" in captured["prompt"]
    assert "[UNVERIFIED" in captured["prompt"]  # shaky claim arrives flagged


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
