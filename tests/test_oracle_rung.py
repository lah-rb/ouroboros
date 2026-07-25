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


# ── Shared row semantics ──────────────────────────────────────────────────
#
# `n_findings` carries every row, so no row can be vacuous:
#   None -> the validation_results key must be ABSENT (the rung appended
#           nothing at all — stronger than "appended an empty list")
#   0    -> present-but-empty, or absent (the `.get(..., [])` form)
#   1    -> exactly one finding, and it must be a FAILURE (a "finding" that
#           passed would be a contradiction, so this is asserted for every
#           finding row rather than only the three that used to check it)
#
# `expected_result` uses SUBSET semantics so each row names only the result
# keys it cares about — the sanity rung's whole point is that different
# inputs surface different keys (check_plausibility vs sanity_eligible), and
# a single fixed expectation column would erase that distinction.


def _assert_findings(out, n_findings, finding_name=""):
    key = "validation_results"
    if n_findings is None:
        assert (
            key not in out.context_updates
        ), f"expected the rung to append nothing, got {out.context_updates.get(key)}"
        return
    vr = out.context_updates.get(key, [])
    assert len(vr) == n_findings, f"expected {n_findings} finding(s), got {vr}"
    if n_findings:
        assert not vr[0]["passed"], f"a finding must be a failure: {vr[0]}"
        assert vr[0]["required"], f"a finding must be required: {vr[0]}"
        if finding_name:
            assert vr[0]["name"] == finding_name, f"got {vr[0]['name']!r}"


def _assert_result(out, expected_result: dict) -> None:
    if not expected_result:
        return
    for k, want in expected_result.items():
        assert k in out.result, f"result missing {k!r}: {out.result}"
        assert (
            out.result[k] == want
        ), f"result[{k!r}] = {out.result[k]!r}, want {want!r}"


# ── Sanity rung (D2) ───────────────────────────────────────────────────────

_SANITY = [
    # A bare 0 is degenerate: flag it AND skip the plausibility turn.
    pytest.param(
        CRIT,
        {"/app/answer.txt": "0"},
        1,
        {"check_plausibility": False},
        "",
        id="bare_zero_is_degenerate",
    ),
    pytest.param(
        CRIT,
        {"/app/answer.txt": "10994372"},
        0,
        {"check_plausibility": True},
        "10994372",
        id="good_answer_requests_plausibility",
    ),
    # 0 is the ANSWER when the criteria name it — defer, do not flag.
    pytest.param(
        [
            {
                "command": "grep -qx 0 /app/answer.txt",
                "name": "is-zero",
                "required": True,
            }
        ],
        {"/app/answer.txt": "0"},
        0,
        {},
        "",
        id="defers_on_zero_the_criteria_names",
    ),
    # Not an answer-writing task at all → the rung is not eligible and must
    # append nothing (absent key, not an empty list).
    pytest.param(
        [{"command": "systemctl is-active nginx", "name": "svc", "required": True}],
        {},
        None,
        {"sanity_eligible": False},
        "",
        id="skips_non_answer_task",
    ),
    # Absent artifact is left to the existence check — fail-safe, not a finding.
    pytest.param(
        CRIT,
        {},
        None,
        {"sanity_eligible": False},
        "",
        id="failsafe_on_absent_artifact",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("criteria,files,n_findings,expected_result,excerpt", _SANITY)
async def test_sanity_rung(criteria, files, n_findings, expected_result, excerpt):
    out = await action_check_output_sanity(
        _si(_mission(criteria), MockEffects(files=files))
    )
    _assert_findings(out, n_findings)
    _assert_result(out, expected_result)
    if excerpt:
        assert excerpt in out.context_updates["sanity_artifact_excerpt"]


# ── Recorded verdicts: sanity plausibility + verify-before-harvest ─────────

_RECORDS = [
    pytest.param(
        action_record_output_sanity,
        json.dumps({"plausible": False, "reason": "3 for a huge dataset"}),
        {"sanity_artifact_excerpt": "/app/answer.txt:\n3"},
        1,
        "",
        False,
        id="sanity_implausible_appends_fail",
    ),
    # Unparseable verdict → nothing at all is published (fail-safe).
    pytest.param(
        action_record_output_sanity,
        "not json",
        {},
        None,
        "",
        True,
        id="sanity_inconclusive_failsafe",
    ),
    pytest.param(
        action_record_completion_verify,
        json.dumps({"genuinely_done": False, "reason": "holds 0"}),
        {},
        1,
        "completion_verify",
        False,
        id="verify_refute_appends_fail",
    ),
    pytest.param(
        action_record_completion_verify,
        json.dumps({"genuinely_done": True, "reason": "ok"}),
        {},
        0,
        "",
        False,
        id="verify_confirmed_no_fail",
    ),
    pytest.param(
        action_record_completion_verify,
        "garbage",
        {},
        0,
        "",
        False,
        id="verify_inconclusive_failsafe",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,response,extra_ctx,n_findings,finding_name,updates_empty", _RECORDS
)
async def test_recorded_verdicts(
    action, response, extra_ctx, n_findings, finding_name, updates_empty
):
    out = await action(_si(_mission(), inference_response=response, **extra_ctx))
    _assert_findings(out, n_findings, finding_name)
    if updates_empty:
        assert out.context_updates == {}, out.context_updates
    if action is action_record_completion_verify:
        # No restore dance: the judge verdict reaches decide via the dedicated
        # judge_response key, never by restoring inference_response.
        assert "inference_response" not in out.context_updates


# ── Verify-before-harvest (D4) — keepers ──────────────────────────────────


@pytest.mark.asyncio
async def test_reprobe_skips_when_judge_not_done():
    si = _si(_mission(CRIT), inference_response=json.dumps({"task_complete": False}))
    out = await action_reprobe_completion(si)
    assert out.result["do_verify"] is False


@pytest.mark.asyncio
async def test_reprobe_builds_transcript_and_saves_judge():
    """Not a row: asserts on two DIFFERENT published context keys (the built
    transcript and the saved judge verdict), not on validation findings."""
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
    assert "task_complete" in out.context_updates["judge_response"]


# ── Profile-gated rungs (Phase 2): liveness / conservation / round-trip ────


def _mission_p(profile, criteria=None, objective="do the thing"):
    m = _mission(criteria, objective)
    m.config.task_profile = profile
    return m


def _sh(return_code, stdout="", stderr=""):
    return {
        "/bin/sh": CommandResult(
            return_code=return_code, stdout=stdout, stderr=stderr, command="x"
        )
    }


_CURL = [{"command": "curl -sf localhost:8080/health", "name": "up", "required": True}]
_CSV = [{"command": "test -s /app/out.csv", "name": "out", "required": True}]
_ARC = [{"command": "test -s /app/logs.tar.gz", "name": "arc", "required": True}]

_PROFILE = [
    # liveness
    pytest.param(
        "service",
        _CURL,
        {},
        _sh(7, stderr="curl: (7) Connection refused"),
        "do the thing",
        1,
        "service_oracle",
        {},
        id="service_dead_flagged",
    ),
    pytest.param(
        "service",
        _CURL,
        {},
        _sh(0, stdout="OK"),
        "do the thing",
        0,
        "",
        {},
        id="service_live_passes",
    ),
    # conservation — empty and header-only were one function with an internal
    # loop; as rows they are individually reported.
    pytest.param(
        "data_transform",
        _CSV,
        {"/app/out.csv": ""},
        {},
        "do the thing",
        1,
        "data_transform_oracle",
        {},
        id="conservation_empty_output",
    ),
    pytest.param(
        "data_transform",
        _CSV,
        {"/app/out.csv": "col1,col2\n"},
        {},
        "do the thing",
        1,
        "data_transform_oracle",
        {},
        id="conservation_header_only_output",
    ),
    pytest.param(
        "data_transform",
        _CSV,
        {"/app/out.csv": "col1,col2\n1,2\n3,4\n"},
        {},
        "do the thing",
        0,
        "",
        {},
        id="conservation_with_data_rows_passes",
    ),
    # round-trip
    pytest.param(
        "invertible",
        _ARC,
        {"/app/logs.tar.gz": "corrupt"},
        _sh(1, stderr="gzip: invalid"),
        "compress logs to /app/logs.tar.gz",
        1,
        "invertible_oracle",
        {},
        id="roundtrip_corrupt_archive_flagged",
    ),
    # a profile with no rung must skip cleanly and append nothing
    pytest.param(
        "plain",
        None,
        {},
        {},
        "do the thing",
        None,
        "",
        {"profile_checked": False},
        id="unknown_profile_skipped",
    ),
    # regression (repair profile)
    pytest.param(
        "repair",
        None,
        {},
        _sh(
            2, stderr="ImportError: cannot import name 'foo'\nerrors during collection"
        ),
        "do the thing",
        1,
        "repair_oracle",
        {},
        id="repair_broken_collection_flagged",
    ),
    pytest.param(
        "repair",
        None,
        {},
        _sh(0, stdout="collected 42 items"),
        "do the thing",
        0,
        "",
        {},
        id="repair_clean_collection_passes",
    ),
    # A pre-existing absence (no pytest) is NOT a fix-induced break. NOTE this
    # row pins the OUTCOME (no false finding), not the MECHANISM: at the
    # action boundary "skipped because pytest is absent" and "ran and found
    # nothing" are indistinguishable — identical result dict, identical empty
    # findings. Mutating the guard away does not fail this row, which was
    # equally true of the pre-table test it replaces. The mechanism is pinned
    # directly on _check_regression below, where None (skip) is observable.
    pytest.param(
        "repair",
        None,
        {},
        _sh(1, stderr="No module named pytest"),
        "do the thing",
        0,
        "",
        {},
        id="repair_skips_when_pytest_absent",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,criteria,files,commands,objective,n_findings,finding_name,expected_result",
    _PROFILE,
)
async def test_profile_oracle(
    profile,
    criteria,
    files,
    commands,
    objective,
    n_findings,
    finding_name,
    expected_result,
):
    eff = MockEffects(files=files, commands=commands)
    out = await action_check_profile_oracle(
        _si(_mission_p(profile, criteria, objective), eff)
    )
    _assert_findings(out, n_findings, finding_name)
    _assert_result(out, expected_result)


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


@pytest.mark.asyncio
async def test_check_regression_skips_when_pytest_is_absent():
    """The repair rung must not blame a fix for a tool that was never there.

    Pinned at the HELPER, not through action_check_profile_oracle: the action
    collapses "skipped" and "ran clean" into the same output, so only the
    rung's own None-vs-string return can prove the guard fires. Found while
    mutation-checking C6 — removing the guard failed nothing at the action
    level, which is exactly the silent-hole case the mechanic exists for.
    """
    from agent.actions.oracle_actions import _check_regression

    # The message must be the REALISTIC one. A bare "No module named pytest"
    # is non-discriminating: it returns None with or without the guard,
    # because it matches no collection-error pattern either. Real absence
    # emits "ModuleNotFoundError: ...", which the collection-error regex WOULD
    # flag — so this input is the only one that proves the guard is what
    # suppresses the finding.
    absent = MockEffects(
        commands=_sh(1, stderr="ModuleNotFoundError: No module named pytest")
    )
    assert await _check_regression(absent, [], "do the thing") is None

    # Contrast: a genuine collection break IS reported (so the None above is
    # the guard talking, not the helper being inert).
    broken = MockEffects(
        commands=_sh(
            2, stderr="ImportError: cannot import name 'foo'\nerrors during collection"
        )
    )
    assert await _check_regression(broken, [], "do the thing") is not None
