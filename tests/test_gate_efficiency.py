"""Gate efficiency: deterministic finding pipeline + refuted-signature suppression.

Live failure (gpt-oss parallel run): 52 consecutive gate-fail rounds
sustained by 7 schema findings that the prose layer re-paraphrased each
round (2-3 signature variants per finding, defeating dedup) and that
probes re-refuted up to 8 times (refutations were write-only telemetry).
Two fixes: deterministic checker findings now flow into fix_tasks
directly with exact signatures, bypassing the prose layer; and a claim
refuted twice is suppressed at harvest.
"""

from __future__ import annotations

import pytest

from agent.actions.mission_actions import (
    _quality_finding_signature,
    action_harvest_quality_findings,
)
from agent.actions.refinement_actions import action_apply_quality_gate_results
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    GoalRecord,
    MissionConfig,
    MissionState,
    NoteRecord,
)

_SHAPE_RESULTS = {
    "issues": [
        {
            "kind": "undeclared_key",
            "path": "config.yaml.items[0]",
            "detail": "key 'usable_on' is not in the declared example",
        }
    ],
    "files_checked": 1,
}

_SUMMARY_WITH_PARAPHRASE = """```json
{"verdict": "fail", "summary": "issues found", "blocking_issues": [
  {"description": "config.yaml item entry has undeclared key 'usable_on' not in the declared example", "class": "functional", "repro": []},
  {"description": "saving overwrites the backup without confirmation", "class": "functional", "repro": ["save", "save"]}
]}
```"""


def _si(context, inputs=None, effects=None) -> StepInput:
    return StepInput(
        context=context,
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="quality_gate", step_id="t"),
        effects=effects,
    )


# ── deterministic pipeline ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_shape_findings_merge_with_exact_signature_and_paraphrase_drops():
    out = await action_apply_quality_gate_results(
        _si(
            {
                "inference_response": _SUMMARY_WITH_PARAPHRASE,
                "data_shape_results": _SHAPE_RESULTS,
            }
        )
    )
    tasks = out.context_updates["quality_results"]["fix_tasks"]
    descs = [t["description"] for t in tasks]
    # The LLM paraphrase of the checker finding is gone…
    assert not any("item entry has undeclared" in d for d in descs)
    # …the behavioral finding survives…
    assert any("backup without confirmation" in d for d in descs)
    # …and the deterministic original is present with an exact signature.
    det = next(t for t in tasks if t.get("signature"))
    assert det["signature"] == ("shape|undeclared_key|config.yaml.items[0]|usable_on")
    assert _quality_finding_signature(det) == det["signature"]


@pytest.mark.asyncio
async def test_shape_findings_force_fail_even_on_llm_pass():
    raw = '```json\n{"verdict": "pass", "summary": "clean", "blocking_issues": []}\n```'
    out = await action_apply_quality_gate_results(
        _si({"inference_response": raw, "data_shape_results": _SHAPE_RESULTS})
    )
    assert out.result["all_passing"] is False
    assert out.result["has_findings"] is True


@pytest.mark.asyncio
async def test_no_shape_results_changes_nothing():
    raw = '```json\n{"verdict": "pass", "summary": "clean", "blocking_issues": []}\n```'
    out = await action_apply_quality_gate_results(_si({"inference_response": raw}))
    assert out.result["all_passing"] is True
    assert out.context_updates["quality_results"]["fix_tasks"] == []


# ── refuted-signature suppression ─────────────────────────────────────


def _refuted_note(claim: str) -> NoteRecord:
    return NoteRecord(
        content=f"Quality gate claimed: {claim} — probe REFUTED it: behavior worked",
        category="failure_analysis",
        source_flow="quality_gate",
    )


def _mission(notes) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=[
            GoalRecord(description="d", type="structural", status="complete"),
        ],
        notes=notes,
    )


@pytest.mark.asyncio
async def test_twice_refuted_claim_is_suppressed():
    claim = "frobnicator panel shows undeclared widget 'gizmo_dial'"
    mission = _mission([_refuted_note(claim), _refuted_note(claim)])
    fx = MockEffects(mission=mission)
    out = await action_harvest_quality_findings(
        _si(
            {
                "mission": mission,
                "quality_results": {
                    "all_passing": False,
                    "fix_tasks": [{"description": claim, "class": "functional"}],
                },
            },
            effects=fx,
        )
    )
    # The only finding was suppressed noise — gate finalizes instead of looping.
    assert out.result.get("done") is True
    assert not any(g.origin == "quality_gate" for g in mission.goals)


@pytest.mark.asyncio
async def test_once_refuted_claim_still_harvests():
    claim = "frobnicator panel shows undeclared widget 'gizmo_dial'"
    mission = _mission([_refuted_note(claim)])
    fx = MockEffects(mission=mission)
    out = await action_harvest_quality_findings(
        _si(
            {
                "mission": mission,
                "quality_results": {
                    "all_passing": False,
                    "fix_tasks": [{"description": claim, "class": "functional"}],
                },
            },
            effects=fx,
        )
    )
    assert out.result.get("harvested") is True
    assert any(g.origin == "quality_gate" for g in mission.goals)


@pytest.mark.asyncio
async def test_suppression_spares_other_findings():
    noisy = "frobnicator panel shows undeclared widget 'gizmo_dial'"
    real = "export command writes an empty file when the list has one entry"
    mission = _mission([_refuted_note(noisy), _refuted_note(noisy)])
    fx = MockEffects(mission=mission)
    out = await action_harvest_quality_findings(
        _si(
            {
                "mission": mission,
                "quality_results": {
                    "all_passing": False,
                    "fix_tasks": [
                        {"description": noisy, "class": "functional"},
                        {
                            "description": real,
                            "class": "functional",
                            "repro": ["export"],
                        },
                    ],
                },
            },
            effects=fx,
        )
    )
    assert out.result.get("harvested") is True
    qg = [g for g in mission.goals if g.origin == "quality_gate"]
    assert len(qg) == 1 and "export command" in qg[0].description


# ── prompt levers present ─────────────────────────────────────────────


def test_prompt_levers_in_place():
    ev = open("prompts/interact/evaluate_rules.yaml").read()
    assert "it is NOT okay" in ev
    cs = open("prompts/interact/charter_specification.yaml").read()
    assert "round-trip" in cs and "CHANGE something" in cs
    sm = open("prompts/quality_gate/summarize.yaml").read()
    assert "AUTOMATICALLY" in sm and "VERBATIM" not in sm
