"""Scraper flow set end-to-end (mock): plan → discover → catalog → gate.

Drives research_control through the full pipeline with canned inference
and HTTP: one aspect (target 1), one paper found on both APIs (DOI
dedup), OA PDF downloaded, tagged exact with a justification that
quotes the abstract (so the gate's deterministic overlap check grounds
it without a judge turn), coverage met → derived PASS → mission
completed. Asserts the databank contents and final mission status —
the scraper analog of the runtime integration tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from agent.actions import scholarly_actions
from agent.actions.scholarly_actions import read_databank
from agent.effects.mock import MockEffects
from agent.effects.protocol import HttpResult
from agent.loop import run_agent
from agent.persistence.models import MissionConfig, MissionState

_ABSTRACT = "Survey grain boundary segregation in high entropy alloys."

_PLAN_JSON = json.dumps(
    {
        "aspects": [
            {
                "name": "gb segregation",
                "description": "Grain boundary segregation in HEAs",
                "seed_queries": ["grain boundary segregation high entropy alloy"],
                "coverage_target": 1,
            }
        ],
        "notes": "single-aspect smoke",
    }
)

_QUERIES_JSON = json.dumps(["gb segregation CoCrFeNi"])

_PAPER_ABSTRACT = (
    "We study grain boundary segregation in a CoCrFeNi high entropy alloy."
)

_TAGS_JSON = json.dumps(
    {
        "doi_10.1000_hea.1": [
            {
                "aspect": "gb segregation",
                "relevance": "exact",
                # Quotes the abstract -> deterministic overlap check
                # grounds it; the gate needs no judge turn.
                "justification": (
                    "we study grain boundary segregation in a CoCrFeNi "
                    "high entropy alloy"
                ),
            }
        ]
    }
)

_S2_HIT = {
    "paperId": "s2abc",
    "title": "GB segregation in CoCrFeNi",
    "abstract": _PAPER_ABSTRACT,
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
    "abstract_inverted_index": None,
    "publication_year": 2024,
    "host_venue": {"display_name": "Acta Mat"},
    "authorships": [],
    "best_oa_location": {"pdf_url": ""},
    "ids": {"openalex": "W1"},
}


def test_scraper_pipeline_end_to_end():
    mission = MissionState(
        objective=_ABSTRACT,
        config=MissionConfig(working_directory="/tmp/x", flow_set="scraper"),
    )
    fx = MockEffects(
        mission=mission,
        inference_responses=[
            f"```json\n{_PLAN_JSON}\n```",  # plan_research.design_plan
            f"```json\n{_QUERIES_JSON}\n```",  # discover.refine_queries
            f"```json\n{_TAGS_JSON}\n```",  # acquire_catalog.tag_papers
        ],
        http_responses={
            "https://api.semanticscholar.org/graph/v1/paper/search": HttpResult(
                status=200, url="s2", json_data={"data": [_S2_HIT]}
            ),
            "https://api.semanticscholar.org/graph/v1/paper/": HttpResult(
                status=200, url="refs", json_data={"data": []}
            ),
            "https://api.openalex.org/works": HttpResult(
                status=200, url="oa", json_data={"results": [_OPENALEX_HIT]}
            ),
        },
    )

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    async def _sleepless(_s):
        return None

    async def _run():
        with patch.object(scholarly_actions.asyncio, "sleep", _sleepless):
            return await run_agent(
                mission_id=mission.id,
                effects=fx,
                flows_dir=os.path.join(root, "flows"),
                prompts_dir=os.path.join(root, "prompts"),
                entry_flow="research_control",
                max_cycles=12,
            )

    asyncio.run(_run())

    saved = fx._state["mission"]
    assert saved.status == "completed"
    assert saved.research_plan is not None
    assert [g.status for g in saved.goals] == ["complete", "complete"]
    assert {g.type for g in saved.goals} == {"discovery", "extraction"}

    bank = asyncio.run(read_databank(fx))
    (rec,) = bank.values()
    assert rec["paper_key"] == "doi_10.1000_hea.1"
    assert rec["status"] == "cataloged"
    assert rec["access_status"] == "oa_pdf"
    assert rec["pdf_path"] == "pdfs/doi_10.1000_hea.1.pdf"
    assert rec["pdf_path"] in fx._files  # mock download wrote it
    assert rec["tags"][0]["relevance"] == "exact"
    # Cross-links file written by the gate (no corpus-internal edges here).
    assert json.loads(fx._files["databank/links.json"]) == {"edges": []}
