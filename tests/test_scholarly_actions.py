"""Scholarly actions: search normalization, dedup, OA chain, databank.

Canned-HTTP tests for the scraper's I/O layer. Pins: S2/OpenAlex
normalization (including OpenAlex's inverted abstract), DOI-keyed dedup
with arXiv/S2 fallbacks, the OA resolution chain (known url → Unpaywall
→ closed, where closed papers PROCEED — access state, not failure),
download status transitions (fetch failure → oa_unresolved, still
cataloged), tag validation against the plan + relevance enum, and the
databank's last-record-wins read.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.scholarly_actions import (
    _deinvert_abstract,
    action_apply_paper_tags,
    action_download_papers,
    action_merge_candidates,
    action_resolve_oa_pdf,
    action_scholarly_search,
    paper_key,
    read_databank,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import DownloadResult, HttpResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    AspectSpec,
    MissionConfig,
    MissionState,
    ResearchPlanState,
)

_S2_HIT = {
    "paperId": "s2abc",
    "title": "GB segregation in CoCrFeNi",
    "abstract": "We study grain boundary segregation in a high entropy alloy.",
    "year": 2024,
    "venue": "Acta Mat",
    "authors": [{"name": "A. Smith"}],
    "externalIds": {"DOI": "10.1000/hea.1"},
    "openAccessPdf": {"url": "https://oa.org/hea1.pdf"},
}

_OPENALEX_HIT = {
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.1000/hea.1",
    "title": "GB segregation in CoCrFeNi",
    "abstract_inverted_index": {"Grain": [0], "boundary": [1], "study": [2]},
    "publication_year": 2024,
    "primary_location": {"source": {"display_name": "Acta Mat"}},
    "authorships": [{"author": {"display_name": "A. Smith"}}],
    "best_oa_location": {"pdf_url": "", "license": "cc-by"},
    "ids": {"openalex": "W1"},
    "language": "en",
}


def _si(effects, context=None, params=None) -> StepInput:
    return StepInput(
        context=context or {},
        params=params or {},
        meta=FlowMeta(flow_name="discover", step_id="x"),
        effects=effects,
    )


def _http(s2_hits=None, oa_hits=None):
    return {
        "https://api.semanticscholar.org/graph/v1/paper/search": HttpResult(
            status=200, url="s2", json_data={"data": s2_hits or []}
        ),
        "https://api.openalex.org/works": HttpResult(
            status=200, url="oa", json_data={"results": oa_hits or []}
        ),
    }


def test_deinvert_abstract():
    assert _deinvert_abstract({"world": [1], "hello": [0]}) == "hello world"
    assert _deinvert_abstract(None) == ""


def test_paper_key_fallback_chain():
    assert paper_key({"doi": "10.1000/A.B"}) == "doi_10.1000_a.b"
    assert paper_key({"arxiv_id": "2401.01234"}) == "arxiv_2401.01234"
    assert paper_key({"s2_id": "abc"}) == "s2_abc"
    assert paper_key({"title": "Some Title!"}).startswith("title_some_title")


@pytest.mark.asyncio
async def test_search_normalizes_both_apis():
    fx = MockEffects(http_responses=_http([_S2_HIT], [_OPENALEX_HIT]))
    out = await action_scholarly_search(
        _si(fx, context={"search_queries": ["hea gb"]}, params={"aspect_name": "gb"})
    )
    assert out.result == {"results_found": 2, "s2_count": 1, "openalex_count": 1}
    recs = out.context_updates["raw_candidates"]
    assert recs[0]["doi"] == "10.1000/hea.1"
    assert recs[0]["source_aspects"] == ["gb"]
    assert recs[1]["abstract"] == "Grain boundary study"  # de-inverted
    assert recs[0]["paper_key"] == recs[1]["paper_key"]  # same DOI
    assert recs[1]["language"] == "en"
    assert recs[1]["license"] == "cc-by"
    # S2 records carry the fields too (empty — S2 doesn't provide them).
    assert recs[0]["language"] == "" and recs[0]["license"] == ""


@pytest.mark.asyncio
async def test_search_api_failure_is_fail_soft():
    fx = MockEffects()  # no canned responses -> status 0 errors
    out = await action_scholarly_search(
        _si(fx, context={"search_queries": ["q"]}, params={"aspect_name": "a"})
    )
    assert out.result["results_found"] == 0


@pytest.mark.asyncio
async def test_merge_dedups_within_batch_and_against_databank():
    fx = MockEffects(http_responses=_http([_S2_HIT], [_OPENALEX_HIT]))
    search = await action_scholarly_search(
        _si(fx, context={"search_queries": ["q"]}, params={"aspect_name": "gb"})
    )
    out = await action_merge_candidates(
        _si(
            fx,
            context={"raw_candidates": search.context_updates["raw_candidates"]},
            params={"aspect_name": "gb"},
        )
    )
    # Two raw hits, one DOI -> one new candidate.
    assert out.result["new_candidates"] == 1
    assert out.result["aspect_total"] == 1
    # Re-merge under a second aspect: existing record gains the aspect.
    out2 = await action_merge_candidates(
        _si(
            fx,
            context={"raw_candidates": search.context_updates["raw_candidates"]},
            params={"aspect_name": "processing"},
        )
    )
    assert out2.result["new_candidates"] == 0
    assert out2.result["merged_existing"] == 1
    bank = await read_databank(fx)
    (rec,) = bank.values()
    assert sorted(rec["source_aspects"]) == ["gb", "processing"]
    assert out.context_updates["directive_report"]["flow"] == "discover"


@pytest.mark.asyncio
async def test_resolve_oa_chain_and_closed_is_not_failure():
    fx = MockEffects(
        http_responses={
            "https://api.unpaywall.org/v2/10.2/up": HttpResult(
                status=200,
                url="up",
                json_data={
                    "best_oa_location": {
                        "url_for_pdf": "https://up.org/p.pdf",
                        "license": "cc-by-nc",
                    }
                },
            ),
            "https://api.unpaywall.org/v2/10.3/closed": HttpResult(
                status=200, url="up", json_data={"best_oa_location": None}
            ),
        }
    )
    batch = [
        {"paper_key": "a", "oa_pdf_url": "https://oa.org/a.pdf", "doi": "10.1/a"},
        {"paper_key": "b", "oa_pdf_url": "", "doi": "10.2/up"},
        {"paper_key": "c", "oa_pdf_url": "", "doi": "10.3/closed"},
    ]
    out = await action_resolve_oa_pdf(_si(fx, context={"catalog_batch": batch}))
    assert out.result == {"resolved": 2, "closed": 1}
    statuses = {r["paper_key"]: r["access_status"] for r in batch}
    assert statuses == {"a": "oa_pdf", "b": "oa_pdf", "c": "closed"}
    assert batch[1]["oa_pdf_url"] == "https://up.org/p.pdf"
    assert batch[1]["license"] == "cc-by-nc"  # Unpaywall license capture


@pytest.mark.asyncio
async def test_download_failure_downgrades_to_unresolved():
    fx = MockEffects(
        http_downloads={
            "https://x.org/bad.pdf": DownloadResult(
                success=False, url="https://x.org/bad.pdf", path="p", error="403"
            )
        }
    )
    batch = [
        {
            "paper_key": "good",
            "access_status": "oa_pdf",
            "oa_pdf_url": "https://x.org/ok.pdf",
            "pdf_path": "",
        },
        {
            "paper_key": "bad",
            "access_status": "oa_pdf",
            "oa_pdf_url": "https://x.org/bad.pdf",
            "pdf_path": "",
        },
        {
            "paper_key": "closed",
            "access_status": "closed",
            "oa_pdf_url": "",
            "pdf_path": "",
        },
    ]
    out = await action_download_papers(_si(fx, context={"catalog_batch": batch}))
    assert out.result == {"downloaded": 1, "failed": 1}
    assert batch[0]["status"] == "acquired" and batch[0]["pdf_path"] == "pdfs/good.pdf"
    assert batch[1]["access_status"] == "oa_unresolved"
    assert batch[2]["access_status"] == "closed"  # untouched, proceeds


def _mission_with_plan():
    return MissionState(
        objective="t",
        config=MissionConfig(working_directory="/tmp/x"),
        research_plan=ResearchPlanState(
            aspects=[AspectSpec(name="gb"), AspectSpec(name="processing")]
        ),
    )


@pytest.mark.asyncio
async def test_apply_tags_validates_and_catalogs():
    fx = MockEffects()
    batch = [
        {"paper_key": "p1", "status": "acquired", "abstract": "a", "tags": []},
        {"paper_key": "p2", "status": "candidate", "abstract": "b", "tags": []},
    ]
    tags = {
        "p1": [
            {"aspect": "gb", "relevance": "exact", "justification": "grain boundary"},
            {"aspect": "nonsense", "relevance": "exact", "justification": "x"},
            {"aspect": "processing", "relevance": "kinda", "justification": "x"},
        ]
        # p2 omitted by the model -> stays un-cataloged for a retry batch
    }
    out = await action_apply_paper_tags(
        _si(
            fx,
            context={
                "catalog_batch": batch,
                "mission": _mission_with_plan(),
                "inference_response": f"```json\n{json.dumps(tags)}\n```",
            },
        )
    )
    assert out.result["cataloged"] == 1
    assert out.result["dropped_tags"] == 2  # bad aspect + bad relevance
    assert batch[0]["status"] == "cataloged"
    assert batch[0]["tags"] == [
        {"aspect": "gb", "relevance": "exact", "justification": "grain boundary"}
    ]
    assert batch[1]["status"] == "candidate"
    # Both records persisted to the databank regardless.
    bank = await read_databank(fx)
    assert set(bank) == {"p1", "p2"}
    assert out.context_updates["directive_report"]["status"] == "partial"


@pytest.mark.asyncio
async def test_databank_last_record_wins():
    fx = MockEffects(
        files={
            "databank/papers.jsonl": "\n".join(
                [
                    json.dumps({"paper_key": "p1", "status": "candidate"}),
                    json.dumps({"paper_key": "p1", "status": "cataloged"}),
                    "not json",
                ]
            )
        }
    )
    bank = await read_databank(fx)
    assert bank["p1"]["status"] == "cataloged"
