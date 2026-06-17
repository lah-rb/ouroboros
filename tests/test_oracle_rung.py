"""Oracle rungs — non-degeneracy sanity (D2) + verify-before-harvest (D4).

Pins the contract: each rung appends a {required:true} validation_result only on
a GENUINE finding, never on its own error (fail-safe); the sanity floor defers on
a 0 the criteria names; and the VBH loop restores the judge verdict for decide.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.oracle_actions import (
    action_check_output_sanity,
    action_check_profile_oracle,
    action_record_completion_verify,
    action_record_output_sanity,
    action_reprobe_completion,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState, TaskState

CRIT = [{"command": "test -s /app/answer.txt", "name": "exists", "required": True}]


def _mission(criteria=None, objective="write the token count to /app/answer.txt"):
    m = MissionState(
        objective=objective,
        status="active",
        config=MissionConfig(working_directory="/app", flow_set="ops"),
    )
    m.task_definition = TaskState(
        task_spec=objective, completion_criteria=criteria if criteria is not None else []
    )
    return m


def _si(mission, effects=None, **ctx) -> StepInput:
    return StepInput(
        context={"mission": mission, **ctx},
        params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"),
        effects=effects if effects is not None else MockEffects(),
    )


# ── Sanity rung (D2) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sanity_flags_bare_zero():
    out = await action_check_output_sanity(
        _si(_mission(CRIT), MockEffects(files={"/app/answer.txt": "0"}))
    )
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["required"] and not vr[0]["passed"]
    assert out.result["check_plausibility"] is False  # degenerate → skip plausibility


@pytest.mark.asyncio
async def test_sanity_passes_good_answer_and_requests_plausibility():
    out = await action_check_output_sanity(
        _si(_mission(CRIT), MockEffects(files={"/app/answer.txt": "10994372"}))
    )
    assert out.context_updates["validation_results"] == []  # no finding
    assert out.result["check_plausibility"] is True
    assert "10994372" in out.context_updates["sanity_artifact_excerpt"]


@pytest.mark.asyncio
async def test_sanity_defers_on_zero_the_criteria_names():
    crit = [{"command": "grep -qx 0 /app/answer.txt", "name": "is-zero", "required": True}]
    out = await action_check_output_sanity(
        _si(_mission(crit), MockEffects(files={"/app/answer.txt": "0"}))
    )
    assert out.context_updates["validation_results"] == []  # 0 is the expected answer


@pytest.mark.asyncio
async def test_sanity_skips_non_answer_task():
    crit = [{"command": "systemctl is-active nginx", "name": "svc", "required": True}]
    out = await action_check_output_sanity(_si(_mission(crit)))
    assert out.result["sanity_eligible"] is False
    assert "validation_results" not in out.context_updates  # appended nothing


@pytest.mark.asyncio
async def test_sanity_failsafe_on_absent_artifact():
    out = await action_check_output_sanity(_si(_mission(CRIT)))  # no file seeded
    assert out.result["sanity_eligible"] is False  # absent → left to existence check
    assert "validation_results" not in out.context_updates


@pytest.mark.asyncio
async def test_record_sanity_implausible_appends_fail():
    si = _si(
        _mission(),
        inference_response=json.dumps({"plausible": False, "reason": "3 for a huge dataset"}),
        sanity_artifact_excerpt="/app/answer.txt:\n3",
    )
    out = await action_record_output_sanity(si)
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and not vr[0]["passed"]


@pytest.mark.asyncio
async def test_record_sanity_inconclusive_failsafe():
    out = await action_record_output_sanity(_si(_mission(), inference_response="not json"))
    assert out.context_updates == {}  # nothing appended on an unparseable verdict


# ── Verify-before-harvest (D4) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reprobe_skips_when_judge_not_done():
    si = _si(_mission(CRIT), inference_response=json.dumps({"task_complete": False}))
    out = await action_reprobe_completion(si)
    assert out.result["do_verify"] is False


@pytest.mark.asyncio
async def test_reprobe_builds_transcript_and_saves_judge():
    eff = MockEffects(
        files={"/app/answer.txt": "10994372"},
        commands={"/bin/sh": CommandResult(return_code=0, stdout="", stderr="", command="x")},
    )
    si = _si(_mission(CRIT), eff, inference_response=json.dumps({"task_complete": True}))
    out = await action_reprobe_completion(si)
    assert out.result["do_verify"] is True
    assert "10994372" in out.context_updates["vbh_transcript"]
    assert "task_complete" in out.context_updates["judge_response"]  # judge verdict saved


@pytest.mark.asyncio
async def test_record_verify_refute_appends_fail_and_restores_judge():
    judge = json.dumps({"task_complete": True, "feedback": ""})
    si = _si(
        _mission(),
        inference_response=json.dumps({"genuinely_done": False, "reason": "holds 0"}),
        judge_response=judge,
    )
    out = await action_record_completion_verify(si)
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and not vr[0]["passed"] and vr[0]["name"] == "completion_verify"
    assert out.context_updates["inference_response"] == judge  # restored for decide


@pytest.mark.asyncio
async def test_record_verify_confirmed_no_fail_restores_judge():
    judge = json.dumps({"task_complete": True})
    si = _si(
        _mission(),
        inference_response=json.dumps({"genuinely_done": True, "reason": "ok"}),
        judge_response=judge,
    )
    out = await action_record_completion_verify(si)
    assert out.context_updates.get("validation_results", []) == []
    assert out.context_updates["inference_response"] == judge


@pytest.mark.asyncio
async def test_record_verify_inconclusive_failsafe():
    judge = json.dumps({"task_complete": True})
    out = await action_record_completion_verify(
        _si(_mission(), inference_response="garbage", judge_response=judge)
    )
    assert out.context_updates.get("validation_results", []) == []  # no fail manufactured
    assert out.context_updates["inference_response"] == judge  # still restored


# ── Profile-gated rungs (Phase 2): liveness / conservation / round-trip ────


def _mission_p(profile, criteria=None, objective="do the thing"):
    m = _mission(criteria, objective)
    m.config.task_profile = profile
    return m


@pytest.mark.asyncio
async def test_liveness_fails_on_dead_service():
    crit = [{"command": "curl -sf localhost:8080/health", "name": "up", "required": True}]
    eff = MockEffects(commands={"/bin/sh": CommandResult(
        return_code=7, stdout="", stderr="curl: (7) Connection refused", command="x")})
    out = await action_check_profile_oracle(_si(_mission_p("service", crit), eff))
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and not vr[0]["passed"] and vr[0]["name"] == "service_oracle"


@pytest.mark.asyncio
async def test_liveness_passes_on_live_service():
    crit = [{"command": "curl -sf localhost:8080/health", "name": "up", "required": True}]
    eff = MockEffects(commands={"/bin/sh": CommandResult(
        return_code=0, stdout="OK", stderr="", command="x")})
    out = await action_check_profile_oracle(_si(_mission_p("service", crit), eff))
    assert out.context_updates.get("validation_results", []) == []


@pytest.mark.asyncio
async def test_conservation_flags_empty_and_zero_row_output():
    crit = [{"command": "test -s /app/out.csv", "name": "out", "required": True}]
    for content in ("", "col1,col2\n"):  # empty, header-only
        eff = MockEffects(files={"/app/out.csv": content})
        out = await action_check_profile_oracle(_si(_mission_p("data_transform", crit), eff))
        vr = out.context_updates["validation_results"]
        assert len(vr) == 1 and vr[0]["name"] == "data_transform_oracle"


@pytest.mark.asyncio
async def test_conservation_passes_with_data_rows():
    crit = [{"command": "test -s /app/out.csv", "name": "out", "required": True}]
    eff = MockEffects(files={"/app/out.csv": "col1,col2\n1,2\n3,4\n"})
    out = await action_check_profile_oracle(_si(_mission_p("data_transform", crit), eff))
    assert out.context_updates.get("validation_results", []) == []


@pytest.mark.asyncio
async def test_roundtrip_flags_corrupt_archive():
    crit = [{"command": "test -s /app/logs.tar.gz", "name": "arc", "required": True}]
    eff = MockEffects(
        files={"/app/logs.tar.gz": "corrupt"},
        commands={"/bin/sh": CommandResult(return_code=1, stdout="", stderr="gzip: invalid", command="x")},
    )
    out = await action_check_profile_oracle(
        _si(_mission_p("invertible", crit, "compress logs to /app/logs.tar.gz"), eff))
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["name"] == "invertible_oracle"


@pytest.mark.asyncio
async def test_profile_oracle_skips_other_profiles():
    out = await action_check_profile_oracle(_si(_mission_p("plain"), MockEffects()))
    assert out.result["profile_checked"] is False
    assert "validation_results" not in out.context_updates


@pytest.mark.asyncio
async def test_regression_flags_broken_collection():
    eff = MockEffects(commands={"/bin/sh": CommandResult(
        return_code=2, stdout="",
        stderr="ImportError: cannot import name 'foo'\nerrors during collection",
        command="x")})
    out = await action_check_profile_oracle(_si(_mission_p("repair"), eff))
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["name"] == "repair_oracle"


@pytest.mark.asyncio
async def test_regression_passes_clean_collection():
    eff = MockEffects(commands={"/bin/sh": CommandResult(
        return_code=0, stdout="collected 42 items", stderr="", command="x")})
    out = await action_check_profile_oracle(_si(_mission_p("repair"), eff))
    assert out.context_updates.get("validation_results", []) == []


@pytest.mark.asyncio
async def test_regression_skips_when_pytest_absent():
    # A pre-existing absence (no pytest) is NOT a fix-induced break — must skip.
    eff = MockEffects(commands={"/bin/sh": CommandResult(
        return_code=1, stdout="", stderr="No module named pytest", command="x")})
    out = await action_check_profile_oracle(_si(_mission_p("repair"), eff))
    assert out.context_updates.get("validation_results", []) == []
