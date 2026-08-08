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
    pytest.skip(
        "RETIRED (operator, 2026-08-08): the deterministic shape merge was "
        "evicted from the gate with the exemplar diff — see OPEN_TASKS §23"
    )
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
    pytest.skip(
        "RETIRED (operator, 2026-08-08): the deterministic shape merge was "
        "evicted from the gate with the exemplar diff — see OPEN_TASKS §23"
    )
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


# ── Fix 3b: shape task keeps the real data file (issue["file"]) ───────


def test_shape_task_prefers_issue_file_over_path_regex():
    """A shape issue with an extensionless path (rooms[2].exits) must take its
    file from issue["file"] (stamped by validate_data_shapes), not the path
    regex (which yields "") — otherwise the harvested goal loses the data
    link. Signature is unchanged (no file component)."""
    from agent.actions.refinement_actions import _deterministic_shape_tasks

    results = {
        "issues": [
            {
                "kind": "undeclared_key",
                "path": "rooms[2].exits",
                "detail": (
                    "key 'west' is not in the declared example "
                    "(declared keys here: ['east', 'north'])"
                ),
                "file": "world/rooms.yaml",
            }
        ]
    }
    tasks = _deterministic_shape_tasks(results)
    assert len(tasks) == 1
    assert tasks[0]["file"] == "world/rooms.yaml"
    assert tasks[0]["signature"] == "shape|undeclared_key|rooms[2].exits|west"


# ── shape false-positive: behavior-refutation suppression ─────────────


def _shape_mission(goal: GoalRecord) -> MissionState:
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=[goal],
    )


def _shape_qg(sig: str, desc: str) -> dict:
    return {
        "all_passing": False,
        "fix_tasks": [
            {"description": desc, "class": "functional", "repro": [], "signature": sig}
        ],
    }


@pytest.mark.asyncio
async def test_shape_finding_suppressed_after_two_behavior_refutes():
    sig = "shape|undeclared_key|rooms[2].exits|west"
    desc = "rooms[2].exits: key 'west' is not in the declared example"
    goal = GoalRecord(
        description=desc,
        type="functional",
        status="complete",  # behavior passed → goal completed
        origin="quality_gate",
        finding_signature=sig,
        interaction_mode="exploratory",
    )
    m = _shape_mission(goal)
    fx = MockEffects(mission=m)

    # Round 1: shape-check re-flags → reopen (fix "didn't hold"), streak 1.
    out1 = await action_harvest_quality_findings(
        _si({"mission": m, "quality_results": _shape_qg(sig, desc)}, effects=fx)
    )
    assert goal.status == "incomplete"
    assert goal.shape_refutes == 1
    assert out1.result.get("done") is not True  # reopened, not finalized

    # Behavior passes again → goal completes.
    goal.status = "complete"

    # Round 2: second behavior-refute → suppressed, goal STAYS complete.
    out2 = await action_harvest_quality_findings(
        _si({"mission": m, "quality_results": _shape_qg(sig, desc)}, effects=fx)
    )
    assert goal.status == "complete"
    assert goal.shape_refutes == 2
    # Only finding, all suppressed → the loop-breaker finalizes.
    assert out2.result.get("done") is True


@pytest.mark.asyncio
async def test_behavioral_reopen_not_suppressed():
    # A completed goal with a NON-shape signature reopens normally — the
    # behavior-refute gate is shape-only.
    sig = "engine.py|_handle_move"
    goal = GoalRecord(
        description="movement between rooms is broken",
        type="functional",
        status="complete",
        origin="quality_gate",
        finding_signature=sig,
        interaction_mode="exploratory",
    )
    m = _shape_mission(goal)
    fx = MockEffects(mission=m)
    await action_harvest_quality_findings(
        _si(
            {"mission": m, "quality_results": _shape_qg(sig, goal.description)},
            effects=fx,
        )
    )
    assert goal.status == "incomplete"  # reopened as normal
    assert goal.shape_refutes == 0  # untouched (not a shape finding)


@pytest.mark.asyncio
async def test_in_flight_shape_goal_not_counted():
    # An INCOMPLETE shape goal being re-flagged is skipped (in flight); only
    # complete→reopen transitions count toward the refute streak.
    sig = "shape|undeclared_key|rooms[2].exits|west"
    desc = "rooms[2].exits: key 'west' is not in the declared example"
    goal = GoalRecord(
        description=desc,
        type="functional",
        status="incomplete",
        origin="quality_gate",
        finding_signature=sig,
        interaction_mode="exploratory",
    )
    m = _shape_mission(goal)
    fx = MockEffects(mission=m)
    await action_harvest_quality_findings(
        _si({"mission": m, "quality_results": _shape_qg(sig, desc)}, effects=fx)
    )
    assert goal.status == "incomplete"
    assert goal.shape_refutes == 0
