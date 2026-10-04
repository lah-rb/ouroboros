"""The research query count is ONE number, told and enforced.

The planner's guidance asked for "a couple" of queries while the extractor
silently kept 3; the model wrote five and the last two — one of them the
mission's boss-weakness question — were dropped with an observation that
said "Extracted 3", not "3 of 5". The count now lives in research.cue as a
single value, interpolated into the planner's instruction and passed to the
extractor, so the two cannot drift apart.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent.actions.refinement_actions import action_extract_search_queries
from agent.models import FlowMeta, StepInput

ROOT = Path(__file__).resolve().parents[1]


def _research() -> dict:
    return json.loads((ROOT / "flows" / "compiled.json").read_text())["research"]


def test_the_prompt_and_the_cap_state_the_same_number():
    steps = _research()["steps"]
    cap = steps["extract_queries"]["params"]["max_queries"]
    literals = [
        s["literal"]
        for s in steps["plan_queries"]["turn"]["sections"]
        if s.get("literal")
    ]
    told = {int(n) for lit in literals for n in re.findall(r"\b(\d+)\b", lit)}
    assert told == {cap}, (told, cap)
    assert str(cap) in steps["plan_queries"]["description"]


def test_the_number_is_defined_once_in_cue():
    src = (ROOT / "flows" / "shared" / "research.cue").read_text()
    assert re.search(r"^_research_max_queries: \d+$", src, re.M)
    assert "max_queries: _research_max_queries" in src
    assert "\\(_research_max_queries)" in src
    # and the guidance prose no longer carries its own count
    guidance = (
        ROOT / "prompts" / "research" / "plan_queries_guidance.yaml"
    ).read_text()
    assert "a couple" not in guidance


@pytest.mark.asyncio
async def test_the_extractor_keeps_the_first_n_in_order():
    cap = _research()["steps"]["extract_queries"]["params"]["max_queries"]
    queries = [f"q{i}" for i in range(cap + 2)]
    out = await action_extract_search_queries(
        StepInput(
            context={"inference_response": json.dumps(queries)},
            params={"max_queries": cap},
            meta=FlowMeta(flow_name="research", step_id="extract_queries"),
        )
    )
    assert out.context_updates["search_queries"] == queries[:cap]
