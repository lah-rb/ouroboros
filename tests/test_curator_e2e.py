"""Curator flow set end-to-end (MockEffects): two papers, gate, corpus.

Drives run_agent from curate_control to mission completion: one paper
is accepted and packed (grounded values), the other denied. Asserts the
databank outcomes, the corpus.json build, the goal states, and the
structural snapshot cleanup — the control loop proven without a model
or the sidecar.
"""

from __future__ import annotations

import asyncio
import json
import os

from agent.effects.mock import MockEffects
from agent.persistence.models import MissionConfig, MissionState

_MD_GOOD = "# Good\n\nYield strength was 759 MPa at 77 K; elongation 71%.\n"
_MD_BAD = "# Bad\n\nGarbled ex%%traction with no usable tables.\n"


def _rec(key):
    return {
        "paper_key": key,
        "title": f"Paper {key}",
        "doi": f"10.1/{key}",
        "license": "cc-by",
        "extraction_status": "extracted",
        "figure_count": 0,  # fig sweep completes immediately (no sidecar)
        "md_path": f"databank/markdown/{key}.md",
    }


def test_curator_end_to_end_mock():
    from agent.loop import run_agent

    mission = MissionState(
        objective="curate corpus",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="curator"),
    )
    # Sweep order is sorted: "bad" dispatches before "good".
    fx = MockEffects(
        files={
            "databank/papers.jsonl": json.dumps(_rec("bad"))
            + "\n"
            + json.dumps(_rec("good"))
            + "\n",
            "databank/markdown/bad.md": _MD_BAD,
            "databank/markdown/good.md": _MD_GOOD,
        },
        mission=mission,
        inference_responses=[
            # Paper "bad": review denies.
            '```json\n{"verdict": "deny", "summary": "garbled extraction",'
            ' "issues": ["tables unreadable"]}\n```',
            # Paper "good": review accepts, pack grounds cleanly.
            '```json\n{"verdict": "accept", "summary": "clean tensile data",'
            ' "issues": []}\n```',
            '```json\n{"yield_strength_mpa": 759, "elongation_pct": 71}\n```',
        ],
    )

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(
        run_agent(
            mission_id=mission.id,
            effects=fx,
            flows_dir=os.path.join(root, "flows"),
            prompts_dir=os.path.join(root, "prompts"),
            entry_flow="curate_control",
            max_cycles=20,
        )
    )

    saved = fx._state["mission"]
    assert saved.status == "completed"
    assert all(
        g.status == "complete"
        for g in saved.goals
        if g.type in ("fig_review", "curate")
    )

    bank = {}
    for line in fx._files["databank/papers.jsonl"].splitlines():
        r = json.loads(line)
        bank[r["paper_key"]] = r
    assert bank["bad"]["review_status"] == "denied"
    assert bank["bad"]["review_issues"] == ["tables unreadable"]
    assert "pack_status" not in bank["bad"] or bank["bad"]["pack_status"] == ""
    assert bank["good"]["review_status"] == "accepted"
    assert bank["good"]["pack_status"] == "packed"
    assert bank["good"]["pack_quality"]["grounding_rate"] == 1.0

    corpus = json.loads(fx._files["databank/dataset/corpus.json"])
    assert corpus["counts"] == {"packed": 1, "denied": 1, "failed": 0}
    assert corpus["papers"][0]["data"]["yield_strength_mpa"] == 759
    assert corpus["key_registry"]["yield_strength_mpa"]["count"] == 1

    # Structural cleanup: no leaked sessions, no leaked snapshots.
    assert not fx._mock_active_sessions
    assert not getattr(fx, "_mock_snapshots", {})

    # Archive sweep ran on completion: completed goals' reports RELOCATED
    # to append-only JSONL (never deleted), counters preserved on record.
    from agent.persistence.archive import iter_archive

    agent_dir = fx._get_persistence().agent_dir
    archived = list(iter_archive(agent_dir, kind="report"))
    assert archived, "completed goals' reports must land in the archive"
    total_counters = sum(g.reports_archived for g in saved.goals)
    assert total_counters == len(archived)
    assert all(not g.reports for g in saved.goals if g.status == "complete")
