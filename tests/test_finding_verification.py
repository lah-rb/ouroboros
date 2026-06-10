"""Verify-before-harvest probe loop: prepare / record / apply.

Gate findings are claims. The measured failure mode (~35% false claims on
the first completed run): "untested: save/load" findings whose features
worked — diagnose found no errors, file_ops blind-edited working files,
and the edits caused real regressions. These tests pin the loop's
contract: only claims that survive a probe reach the harvester, the
verdict is DERIVED from survivors (overriding the model's assertion in
both directions), and every infrastructure failure KEEPS the claim.
"""

from __future__ import annotations

import pytest

from agent.actions.verification_actions import (
    MAX_VERIFY_FINDINGS,
    action_apply_verification_results,
    action_prepare_finding_verification,
    action_record_finding_verification,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput

_RUN = "uv run python main.py"


def _task(issue, cls="functional", repro=None, expected=""):
    t = {"issue": issue, "description": issue, "class": cls}
    if repro is not None:
        t["repro"] = repro
    if expected:
        t["expected"] = expected
    return t


def _si(context=None, params=None) -> StepInput:
    return StepInput(
        context=context or {},
        params=params or {},
        meta=FlowMeta(flow_name="quality_gate", step_id="x"),
        effects=MockEffects(),
    )


def _prepare_si(*tasks, run_command=_RUN, policy="permissive", terminal_output="ux"):
    return _si(
        context={
            "quality_results": {"all_passing": False, "fix_tasks": list(tasks)},
            "terminal_output": terminal_output,
        },
        params={"run_command": run_command, "no_repro_policy": policy},
    )


# ── prepare ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prepare_partitions_functional_with_repro_vs_quality():
    out = await action_prepare_finding_verification(
        _prepare_si(
            _task("use broken", repro=["take x", "use x"], expected="x lights"),
            _task("grammar off", cls="quality"),
        )
    )
    assert out.result == {
        "has_next": True,
        "queued": 1,
        "passthrough": 1,
        "refuted_no_repro": 0,
    }
    cu = out.context_updates
    assert cu["passthrough_tasks"][0]["verification"] == "not-applicable"
    # Head probe keys: launch + repro, claim/expected for the judge.
    assert cu["probe_commands"] == [_RUN, "take x", "use x"]
    assert cu["probe_claim"] == "use broken"
    assert cu["probe_expected"] == "x lights"
    assert "1. take x" in cu["probe_repro_block"]
    # UX transcript snapshotted before probes clobber terminal_output.
    assert cu["gate_terminal_output"] == "ux"


@pytest.mark.asyncio
async def test_prepare_no_repro_permissive_passes_through_tagged():
    out = await action_prepare_finding_verification(
        _prepare_si(_task("vague claim"), policy="permissive")
    )
    assert out.result["has_next"] is False
    assert (
        out.context_updates["passthrough_tasks"][0]["verification"]
        == "unverified-no-repro"
    )


@pytest.mark.asyncio
async def test_prepare_no_repro_strict_refutes():
    out = await action_prepare_finding_verification(
        _prepare_si(_task("vague claim"), policy="strict")
    )
    assert out.result["refuted_no_repro"] == 1
    refuted = out.context_updates["refuted_findings"][0]
    assert refuted["verification"] == "refuted"
    assert "strict policy" in refuted["verification_evidence"]


@pytest.mark.asyncio
async def test_prepare_empty_run_command_passes_everything_through():
    out = await action_prepare_finding_verification(
        _prepare_si(_task("x", repro=["a"]), run_command="")
    )
    assert out.result["has_next"] is False
    assert out.result["passthrough"] == 1


@pytest.mark.asyncio
async def test_prepare_strips_leading_run_command_from_repro():
    out = await action_prepare_finding_verification(
        _prepare_si(_task("x", repro=[_RUN, "go north"]))
    )
    assert out.context_updates["probe_commands"] == [_RUN, "go north"]


@pytest.mark.asyncio
async def test_prepare_queue_cap_overflows_to_tagged_passthrough():
    tasks = [_task(f"bug {i}", repro=["poke"]) for i in range(MAX_VERIFY_FINDINGS + 2)]
    out = await action_prepare_finding_verification(_prepare_si(*tasks))
    assert out.result["queued"] == MAX_VERIFY_FINDINGS
    overflow = [
        t
        for t in out.context_updates["passthrough_tasks"]
        if t["verification"] == "unverified-cap"
    ]
    assert len(overflow) == 2


# ── record ────────────────────────────────────────────────────────────


def _record_si(queue, response="", probe_failed=False, transcript="t" * 600):
    params = {"run_command": _RUN}
    if probe_failed:
        params["probe_failed"] = True
    return _si(
        context={
            "verification_queue": queue,
            "verified_findings": [],
            "refuted_findings": [],
            "inference_response": response,
            "terminal_output": transcript,
        },
        params=params,
    )


@pytest.mark.asyncio
async def test_record_confirmed_keeps_claim_with_evidence():
    si = _record_si(
        [_task("use broken", repro=["use x"])],
        response='```json\n{"confirmed": true, "reason": "use printed an error"}\n```',
    )
    out = await action_record_finding_verification(si)
    assert out.result == {"has_next": False, "confirmed": True}
    kept = out.context_updates["verified_findings"][0]
    assert kept["verification"] == "confirmed"
    assert "use printed an error" in kept["verification_evidence"]
    # Evidence carries the transcript tail, bounded.
    assert "probe transcript tail" in kept["verification_evidence"]


@pytest.mark.asyncio
async def test_record_refuted_moves_claim_out_of_findings():
    si = _record_si(
        [_task("save broken", repro=["save f", "load f"])],
        response='```json\n{"confirmed": false, "reason": "save/load restored state"}\n```',
    )
    out = await action_record_finding_verification(si)
    assert out.result["confirmed"] is False
    assert out.context_updates["verified_findings"] == []
    assert out.context_updates["refuted_findings"][0]["verification"] == "refuted"


@pytest.mark.asyncio
async def test_record_malformed_judge_json_keeps_claim():
    si = _record_si([_task("x", repro=["a"])], response="the vibes seem fine")
    out = await action_record_finding_verification(si)
    assert out.result["confirmed"] is None
    assert (
        out.context_updates["verified_findings"][0]["verification"]
        == "judge-unparseable"
    )


@pytest.mark.asyncio
async def test_record_probe_failed_keeps_claim_inconclusive():
    si = _record_si([_task("x", repro=["a"])], probe_failed=True)
    out = await action_record_finding_verification(si)
    assert out.context_updates["verified_findings"][0]["verification"] == "inconclusive"


@pytest.mark.asyncio
async def test_record_advances_queue_and_publishes_next_probe_keys():
    si = _record_si(
        [_task("first", repro=["a"]), _task("second", repro=["b", "c"])],
        response='```json\n{"confirmed": false, "reason": "works"}\n```',
    )
    out = await action_record_finding_verification(si)
    assert out.result["has_next"] is True
    cu = out.context_updates
    assert len(cu["verification_queue"]) == 1
    assert cu["probe_claim"] == "second"
    assert cu["probe_commands"] == [_RUN, "b", "c"]


@pytest.mark.asyncio
async def test_record_empty_queue_is_noop():
    out = await action_record_finding_verification(_record_si([]))
    assert out.result == {"has_next": False, "confirmed": None}


# ── apply ─────────────────────────────────────────────────────────────


def _apply_si(verified=None, passthrough=None, refuted=None, summary="s"):
    return _si(
        context={
            "quality_results": {"all_passing": False, "summary": summary},
            "verified_findings": verified or [],
            "passthrough_tasks": passthrough or [],
            "refuted_findings": refuted or [],
            "gate_terminal_output": "ux transcript",
            "terminal_output": "last probe transcript",
        }
    )


@pytest.mark.asyncio
async def test_apply_derives_pass_when_all_claims_refuted():
    # Model said FAIL; every claim refuted; no passthrough -> derived PASS.
    si = _apply_si(refuted=[{**_task("x"), "verification": "refuted"}])
    out = await action_apply_verification_results(si)
    assert out.result["all_passing"] is True
    qr = out.context_updates["quality_results"]
    assert qr["all_passing"] is True
    assert qr["verdict"] == "pass"
    assert qr["fix_tasks"] == []


@pytest.mark.asyncio
async def test_apply_derives_fail_from_confirmed_survivor():
    # Even a model "pass" verdict cannot pass a confirmed defect.
    si = _apply_si(
        verified=[{**_task("use broken"), "verification": "confirmed"}],
    )
    si.context["quality_results"]["all_passing"] = True
    out = await action_apply_verification_results(si)
    assert out.result["all_passing"] is False
    qr = out.context_updates["quality_results"]
    assert qr["verdict"] == "fail"
    assert qr["issues"] == ["use broken"]


@pytest.mark.asyncio
async def test_apply_pushes_notes_for_survivors_and_refuted_telemetry():
    si = _apply_si(
        verified=[{**_task("use broken"), "verification": "confirmed"}],
        refuted=[
            {
                **_task("save broken"),
                "verification": "refuted",
                "verification_evidence": "save worked\n--- probe transcript tail ---\n...",
            }
        ],
    )
    out = await action_apply_verification_results(si)
    notes = si.effects.calls_to("push_note")
    assert len(notes) == 2
    assert out.result["refuted"] == 1
    # Refuted note is the false-claim telemetry instrument.
    refuted_notes = [n for n in notes if "refuted-finding" in n.args["tags"]]
    assert len(refuted_notes) == 1


@pytest.mark.asyncio
async def test_apply_restores_gate_terminal_output():
    out = await action_apply_verification_results(_apply_si())
    assert out.context_updates["terminal_output"] == "ux transcript"


@pytest.mark.asyncio
async def test_apply_summary_carries_verification_counts():
    si = _apply_si(
        verified=[{**_task("a"), "verification": "confirmed"}],
        passthrough=[{**_task("g", cls="quality"), "verification": "not-applicable"}],
        refuted=[{**_task("b"), "verification": "refuted"}],
    )
    out = await action_apply_verification_results(si)
    qr = out.context_updates["quality_results"]
    assert "1 confirmed, 1 refuted, 1 passthrough" in qr["summary"]
    assert len(qr["fix_tasks"]) == 2
