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
    action_store_reground_criteria,
    action_store_reground_output_format,
    action_store_search_findings,
)
from agent.actions.oracle_actions import (
    _apply_format_checks,
    action_check_output_format,
    action_gate_reground_criteria,
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

_CHESS = "^[a-h][1-8][a-h][1-8][qrbn]?$"

# (content, checks, n_findings, must_contain)
#
# The expectation is a COUNT, not a bool. `_apply_format_checks` returns a
# LIST of finding strings, and at least one case here turns on the exact
# length: the fail-safe row's whole claim is that a broken check is SKIPPED
# while the remaining checks still fire — one finding, not "some". A boolean
# column would silently accept two.
#
# `must_contain` is set only where the original test asserted on message
# content; the count carries the rest. Adding substring pins to every row
# would trade a real contract for prose (TESTING.md).
_FORMAT_CASES = [
    # line_count — exact, then the `op: <=` maximum form
    pytest.param(
        "a\nb\nc",
        [{"type": "line_count", "value": 1}],
        1,
        "",
        id="line_count_exact_mismatch",
    ),
    pytest.param(
        "only one",
        [{"type": "line_count", "value": 1}],
        0,
        "",
        id="line_count_exact_ok",
    ),
    pytest.param(
        "a\nb",
        [{"type": "line_count", "value": 3, "op": "<="}],
        0,
        "",
        id="line_count_max_ok",
    ),
    # BOUNDARY (added with the table): at-most where actual == n. The
    # pre-table tests never covered it, and it is exactly where an off-by-one
    # in the `>` comparison would live.
    pytest.param(
        "a\nb\nc",
        [{"type": "line_count", "value": 3, "op": "<="}],
        0,
        "",
        id="line_count_max_at_boundary",
    ),
    pytest.param(
        "a\nb\nc\nd",
        [{"type": "line_count", "value": 3, "op": "<="}],
        1,
        "",
        id="line_count_max_exceeded",
    ),
    # regex
    pytest.param("e2e4", [{"type": "regex", "pattern": _CHESS}], 0, "", id="regex_ok"),
    pytest.param(
        "nope", [{"type": "regex", "pattern": _CHESS}], 1, "", id="regex_mismatch"
    ),
    # no_wrapping — every wrapper form the v3 canary produced
    pytest.param("[e2e4]", [{"type": "no_wrapping"}], 1, "wrapped", id="wrap_square"),
    pytest.param(
        '"e2e4"', [{"type": "no_wrapping"}], 1, "wrapped", id="wrap_double_quote"
    ),
    pytest.param("(e2e4)", [{"type": "no_wrapping"}], 1, "wrapped", id="wrap_paren"),
    pytest.param("{e2e4}", [{"type": "no_wrapping"}], 1, "wrapped", id="wrap_brace"),
    pytest.param("e2e4", [{"type": "no_wrapping"}], 0, "", id="wrap_none"),
    # required_keys — present, missing, and non-JSON (a FINDING, not a skip)
    pytest.param(
        '{"total_conflicts": 3, "merged": 5}',
        [{"type": "required_keys", "keys": ["total_conflicts"]}],
        0,
        "",
        id="keys_present",
    ),
    pytest.param(
        '{"merged": 5}',
        [{"type": "required_keys", "keys": ["total_conflicts"]}],
        1,
        "",
        id="keys_missing",
    ),
    pytest.param(
        "not json at all",
        [{"type": "required_keys", "keys": ["x"]}],
        1,
        "",
        id="keys_non_json",
    ),
    # columns — CSV header
    pytest.param(
        "a,b,c\n1,2,3",
        [{"type": "columns", "columns": ["a", "c"]}],
        0,
        "",
        id="columns_present",
    ),
    pytest.param(
        "a,b\n1,2",
        [{"type": "columns", "columns": ["a", "d"]}],
        1,
        "",
        id="columns_missing",
    ),
    # fail-safe: the rung's OWN error (an invalid regex) is skipped, and the
    # other checks in the same list still apply.
    pytest.param(
        "[e2e4]",
        [{"type": "regex", "pattern": "[unterminated"}, {"type": "no_wrapping"}],
        1,
        "wrapped",
        id="failsafe_bad_regex_others_apply",
    ),
    pytest.param("x", [{"type": "made_up"}], 0, "", id="unknown_check_skipped"),
]


@pytest.mark.parametrize("content,checks,n_findings,must_contain", _FORMAT_CASES)
def test_apply_format_checks(content, checks, n_findings, must_contain):
    out = _apply_format_checks(content, checks)
    assert isinstance(out, list), f"expected a list of findings, got {type(out)}"
    assert len(out) == n_findings, f"expected {n_findings} finding(s), got {out}"
    if must_contain:
        assert any(must_contain in f for f in out), f"{must_contain!r} not in {out}"


# ── The rung: GATE / CHECK / APPEND / FAIL-SAFE ──────────────────────────────


@pytest.mark.asyncio
async def test_gate_skips_when_no_spec_or_no_checks():
    out = await action_check_output_format(_si(_mission(spec=None)))
    assert "validation_results" not in out.context_updates
    out2 = await action_check_output_format(
        _si(_mission(spec={"output_file": "/app/o", "checks": []}))
    )
    assert out2.result["format_eligible"] is False


@pytest.mark.asyncio
async def test_flags_wrapped_scalar_passes_bare():
    spec = {
        "output_file": "/app/move.txt",
        "checks": [{"type": "no_wrapping"}, {"type": "line_count", "value": 1}],
    }
    bad = await action_check_output_format(
        _si(_mission(spec), MockEffects(files={"/app/move.txt": "[e2e4]"}))
    )
    vr = bad.context_updates["validation_results"]
    assert len(vr) == 1 and vr[0]["required"] and not vr[0]["passed"]
    good = await action_check_output_format(
        _si(_mission(spec), MockEffects(files={"/app/move.txt": "e2e4"}))
    )
    assert good.result["format_passed"] is True
    assert good.context_updates["validation_results"] == []  # no false gate


@pytest.mark.asyncio
async def test_absent_file_flags_only_with_exists_check():
    # Wrong/absent output filename — flagged ONLY when the spec made the path a
    # requirement (catches the path-tracing close-miss); else deferred.
    spec_exists = {
        "output_file": "/app/reconstructed.ppm",
        "checks": [{"type": "exists"}],
    }
    out = await action_check_output_format(
        _si(_mission(spec_exists), MockEffects(files={}))
    )
    assert out.context_updates["validation_results"][0]["passed"] is False
    spec_no_exists = {
        "output_file": "/app/reconstructed.ppm",
        "checks": [{"type": "line_count", "value": 1}],
    }
    out2 = await action_check_output_format(
        _si(_mission(spec_no_exists), MockEffects(files={}))
    )
    assert out2.result["format_eligible"] is False  # deferred to existence check


@pytest.mark.asyncio
async def test_fail_safe_on_read_error_appends_nothing():
    class Boom(MockEffects):
        async def read_file(self, path):  # noqa: ARG002
            raise RuntimeError("io error")

    out = await action_check_output_format(
        _si(
            _mission({"output_file": "/app/o", "checks": [{"type": "no_wrapping"}]}),
            Boom(),
        )
    )
    assert "validation_results" not in out.context_updates
    assert out.result["format_eligible"] is False


# ── Replay the real v3 canary close-misses end-to-end ────────────────────────


@pytest.mark.asyncio
async def test_replay_v3_chess_and_multisource():
    chess = {
        "output_file": "/app/move.txt",
        "checks": [
            {"type": "no_wrapping"},
            {"type": "regex", "pattern": "^[a-h][1-8][a-h][1-8][qrbn]?$"},
        ],
    }
    out = await action_check_output_format(
        _si(_mission(chess), MockEffects(files={"/app/move.txt": "[e2e4]"}))
    )
    assert out.result["format_passed"] is False  # the bracket close-miss is caught

    ms = {
        "output_file": "/app/conflicts.json",
        "checks": [{"type": "required_keys", "keys": ["total_conflicts"]}],
    }
    out2 = await action_check_output_format(
        _si(_mission(ms), MockEffects(files={"/app/conflicts.json": '{"resolved": 4}'}))
    )
    assert out2.result["format_passed"] is False  # the missing-key close-miss is caught


# ── The store action: parse + conservatism ───────────────────────────────────


@pytest.mark.asyncio
async def test_store_filters_unknown_check_types():
    m = _mission()
    resp = (
        '{"output_file": "o", "checks": [{"type": "no_wrapping"}, {"type": "bogus"}]}'
    )
    await action_store_reground_output_format(_si(m, response=resp))
    spec = m.task_definition.output_format_spec
    assert len(spec["checks"]) == 1 and spec["checks"][0]["type"] == "no_wrapping"


@pytest.mark.asyncio
async def test_store_unparseable_leaves_no_spec():
    # Conservatism: an unparseable response stores no spec (no format gate) —
    # but still marks grounded (the format gate is optional; one-shot).
    m = _mission()
    await action_store_reground_output_format(_si(m, response="not json at all"))
    assert m.task_definition.output_format_spec is None
    assert m.task_definition.output_format_grounded is True


# ── Grounded derivation: gate + store ("reground" = historical name) ─────────


def _si_term(
    mission, terminal="$ ls\nresult.txt stub present\n", response=None
) -> StepInput:
    ctx = {"mission": mission, "terminal_output": terminal}
    if response is not None:
        ctx["inference_response"] = response
    return StepInput(
        context=ctx,
        params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"),
        effects=MockEffects(),
    )


@pytest.mark.asyncio
async def test_gate_fires_on_empty_spec():
    # No usable spec stored, not yet grounded → derive (fires on the first
    # cycle; the blind pre-exploration derivation no longer exists).
    out = await action_gate_reground_output_format(_si_term(_mission(spec=None)))
    assert out.result["needs_reground"] is True
    # A spec with no checks is "empty" too.
    out2 = await action_gate_reground_output_format(
        _si_term(_mission(spec={"output_file": "o", "checks": []}))
    )
    assert out2.result["needs_reground"] is True


@pytest.mark.asyncio
async def test_gate_fires_even_without_terminal_output():
    # Regression for the inert-port canary: terminal_output is NOT reliably a truthy
    # string at the gate's position in the live ops_task flow. The gate must STILL
    # fire (it is structurally post-scan/session; the reground grounds from
    # project_manifest, not terminal_output). The old gate wrongly required a truthy
    # terminal_output here and so never fired in production.
    no_term = StepInput(
        context={"mission": _mission(spec=None)},
        params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"),
        effects=MockEffects(),
    )
    assert (await action_gate_reground_output_format(no_term)).result[
        "needs_reground"
    ] is True


@pytest.mark.asyncio
async def test_gate_skips_only_when_usable_spec_or_grounded():
    # A usable stored spec is never re-derived (zero cost).
    usable = {"output_file": "/app/o", "checks": [{"type": "exists"}]}
    out = await action_gate_reground_output_format(_si_term(_mission(spec=usable)))
    assert out.result["needs_reground"] is False
    # Already grounded → one-shot, never fires twice.
    m = _mission(spec=None)
    m.task_definition.output_format_grounded = True
    assert (await action_gate_reground_output_format(_si_term(m))).result[
        "needs_reground"
    ] is False


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
    # One-shot: if the derivation finds nothing, mark grounded so it won't re-fire
    # (and leave the existing spec untouched — stays conservative, no gate).
    m = _mission(spec=None)
    out = await action_store_reground_output_format(
        _si_term(m, response='{"checks": []}')
    )
    assert out.result["format_check_count"] == 0
    assert m.task_definition.output_format_spec is None
    assert m.task_definition.output_format_grounded is True
    # And once grounded, the gate no longer fires (no per-cycle inference leak).
    assert (await action_gate_reground_output_format(_si_term(m))).result[
        "needs_reground"
    ] is False


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
        context={
            "mission": m,
            "raw_search_results": [
                {
                    "url": "http://x",
                    "content": "use sqlite .recover to rebuild a truncated db",
                }
            ],
        },
        params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"),
        effects=MockEffects(),
    )
    out = await action_store_search_findings(si)
    assert out.result["n_hits"] == 1
    assert "sqlite .recover" in m.task_definition.search_findings
    # empty hits → sentinel so the gate stops re-searching (one-shot).
    m2 = _mission()
    si2 = StepInput(
        context={"mission": m2, "raw_search_results": []},
        params={},
        meta=FlowMeta(flow_name="ops_task", step_id="x"),
        effects=MockEffects(),
    )
    await action_store_search_findings(si2)
    assert m2.task_definition.search_findings.startswith("(no relevant")


# ── Grounded completion-criteria derivation: gate + tighten-only store ───────


@pytest.mark.asyncio
async def test_gate_reground_criteria_fires_until_grounded():
    m = _mission()
    assert (await action_gate_reground_criteria(_si(m))).result[
        "needs_reground"
    ] is True
    m.task_definition.completion_criteria_grounded = True
    assert (await action_gate_reground_criteria(_si(m))).result[
        "needs_reground"
    ] is False


@pytest.mark.asyncio
async def test_store_reground_criteria_merges_tighten_only():
    m = _mission(
        criteria=[{"command": "test -f /app/a", "name": "a", "required": True}]
    )
    resp = (
        '```json\n{"checks": ['
        '{"command": "test -s /app/result.txt", "description": "result exists"}, '
        '{"command": "test -f /app/a", "description": "dup"}]}\n```'
    )
    await action_store_reground_criteria(_si(m, response=resp))
    cmds = [c["command"] for c in m.task_definition.completion_criteria]
    assert "test -f /app/a" in cmds  # prior check preserved (tighten-only)
    assert "test -s /app/result.txt" in cmds  # grounded check added
    assert len(cmds) == 2  # the dup was not re-added
    assert m.task_definition.completion_criteria_grounded is True


# ── Tier 3 derive-guard: reject action-JSON emitted in place of a spec/checks ──


def test_derive_guard_rejects_action_json():
    from agent.actions.operations_actions import (
        _parse_output_format_spec,
        _parse_completion_criteria,
    )

    # Output-format: an action/command emitted instead of a spec → None.
    assert _parse_output_format_spec('{"action": "list_files", "path": ""}') is None
    assert _parse_output_format_spec('{"action": "none"}') is None
    # A real spec still parses.
    assert _parse_output_format_spec(
        '{"output_file": "/app/o", "checks": [{"type": "exists"}]}'
    )
    # Criteria: an action instead of checks → [].
    assert _parse_completion_criteria('{"action": "run", "path": "."}') == []
    # A real checks array still parses.
    assert _parse_completion_criteria(
        '{"checks": [{"command": "test -s /app/x", "description": "x"}]}'
    )
