"""Research gate: tier-based coverage, cross-links, grounding probes.

Doctrine pins: coverage satisfied by exact+close only (adjacent-only
aspects reported distinctly), crosslink edges computed corpus-internally
at gate time (self-references excluded), deterministic overlap check
before any judge turn, judge-unparseable -> UNGROUNDED (an unverifiable
tag must not silently enter the evidence base), and the verdict DERIVED
from probed facts in both directions.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.research_gate_actions import (
    action_apply_research_gate_results,
    action_check_aspect_coverage,
    action_finalize_crosslinks,
    action_prepare_tag_grounding,
    action_record_tag_grounding,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    AspectSpec,
    MissionConfig,
    MissionState,
    ResearchPlanState,
)


def _mission(aspects):
    return MissionState(
        objective="t",
        config=MissionConfig(working_directory="/tmp/x", flow_set="scraper"),
        research_plan=ResearchPlanState(abstract="a", aspects=aspects),
    )


def _si(effects, context=None) -> StepInput:
    return StepInput(
        context=context or {},
        params={},
        meta=FlowMeta(flow_name="research_gate", step_id="x"),
        effects=effects,
    )


def _bank(records):
    return {"databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"}


def _paper(key, tags, status="cataloged", **extra):
    return {"paper_key": key, "status": status, "tags": tags, **extra}


def _tag(aspect, relevance, justification="grain boundary segregation observed"):
    return {"aspect": aspect, "relevance": relevance, "justification": justification}


# ── coverage ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_coverage_counts_exact_close_only_and_flags_adjacent_only():
    fx = MockEffects(
        files=_bank(
            [
                _paper("p1", [_tag("gb", "exact")]),
                _paper("p2", [_tag("gb", "close")]),
                _paper("p3", [_tag("phase", "adjacent")]),
            ]
        )
    )
    m = _mission(
        [
            AspectSpec(name="gb", coverage_target=2),
            AspectSpec(name="phase", coverage_target=1),
        ]
    )
    out = await action_check_aspect_coverage(_si(fx, {"mission": m}))
    assert out.result["all_covered"] is False
    (issue,) = out.context_updates["coverage_report"]
    assert issue["aspect"] == "phase"
    assert issue["have"] == 0
    assert issue["adjacent_only"] is True


@pytest.mark.asyncio
async def test_coverage_passes_when_targets_met():
    fx = MockEffects(files=_bank([_paper("p1", [_tag("gb", "exact")])]))
    m = _mission([AspectSpec(name="gb", coverage_target=1)])
    out = await action_check_aspect_coverage(_si(fx, {"mission": m}))
    assert out.result["all_covered"] is True


# ── cross-links ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_crosslinks_intersect_corpus_and_exclude_self():
    fx = MockEffects(
        files=_bank(
            [
                _paper(
                    "doi_10.1_a",
                    [],
                    doi="10.1/a",
                    reference_dois=["10.1/b", "10.1/a", "10.9/external"],
                ),
                _paper("doi_10.1_b", [], doi="10.1/b", reference_dois=[]),
            ]
        )
    )
    out = await action_finalize_crosslinks(_si(fx))
    assert out.result["edge_count"] == 1
    links = json.loads(fx._files["databank/links.json"])
    assert links["edges"] == [{"from": "doi_10.1_a", "to": "doi_10.1_b"}]


# ── grounding probes ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_grounds_overlapping_justifications_deterministically():
    fx = MockEffects(
        files=_bank(
            [
                _paper(
                    "p1",
                    [
                        _tag("gb", "exact", "grain boundary segregation observed"),
                        _tag("gb", "close", "totally unrelated fabricated claim"),
                    ],
                    abstract="We observed grain boundary segregation in CoCrFeNi.",
                )
            ]
        )
    )
    m = _mission([AspectSpec(name="gb")])
    out = await action_prepare_tag_grounding(_si(fx, {"mission": m}))
    assert out.result["grounded"] == 1  # overlap check passed
    assert out.result["queued"] == 1  # fabricated one goes to the judge
    assert out.context_updates["probe_paper_key"] == "p1"


@pytest.mark.asyncio
async def test_record_unparseable_judge_marks_ungrounded():
    queue = [{"paper_key": "p1", "abstract": "a", "tags": [_tag("gb", "exact", "x")]}]
    out = await action_record_tag_grounding(
        _si(
            MockEffects(),
            {
                "grounding_queue": queue,
                "grounded_tags": [],
                "ungrounded_tags": [],
                "inference_response": "the vibes are fine",
            },
        )
    )
    assert out.result["has_next"] is False
    (bad,) = out.context_updates["ungrounded_tags"]
    assert bad["paper_key"] == "p1"


@pytest.mark.asyncio
async def test_record_judge_can_clear_or_name_specific_aspects():
    queue = [
        {
            "paper_key": "p1",
            "abstract": "a",
            "tags": [_tag("gb", "exact", "x"), _tag("phase", "close", "y")],
        }
    ]
    response = json.dumps(
        {"grounded": False, "ungrounded_aspects": ["phase"], "reason": "tier inflated"}
    )
    out = await action_record_tag_grounding(
        _si(
            MockEffects(),
            {
                "grounding_queue": queue,
                "grounded_tags": [],
                "ungrounded_tags": [],
                "inference_response": f"```json\n{response}\n```",
            },
        )
    )
    grounded = out.context_updates["grounded_tags"]
    ungrounded = out.context_updates["ungrounded_tags"]
    assert [t["aspect"] for t in grounded] == ["gb"]
    assert [t["aspect"] for t in ungrounded] == ["phase"]


# ── derived verdict ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_verdict_derived_both_directions():
    fx = MockEffects(files=_bank([_paper("p1", [_tag("gb", "exact")])]))
    m = _mission([AspectSpec(name="gb", coverage_target=1)])

    clean = await action_apply_research_gate_results(
        _si(fx, {"mission": m, "coverage_report": [], "ungrounded_tags": []})
    )
    assert clean.result["all_passing"] is True
    assert clean.context_updates["gate_results"]["verdict"] == "pass"

    dirty = await action_apply_research_gate_results(
        _si(
            fx,
            {
                "mission": m,
                "coverage_report": [
                    {"class": "coverage", "aspect": "gb", "have": 0, "want": 1}
                ],
                "ungrounded_tags": [
                    {"paper_key": "p1", "aspect": "gb", "relevance": "exact"}
                ],
            },
        )
    )
    assert dirty.result["all_passing"] is False
    gr = dirty.context_updates["gate_results"]
    assert gr["verdict"] == "fail"
    assert {i["class"] for i in gr["blocking_issues"]} == {"coverage", "grounding"}
    # Telemetry notes pushed per issue.
    assert fx.call_count("push_note") == 2
