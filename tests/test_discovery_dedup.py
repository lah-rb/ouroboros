"""Cross-identity dedup: the same paper reaching us twice under two names.

Identifier dedup is already exact — measured on a 5,556-paper corpus, zero DOIs
and zero OpenAlex ids appear on more than one paper_key. What it cannot catch is
ONE WORK arriving under two identities: a Research Square preprint beside its
journal DOI, an S2 record with no DOI beside the DOI record for the same paper,
the same article in a Spanish and an English venue. Measured on that corpus: 55
title groups holding 58 surplus records, and the surplus grows once multilingual
discovery is switched on.

Flagged, never merged. A wrong merge destroys a record irreversibly; a wrong
flag costs one paper's acquisition and stays visible.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.scholarly_actions import (
    _title_fingerprint,
    _title_index,
    action_merge_candidates,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

_LONG = "laser induced breakdown spectroscopy for remote elemental analysis"


def _si(candidates, effects):
    return StepInput(
        context={"raw_candidates": candidates},
        inputs={},
        params={"aspect_name": "libs"},
        meta=FlowMeta(flow_name="discover", step_id="t"),
        effects=effects,
    )


def _bank(*records):
    return MockEffects(
        files={
            "databank/papers.jsonl": "\n".join(json.dumps(r) for r in records) + "\n"
        }
    )


# ── fingerprint ───────────────────────────────────────────────────────


def test_short_titles_never_fingerprint():
    """'Editorial' and 'Raman spectroscopy' recur across unrelated papers;
    collapsing on one would suppress real work."""
    for short in ("Editorial", "Introduction", "Raman spectroscopy", "", None):
        assert _title_fingerprint(short) == ""


def test_fingerprint_survives_punctuation_and_case():
    """The same paper is punctuated differently by different sources."""
    a = _title_fingerprint(f"{_LONG}: a review — part I")
    b = _title_fingerprint(f"{_LONG.upper()} A REVIEW - PART I")
    assert a and a == b


def test_index_prefers_the_copy_worth_keeping():
    """A later duplicate should point at the copy we can actually use."""
    bank = {
        "s2_abc": {"title": _LONG},
        "doi_x": {"title": _LONG, "doi": "10.1/x"},
        "doi_y": {"title": _LONG, "doi": "10.1/y", "pdf_path": "pdfs/y.pdf"},
    }
    assert _title_index(bank)[_title_fingerprint(_LONG)] == "doi_y"


# ── merge behaviour ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_same_title_different_doi_is_flagged_not_dropped():
    """The live case: one article on two DOIs, a Spanish and an English venue.
    The record is KEPT — flagging is reversible, deletion is not."""
    fx = _bank(
        {
            "paper_key": "doi_10.1016_j.ejfs.2015.06.001",
            "title": _LONG,
            "doi": "10.1016/j.ejfs.2015.06.001",
            "status": "candidate",
            "access_status": "oa_pdf",
        }
    )
    cand = {
        "paper_key": "doi_10.18359_rfcb.2030",
        "title": _LONG.title(),
        "doi": "10.18359/rfcb.2030",
        "status": "candidate",
        "source_aspects": ["libs"],
    }
    out = await action_merge_candidates(_si([cand], fx))
    assert out.result["duplicates_flagged"] == 1

    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert "doi_10.18359_rfcb.2030" in bank, "the record must survive"
    assert bank["doi_10.18359_rfcb.2030"]["duplicate_of"] == (
        "doi_10.1016_j.ejfs.2015.06.001"
    )


@pytest.mark.asyncio
async def test_a_genuinely_new_paper_is_not_flagged():
    fx = _bank({"paper_key": "doi_a", "title": _LONG, "doi": "10.1/a"})
    cand = {
        "paper_key": "doi_b",
        "title": "quantitative analysis of renaissance majolica glaze chemistry",
        "doi": "10.1/b",
        "status": "candidate",
    }
    out = await action_merge_candidates(_si([cand], fx))
    assert out.result["duplicates_flagged"] == 0
    from agent.actions.scholarly_actions import read_databank

    assert "duplicate_of" not in (await read_databank(fx))["doi_b"]


@pytest.mark.asyncio
async def test_short_titled_papers_are_never_collapsed():
    fx = _bank({"paper_key": "doi_a", "title": "Editorial", "doi": "10.1/a"})
    cand = {
        "paper_key": "doi_b",
        "title": "Editorial",
        "doi": "10.1/b",
        "status": "candidate",
    }
    out = await action_merge_candidates(_si([cand], fx))
    assert out.result["duplicates_flagged"] == 0


@pytest.mark.asyncio
async def test_flagged_duplicates_are_never_dispatched_for_acquisition():
    """The flag has to suppress SPENDING — fetch, OCR, translation — or it is
    decoration. Exercised through the real sweep, not a re-implementation of
    its filter."""
    from agent.actions.research_plan_actions import action_catalog_sweep_next
    from agent.persistence.models import GoalRecord, MissionConfig, MissionState

    mission = MissionState(
        objective="corpus",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="scraper"),
        goals=[
            GoalRecord(description="acquire", type="extraction", status="incomplete")
        ],
    )
    fx = _bank(
        {"paper_key": "keep", "status": "candidate", "title": _LONG},
        {
            "paper_key": "dupe",
            "status": "candidate",
            "title": _LONG,
            "duplicate_of": "keep",
        },
    )
    fx._mission = mission
    si = StepInput(
        context={"mission": mission},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="research_control", step_id="t"),
        effects=fx,
    )
    out = await action_catalog_sweep_next(si)
    dispatched = (out.context_updates.get("dispatch_config") or {}).get(
        "paper_keys", []
    )
    assert "keep" in dispatched
    assert "dupe" not in dispatched


# ── page extent enrichment at acquisition ─────────────────────────────


@pytest.mark.asyncio
async def test_page_extent_is_enriched_for_papers_we_actually_hold():
    """WHY THIS IS IN THE FLOW: 41% of acquired papers carry no openalex_id —
    they came from S2 or CORE — so _normalize_openalex can never supply the
    page extent for them, however recent they are. Not a migration that
    shrinks to zero; a permanent gap for four papers in ten."""
    from agent.actions.scholarly_actions import action_enrich_paper_metadata

    held = {
        "paper_key": "doi_a",
        "doi": "10.1016/j.nimb.2013.05.098",
        "pdf_path": "pdfs/a.pdf",
    }
    from agent.effects.protocol import HttpResult

    fx = MockEffects(
        http_responses={
            "https://api.openalex.org/works": HttpResult(
                status=200,
                url="https://api.openalex.org/works",
                json_data={
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "doi": "https://doi.org/10.1016/j.nimb.2013.05.098",
                            "biblio": {"first_page": "37", "last_page": "41"},
                        }
                    ]
                },
            )
        }
    )
    si = StepInput(
        context={"catalog_batch": [held]},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="acquire", step_id="t"),
        effects=fx,
    )
    out = await action_enrich_paper_metadata(si)
    assert out.result["enriched"] == 1
    assert held["first_page"] == "37" and held["last_page"] == "41"


@pytest.mark.asyncio
async def test_page_extent_skips_papers_we_did_not_get():
    """A lookup for a paper with no PDF buys nothing — the truncation check
    only ever runs on documents we hold."""
    from agent.actions.scholarly_actions import action_enrich_paper_metadata

    si = StepInput(
        context={"catalog_batch": [{"paper_key": "k", "doi": "10.1/x"}]},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="acquire", step_id="t"),
        effects=MockEffects(),
    )
    out = await action_enrich_paper_metadata(si)
    assert out.result == {"enriched": 0, "looked_up": 0}


@pytest.mark.asyncio
async def test_metadata_enrichment_does_not_refetch_what_it_has():
    from agent.actions.scholarly_actions import action_enrich_paper_metadata

    rec = {
        "paper_key": "k",
        "doi": "10.1/x",
        "pdf_path": "pdfs/k.pdf",
        "first_page": "12",
        "last_page": "20",
        "license": "cc-by",
    }
    si = StepInput(
        context={"catalog_batch": [rec]},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="acquire", step_id="t"),
        effects=MockEffects(),
    )
    out = await action_enrich_paper_metadata(si)
    assert out.result["looked_up"] == 0


@pytest.mark.asyncio
async def test_a_page_extent_alone_does_not_satisfy_the_check():
    """`not (a or b and c)` parses as `not (a or (b and c))` and would skip
    every record that has an extent but no license — 47% of the live corpus
    is missing a license, and it is what makes the corpus filterable before
    any training use."""
    from agent.actions.scholarly_actions import action_enrich_paper_metadata
    from agent.effects.protocol import HttpResult

    rec = {
        "paper_key": "k",
        "doi": "10.1/x",
        "pdf_path": "pdfs/k.pdf",
        "first_page": "12",
        "last_page": "20",
    }
    fx = MockEffects(
        http_responses={
            "https://api.openalex.org/works": HttpResult(
                status=200,
                url="https://api.openalex.org/works",
                json_data={
                    "results": [
                        {
                            "id": "https://openalex.org/W1",
                            "doi": "https://doi.org/10.1/x",
                            "best_oa_location": {"license": "cc-by"},
                        }
                    ]
                },
            )
        }
    )
    si = StepInput(
        context={"catalog_batch": [rec]},
        inputs={},
        params={},
        meta=FlowMeta(flow_name="acquire", step_id="t"),
        effects=fx,
    )
    out = await action_enrich_paper_metadata(si)
    assert out.result["looked_up"] == 1
    assert rec["license"] == "cc-by"
    assert rec["first_page"] == "12", "must not clobber an extent it already had"
