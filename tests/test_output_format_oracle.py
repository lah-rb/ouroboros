"""Output-format oracle — deterministic shape check vs a derived spec.

Pins the contract: the rung flags a SHAPE mismatch (wrapping, missing key, wrong
line count, wrong/absent filename) as a {required:true} validation_result, never
on its own error (fail-safe), and NEVER on a correct answer (the conservatism that
keeps it from blocking a right result on a guessed format). Includes a replay of
the real v3 canary close-misses (chess `[e2e4]`, multi-source missing key).
"""

from __future__ import annotations

import pytest

from agent.actions.operations_actions import (
    action_exa_probe_gate,
    action_store_output_format,
    action_store_reground_output_format,
    action_store_search_findings,
)
from agent.actions.oracle_actions import (
    _apply_format_checks,
    action_check_output_format,
    action_gate_reground_output_format,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState, TaskState


def _mission(spec=None, criteria=None, objective="write the answer to /app/out.txt"):
    m = MissionState(
        objective=objective,
        status="active",
        config=MissionConfig(working_directory="/app", flow_set="ops"),
    )
    m.task_definition = TaskState(
        task_spec=objective,
        completion_criteria=criteria or [],
        output_format_spec=spec,
    )
    return m


def _si(mission, effects=None, response=None) -> StepInput:
    ctx = {"mission": mission}
    if response is not None:
        ctx["inference_response"] = response
    return StepInput(
        context=ctx,
        params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"),
        effects=effects if effects is not None else MockEffects(),
    )


# ── _apply_format_checks: per type ───────────────────────────────────────────


def test_line_count_exact_and_max():
    assert _apply_format_checks("a\nb\nc", [{"type": "line_count", "value": 1}])
    assert not _apply_format_checks("only one", [{"type": "line_count", "value": 1}])
    assert not _apply_format_checks("a\nb", [{"type": "line_count", "value": 3, "op": "<="}])
    assert _apply_format_checks("a\nb\nc\nd", [{"type": "line_count", "value": 3, "op": "<="}])


def test_regex_and_no_wrapping():
    pat = "^[a-h][1-8][a-h][1-8][qrbn]?$"
    assert not _apply_format_checks("e2e4", [{"type": "regex", "pattern": pat}])
    assert _apply_format_checks("nope", [{"type": "regex", "pattern": pat}])
    for wrapped in ("[e2e4]", '"e2e4"', "(e2e4)", "{e2e4}"):
        assert _apply_format_checks(wrapped, [{"type": "no_wrapping"}]), wrapped
    assert not _apply_format_checks("e2e4", [{"type": "no_wrapping"}])


def test_required_keys_json_and_non_json():
    assert not _apply_format_checks('{"total_conflicts": 3, "merged": 5}',
                                    [{"type": "required_keys", "keys": ["total_conflicts"]}])
    assert _apply_format_checks('{"merged": 5}',
                                [{"type": "required_keys", "keys": ["total_conflicts"]}])
    # non-JSON when keys are required is itself a shape miss (a finding, not a skip)
    assert _apply_format_checks("not json at all",
                                [{"type": "required_keys", "keys": ["x"]}])


def test_columns_csv_header():
    assert not _apply_format_checks("a,b,c\n1,2,3", [{"type": "columns", "columns": ["a", "c"]}])
    assert _apply_format_checks("a,b\n1,2", [{"type": "columns", "columns": ["a", "d"]}])


def test_fail_safe_skips_broken_check_applies_others():
    # An invalid regex is the rung's own error → skip THAT check, still apply the rest.
    out = _apply_format_checks("[e2e4]", [{"type": "regex", "pattern": "[unterminated"},
                                          {"type": "no_wrapping"}])
    assert len(out) == 1 and "wrapped" in out[0]
    # Unknown check type → skipped, no crash.
    assert _apply_format_checks("x", [{"type": "made_up"}]) == []


# ── The rung: GATE / CHECK / APPEND / FAIL-SAFE ──────────────────────────────


@pytest.mark.asyncio
async def test_gate_skips_when_no_spec_or_no_checks():
    out = await action_check_output_format(_si(_mission(spec=None)))
    assert "validation_results" not in out.context_updates
    out2 = await action_check_output_format(_si(_mission(spec={"output_file": "/app/o", "checks": []})))
    assert out2.result["format_eligible"] is False


@pytest.mark.asyncio
async def test_flags_wrapped_scalar_passes_bare():
    spec = {"output_file": "/app/move.txt",
            "checks": [{"type": "no_wrapping"}, {"type": "line_count", "value": 1}]}
    bad = await action_check_output_format(
        _si(_mission(spec), MockEffects(files={"/app/move.txt": "[e2e4]"})))
    vr = bad.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["required"] and not vr[0]["passed"]
    good = await action_check_output_format(
        _si(_mission(spec), MockEffects(files={"/app/move.txt": "e2e4"})))
    assert good.result["format_passed"] is True
    assert good.context_updates["validation_results"] == []  # no false gate


@pytest.mark.asyncio
async def test_absent_file_flags_only_with_exists_check():
    # Wrong/absent output filename — flagged ONLY when the spec made the path a
    # requirement (catches the path-tracing close-miss); else deferred.
    spec_exists = {"output_file": "/app/reconstructed.ppm", "checks": [{"type": "exists"}]}
    out = await action_check_output_format(_si(_mission(spec_exists), MockEffects(files={})))
    assert out.context_updates["validation_results"][0]["passed"] is False
    spec_no_exists = {"output_file": "/app/reconstructed.ppm", "checks": [{"type": "line_count", "value": 1}]}
    out2 = await action_check_output_format(_si(_mission(spec_no_exists), MockEffects(files={})))
    assert out2.result["format_eligible"] is False  # deferred to existence check


@pytest.mark.asyncio
async def test_fail_safe_on_read_error_appends_nothing():
    class Boom(MockEffects):
        async def read_file(self, path):  # noqa: ARG002
            raise RuntimeError("io error")

    out = await action_check_output_format(
        _si(_mission({"output_file": "/app/o", "checks": [{"type": "no_wrapping"}]}), Boom()))
    assert "validation_results" not in out.context_updates
    assert out.result["format_eligible"] is False


# ── Replay the real v3 canary close-misses end-to-end ────────────────────────


@pytest.mark.asyncio
async def test_replay_v3_chess_and_multisource():
    chess = {"output_file": "/app/move.txt",
             "checks": [{"type": "no_wrapping"},
                        {"type": "regex", "pattern": "^[a-h][1-8][a-h][1-8][qrbn]?$"}]}
    out = await action_check_output_format(
        _si(_mission(chess), MockEffects(files={"/app/move.txt": "[e2e4]"})))
    assert out.result["format_passed"] is False  # the bracket close-miss is caught

    ms = {"output_file": "/app/conflicts.json",
          "checks": [{"type": "required_keys", "keys": ["total_conflicts"]}]}
    out2 = await action_check_output_format(
        _si(_mission(ms), MockEffects(files={"/app/conflicts.json": '{"resolved": 4}'})))
    assert out2.result["format_passed"] is False  # the missing-key close-miss is caught


# ── The store action: parse + conservatism ───────────────────────────────────


@pytest.mark.asyncio
async def test_store_parses_valid_spec():
    m = _mission()
    resp = '```json\n{"output_file": "/app/move.txt", "checks": [{"type": "no_wrapping"}]}\n```'
    out = await action_store_output_format(_si(m, response=resp))
    assert out.result["format_check_count"] == 1
    assert m.task_definition.output_format_spec["output_file"] == "/app/move.txt"


@pytest.mark.asyncio
async def test_store_conservative_empty_and_unparseable():
    # Conservatism: no checks → store None (no gate).
    m = _mission()
    out = await action_store_output_format(_si(m, response='{"checks": []}'))
    assert out.result["format_check_count"] == 0
    assert m.task_definition.output_format_spec is None
    # Unparseable → None.
    m2 = _mission()
    await action_store_output_format(_si(m2, response="not json at all"))
    assert m2.task_definition.output_format_spec is None


@pytest.mark.asyncio
async def test_store_filters_unknown_check_types():
    m = _mission()
    resp = '{"output_file": "o", "checks": [{"type": "no_wrapping"}, {"type": "bogus"}]}'
    await action_store_output_format(_si(m, response=resp))
    spec = m.task_definition.output_format_spec
    assert len(spec["checks"]) == 1 and spec["checks"][0]["type"] == "no_wrapping"


# ── Grounded reground: gate + store (quality_gate port) ──────────────────────


def _si_term(mission, terminal="$ ls\nresult.txt stub present\n", response=None) -> StepInput:
    ctx = {"mission": mission, "terminal_output": terminal}
    if response is not None:
        ctx["inference_response"] = response
    return StepInput(
        context=ctx, params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"), effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_gate_fires_on_empty_spec():
    # Blind early pass left no usable spec, not yet grounded → re-derive (the
    # 69%-empty-spec recovery).
    out = await action_gate_reground_output_format(_si_term(_mission(spec=None)))
    assert out.result["needs_reground"] is True
    # A spec with no checks is "empty" too.
    out2 = await action_gate_reground_output_format(
        _si_term(_mission(spec={"output_file": "o", "checks": []})))
    assert out2.result["needs_reground"] is True


@pytest.mark.asyncio
async def test_gate_fires_even_without_terminal_output():
    # Regression for the inert-port canary: terminal_output is NOT reliably a truthy
    # string at the gate's position in the live ops_task flow. The gate must STILL
    # fire (it is structurally post-scan/session; the reground grounds from
    # project_manifest, not terminal_output). The old gate wrongly required a truthy
    # terminal_output here and so never fired in production.
    no_term = StepInput(
        context={"mission": _mission(spec=None)}, params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"), effects=MockEffects())
    assert (await action_gate_reground_output_format(no_term)).result["needs_reground"] is True


@pytest.mark.asyncio
async def test_gate_skips_only_when_usable_spec_or_grounded():
    # A usable early spec is never re-derived (zero cost; trust the early pass).
    usable = {"output_file": "/app/o", "checks": [{"type": "exists"}]}
    out = await action_gate_reground_output_format(_si_term(_mission(spec=usable)))
    assert out.result["needs_reground"] is False
    # Already grounded → one-shot, never fires twice.
    m = _mission(spec=None)
    m.task_definition.output_format_grounded = True
    assert (await action_gate_reground_output_format(_si_term(m))).result["needs_reground"] is False


@pytest.mark.asyncio
async def test_reground_store_sets_spec_and_grounds():
    m = _mission(spec=None)
    resp = '```json\n{"output_file": "/app/result.txt", "checks": [{"type": "exists"}]}\n```'
    out = await action_store_reground_output_format(_si_term(m, response=resp))
    assert out.result["format_check_count"] == 1
    assert m.task_definition.output_format_spec["output_file"] == "/app/result.txt"
    assert m.task_definition.output_format_grounded is True


@pytest.mark.asyncio
async def test_reground_store_marks_grounded_even_when_empty():
    # One-shot: if the reground still finds nothing, mark grounded so it won't re-fire
    # (and leave the existing spec untouched — stays conservative, no gate).
    m = _mission(spec=None)
    out = await action_store_reground_output_format(_si_term(m, response='{"checks": []}'))
    assert out.result["format_check_count"] == 0
    assert m.task_definition.output_format_spec is None
    assert m.task_definition.output_format_grounded is True
    # And once grounded, the gate no longer fires (no per-cycle inference leak).
    assert (await action_gate_reground_output_format(_si_term(m))).result["needs_reground"] is False


# ── Stuck-task external search: gate + store (anti-give-up dynamic arm) ───────


@pytest.mark.asyncio
async def test_exa_probe_gate_fires_when_stuck():
    m = _mission(objective="recover a truncated sqlite database at /app/trunc.db")
    m.task_definition.attempts = 2
    out = await action_exa_probe_gate(_si(m))
    assert out.result["should_search"] is True
    assert out.context_updates["search_queries"]  # a query was derived


@pytest.mark.asyncio
async def test_exa_probe_gate_skips_early_or_already_searched():
    # < 2 attempts → not stuck yet.
    m = _mission()
    m.task_definition.attempts = 1
    assert (await action_exa_probe_gate(_si(m))).result["should_search"] is False
    # already searched → one-shot, never re-fires.
    m2 = _mission()
    m2.task_definition.attempts = 3
    m2.task_definition.search_findings = "prior hits"
    assert (await action_exa_probe_gate(_si(m2))).result["should_search"] is False


@pytest.mark.asyncio
async def test_store_search_findings_formats_and_one_shots():
    m = _mission()
    si = StepInput(
        context={"mission": m, "raw_search_results": [
            {"url": "http://x", "content": "use sqlite .recover to rebuild a truncated db"}]},
        params={}, meta=FlowMeta(flow_name="ops_task", step_id="x"), effects=MockEffects())
    out = await action_store_search_findings(si)
    assert out.result["n_hits"] == 1
    assert "sqlite .recover" in m.task_definition.search_findings
    # empty hits → sentinel so the gate stops re-searching (one-shot).
    m2 = _mission()
    si2 = StepInput(
        context={"mission": m2, "raw_search_results": []}, params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"), effects=MockEffects())
    await action_store_search_findings(si2)
    assert m2.task_definition.search_findings.startswith("(no relevant")
