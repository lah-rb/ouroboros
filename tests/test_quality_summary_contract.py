"""Summarize finding contract: repro/expected normalization + verification gating.

Verify-before-harvest treats gate findings as claims: each functional
blocking issue carries a ``repro`` (stdin lines to demonstrate it) and an
``expected`` sentence. ``_parse_quality_summary`` normalizes whatever the
model emitted into ``list[str]`` repro / bounded ``expected`` so the
verification loop never sees a malformed shape, and
``action_apply_quality_gate_results`` publishes ``has_findings`` (the
verification-loop routing signal) and defers note-pushing to
``apply_verification_results`` in completion mode so refuted claims never
land in the mission record.
"""

from __future__ import annotations

import pytest

from agent.actions.refinement_actions import (
    _normalize_repro,
    _parse_quality_summary,
    action_apply_quality_gate_results,
)
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput


def _summary_json(*issues: str) -> str:
    body = ",".join(issues)
    return f'```json\n{{"verdict": "fail", "blocking_issues": [{body}], "summary": "s"}}\n```'


def test_repro_and_expected_normalized_on_dict_issues():
    raw = _summary_json(
        '{"issue": "use has no effect", "class": "functional",'
        ' "repro": [" take lantern ", "", "use lantern"],'
        ' "expected": "lantern lights the room"}'
    )
    task = _parse_quality_summary(raw)["fix_tasks"][0]
    assert task["repro"] == ["take lantern", "use lantern"]
    assert task["expected"] == "lantern lights the room"


def test_repro_string_becomes_single_line_list():
    raw = _summary_json(
        '{"issue": "x", "class": "functional", "repro": "take lantern"}'
    )
    assert _parse_quality_summary(raw)["fix_tasks"][0]["repro"] == ["take lantern"]


def test_repro_non_list_non_string_drops_to_empty():
    raw = _summary_json('{"issue": "x", "class": "functional", "repro": 7}')
    task = _parse_quality_summary(raw)["fix_tasks"][0]
    assert task["repro"] == []
    assert task["expected"] == ""


def test_repro_truncated_to_cap():
    lines = ", ".join(f'"cmd{i}"' for i in range(15))
    raw = _summary_json(f'{{"issue": "x", "class": "functional", "repro": [{lines}]}}')
    repro = _parse_quality_summary(raw)["fix_tasks"][0]["repro"]
    assert len(repro) == 10
    assert repro[0] == "cmd0" and repro[-1] == "cmd9"


def test_expected_bounded_to_300_chars():
    raw = _summary_json(
        f'{{"issue": "x", "class": "functional", "expected": "{"e" * 400}"}}'
    )
    assert len(_parse_quality_summary(raw)["fix_tasks"][0]["expected"]) == 300


def test_legacy_string_issue_gets_empty_repro_fields():
    raw = '```json\n{"verdict": "fail", "blocking_issues": ["it broke"], "summary": "s"}\n```'
    task = _parse_quality_summary(raw)["fix_tasks"][0]
    assert task["repro"] == []
    assert task["expected"] == ""
    assert task["class"] == "functional"


def test_normalize_repro_handles_none():
    assert _normalize_repro(None) == []


def _si(raw: str, mode: str) -> StepInput:
    return StepInput(
        context={"inference_response": raw, "validation_results": []},
        params={},
        inputs={"mode": mode},
        meta=FlowMeta(flow_name="quality_gate", step_id="evaluate_results"),
        effects=MockEffects(),
    )


_FAIL_RAW = _summary_json(
    '{"issue": "use has no effect", "class": "functional", "repro": ["use x"]}'
)


@pytest.mark.asyncio
async def test_has_findings_published_and_notes_deferred_in_completion_mode():
    si = _si(_FAIL_RAW, mode="completion")
    out = await action_apply_quality_gate_results(si)
    assert out.result["has_findings"] is True
    assert out.result["all_passing"] is False
    # Notes move to apply_verification_results — refuted claims must
    # never be recorded as failure_analysis.
    assert si.effects.call_count("push_note") == 0


@pytest.mark.asyncio
async def test_checkpoint_mode_keeps_note_pushing():
    si = _si(_FAIL_RAW, mode="checkpoint")
    out = await action_apply_quality_gate_results(si)
    assert out.result["has_findings"] is True
    assert si.effects.call_count("push_note") == 1


@pytest.mark.asyncio
async def test_no_findings_means_has_findings_false():
    raw = '```json\n{"verdict": "pass", "blocking_issues": [], "summary": "ok"}\n```'
    out = await action_apply_quality_gate_results(_si(raw, mode="completion"))
    assert out.result["has_findings"] is False
    assert out.result["all_passing"] is True
