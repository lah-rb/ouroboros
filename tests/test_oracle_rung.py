"""Oracle rungs — non-degeneracy sanity (D2) + verify-before-harvest (D4).

Pins the contract: each rung appends a {required:true} validation_result only on
a GENUINE finding, never on its own error (fail-safe); the sanity floor defers on
a 0 the criteria names; the VBH judge verdict rides the dedicated judge_response
key (no inference_response restore); and the combined artifact oracle runs all
rungs over ONE artifact read.
"""

from __future__ import annotations

import json

import pytest

from agent.actions.oracle_actions import (
    action_check_artifact_oracles,
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
        task_spec=objective,
        completion_criteria=criteria if criteria is not None else [],
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
    crit = [
        {"command": "grep -qx 0 /app/answer.txt", "name": "is-zero", "required": True}
    ]
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
        inference_response=json.dumps(
            {"plausible": False, "reason": "3 for a huge dataset"}
        ),
        sanity_artifact_excerpt="/app/answer.txt:\n3",
    )
    out = await action_record_output_sanity(si)
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and not vr[0]["passed"]


@pytest.mark.asyncio
async def test_record_sanity_inconclusive_failsafe():
    out = await action_record_output_sanity(
        _si(_mission(), inference_response="not json")
    )
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
        commands={
            "/bin/sh": CommandResult(return_code=0, stdout="", stderr="", command="x")
        },
    )
    si = _si(
        _mission(CRIT), eff, inference_response=json.dumps({"task_complete": True})
    )
    out = await action_reprobe_completion(si)
    assert out.result["do_verify"] is True
    assert "10994372" in out.context_updates["vbh_transcript"]
    assert (
        "task_complete" in out.context_updates["judge_response"]
    )  # judge verdict saved


@pytest.mark.asyncio
async def test_record_verify_refute_appends_fail():
    si = _si(
        _mission(),
        inference_response=json.dumps({"genuinely_done": False, "reason": "holds 0"}),
    )
    out = await action_record_completion_verify(si)
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and not vr[0]["passed"] and vr[0]["name"] == "completion_verify"
    # No restore dance: the judge verdict reaches decide via judge_response.
    assert "inference_response" not in out.context_updates


@pytest.mark.asyncio
async def test_record_verify_confirmed_no_fail():
    si = _si(
        _mission(),
        inference_response=json.dumps({"genuinely_done": True, "reason": "ok"}),
    )
    out = await action_record_completion_verify(si)
    assert out.context_updates.get("validation_results", []) == []
    assert "inference_response" not in out.context_updates


@pytest.mark.asyncio
async def test_record_verify_inconclusive_failsafe():
    out = await action_record_completion_verify(
        _si(_mission(), inference_response="garbage")
    )
    assert (
        out.context_updates.get("validation_results", []) == []
    )  # no fail manufactured


# ── Profile-gated rungs (Phase 2): liveness / conservation / round-trip ────


def _mission_p(profile, criteria=None, objective="do the thing"):
    m = _mission(criteria, objective)
    m.config.task_profile = profile
    return m


@pytest.mark.asyncio
async def test_liveness_fails_on_dead_service():
    crit = [
        {"command": "curl -sf localhost:8080/health", "name": "up", "required": True}
    ]
    eff = MockEffects(
        commands={
            "/bin/sh": CommandResult(
                return_code=7,
                stdout="",
                stderr="curl: (7) Connection refused",
                command="x",
            )
        }
    )
    out = await action_check_profile_oracle(_si(_mission_p("service", crit), eff))
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and not vr[0]["passed"] and vr[0]["name"] == "service_oracle"


@pytest.mark.asyncio
async def test_liveness_passes_on_live_service():
    crit = [
        {"command": "curl -sf localhost:8080/health", "name": "up", "required": True}
    ]
    eff = MockEffects(
        commands={
            "/bin/sh": CommandResult(return_code=0, stdout="OK", stderr="", command="x")
        }
    )
    out = await action_check_profile_oracle(_si(_mission_p("service", crit), eff))
    assert out.context_updates.get("validation_results", []) == []


@pytest.mark.asyncio
async def test_conservation_flags_empty_and_zero_row_output():
    crit = [{"command": "test -s /app/out.csv", "name": "out", "required": True}]
    for content in ("", "col1,col2\n"):  # empty, header-only
        eff = MockEffects(files={"/app/out.csv": content})
        out = await action_check_profile_oracle(
            _si(_mission_p("data_transform", crit), eff)
        )
        vr = out.context_updates["validation_results"]
        assert len(vr) == 1 and vr[0]["name"] == "data_transform_oracle"


@pytest.mark.asyncio
async def test_conservation_passes_with_data_rows():
    crit = [{"command": "test -s /app/out.csv", "name": "out", "required": True}]
    eff = MockEffects(files={"/app/out.csv": "col1,col2\n1,2\n3,4\n"})
    out = await action_check_profile_oracle(
        _si(_mission_p("data_transform", crit), eff)
    )
    assert out.context_updates.get("validation_results", []) == []


@pytest.mark.asyncio
async def test_roundtrip_flags_corrupt_archive():
    crit = [{"command": "test -s /app/logs.tar.gz", "name": "arc", "required": True}]
    eff = MockEffects(
        files={"/app/logs.tar.gz": "corrupt"},
        commands={
            "/bin/sh": CommandResult(
                return_code=1, stdout="", stderr="gzip: invalid", command="x"
            )
        },
    )
    out = await action_check_profile_oracle(
        _si(_mission_p("invertible", crit, "compress logs to /app/logs.tar.gz"), eff)
    )
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["name"] == "invertible_oracle"


@pytest.mark.asyncio
async def test_profile_oracle_skips_other_profiles():
    out = await action_check_profile_oracle(_si(_mission_p("plain"), MockEffects()))
    assert out.result["profile_checked"] is False
    assert "validation_results" not in out.context_updates


@pytest.mark.asyncio
async def test_regression_flags_broken_collection():
    eff = MockEffects(
        commands={
            "/bin/sh": CommandResult(
                return_code=2,
                stdout="",
                stderr="ImportError: cannot import name 'foo'\nerrors during collection",
                command="x",
            )
        }
    )
    out = await action_check_profile_oracle(_si(_mission_p("repair"), eff))
    vr = out.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["name"] == "repair_oracle"


@pytest.mark.asyncio
async def test_regression_passes_clean_collection():
    eff = MockEffects(
        commands={
            "/bin/sh": CommandResult(
                return_code=0, stdout="collected 42 items", stderr="", command="x"
            )
        }
    )
    out = await action_check_profile_oracle(_si(_mission_p("repair"), eff))
    assert out.context_updates.get("validation_results", []) == []


@pytest.mark.asyncio
async def test_regression_skips_when_pytest_absent():
    # A pre-existing absence (no pytest) is NOT a fix-induced break — must skip.
    eff = MockEffects(
        commands={
            "/bin/sh": CommandResult(
                return_code=1, stdout="", stderr="No module named pytest", command="x"
            )
        }
    )
    out = await action_check_profile_oracle(_si(_mission_p("repair"), eff))
    assert out.context_updates.get("validation_results", []) == []


# ── Combined artifact oracle: all rungs, ONE read ──────────────────────────


class _CountingEffects(MockEffects):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.read_calls = 0

    async def read_file(self, path):
        self.read_calls += 1
        return await super().read_file(path)


@pytest.mark.asyncio
async def test_artifact_oracle_runs_all_rungs_over_one_read():
    # Degenerate answer (sanity rung) + shape miss (format rung) on the SAME
    # artifact: both findings append, and the artifact is read exactly once
    # even though sanity + format + (conservation-eligible) profile all want it.
    m = _mission_p("data_transform", CRIT)
    m.task_definition.output_format_spec = {
        "output_file": "/app/answer.txt",
        "checks": [{"type": "line_count", "value": 1}],
    }
    eff = _CountingEffects(
        files={"/app/answer.txt": ""}
    )  # empty → degenerate + 0 lines
    out = await action_check_artifact_oracles(_si(m, eff))
    names = [r["name"] for r in out.context_updates["validation_results"]]
    assert any(n.startswith("output_sanity") for n in names)
    assert any(n.startswith("output_format") for n in names)
    assert any(n == "data_transform_oracle" for n in names)
    assert eff.read_calls == 1  # the whole point: one read, all rungs
    assert (
        out.result["check_plausibility"] is False
    )  # degenerate → no plausibility turn


@pytest.mark.asyncio
async def test_artifact_oracle_clean_answer_routes_to_plausibility():
    m = _mission(CRIT)
    eff = _CountingEffects(files={"/app/answer.txt": "10994372"})
    out = await action_check_artifact_oracles(_si(m, eff))
    assert out.context_updates["validation_results"] == []
    assert out.result["check_plausibility"] is True
    assert "10994372" in out.context_updates["sanity_artifact_excerpt"]


@pytest.mark.asyncio
async def test_artifact_oracle_threads_prior_results_through():
    # Findings from run_checks (already in validation_results) survive, and the
    # rungs' appends land after them.
    m = _mission(CRIT)
    prior = [{"name": "dod", "passed": False, "required": True}]
    eff = _CountingEffects(files={"/app/answer.txt": "0"})
    out = await action_check_artifact_oracles(_si(m, eff, validation_results=prior))
    vr = out.context_updates["validation_results"]
    assert vr[0]["name"] == "dod"
    assert any(r["name"].startswith("output_sanity") for r in vr[1:])
