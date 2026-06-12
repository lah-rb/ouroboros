"""Extractor flow set actions: worklist, quality policy, gate verdict.

Scraper v2 stage two. Every action is deterministic; the quality
thresholds were calibrated on live corpus extractions (faithful band:
numeric 0.89-0.95, span 0.83-0.88 — thresholds sit below it).
"""

from __future__ import annotations

import json

import pytest

from agent.actions.extraction_actions import (
    EXTRACT_BATCH_SIZE,
    action_check_extraction_complete,
    action_derive_extraction_goals,
    action_extract_pdf_batch,
    action_pdf_extract_sweep_next,
    action_reopen_extraction_goal,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput
from agent.persistence.models import GoalRecord, MissionConfig, MissionState


def _bank_line(key, status="", access="oa_pdf", pdf=True):
    return json.dumps(
        {
            "paper_key": key,
            "access_status": access,
            "pdf_path": f"pdfs/{key}.pdf" if pdf else "",
            "extraction_status": status,
            "title": key,
        }
    )


def _mission(goals=None) -> MissionState:
    return MissionState(
        objective="extract",
        status="active",
        config=MissionConfig(working_directory="/tmp/x", flow_set="extractor"),
        goals=goals or [],
    )


def _goal(status="incomplete") -> GoalRecord:
    return GoalRecord(
        description="extract corpus",
        type="pdf_extract",
        status=status,
        finding_signature="corpus-pdf-extract",
    )


def _si(context=None, inputs=None, effects=None) -> StepInput:
    return StepInput(
        context=context or {},
        inputs=inputs or {},
        params={},
        meta=FlowMeta(flow_name="extract_control", step_id="t"),
        effects=effects,
    )


def _fx(bank_lines, mission=None, commands=None) -> MockEffects:
    return MockEffects(
        files={"databank/papers.jsonl": "\n".join(bank_lines) + "\n"},
        mission=mission,
        commands=commands or {},
    )


# ── goal derivation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_derive_creates_single_goal_idempotently():
    mission = _mission()
    fx = _fx([_bank_line("a"), _bank_line("b")], mission=mission)
    out = await action_derive_extraction_goals(_si({"mission": mission}, effects=fx))
    assert out.result["goals_ready"] is True and out.result["created"] == 1
    out2 = await action_derive_extraction_goals(_si({"mission": mission}, effects=fx))
    assert out2.result["created"] == 0
    assert sum(1 for g in mission.goals if g.type == "pdf_extract") == 1


@pytest.mark.asyncio
async def test_derive_nothing_to_do_without_oa_pdfs():
    mission = _mission()
    fx = _fx([_bank_line("a", access="closed", pdf=False)], mission=mission)
    out = await action_derive_extraction_goals(_si({"mission": mission}, effects=fx))
    assert out.result["goals_ready"] is False
    assert not mission.goals


# ── sweep ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sweep_batches_and_prioritizes_retries():
    mission = _mission([_goal()])
    lines = [_bank_line(f"p{i}") for i in range(5)]
    lines.append(_bank_line("retry_me", status="needs_reextract"))
    fx = _fx(lines, mission=mission)
    out = await action_pdf_extract_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result["needs_extract"] is True
    batch = out.context_updates["dispatch_config"]["paper_keys"]
    assert len(batch) == EXTRACT_BATCH_SIZE
    assert batch[0] == "retry_me"


@pytest.mark.asyncio
async def test_sweep_skips_terminal_and_non_oa():
    mission = _mission([_goal()])
    fx = _fx(
        [
            _bank_line("done", status="extracted"),
            _bank_line("dead", status="extract_failed"),
            _bank_line("closed1", access="closed", pdf=False),
        ],
        mission=mission,
    )
    out = await action_pdf_extract_sweep_next(_si({"mission": mission}, effects=fx))
    assert out.result.get("sweep_complete") is True
    assert mission.goals[0].status == "complete"


# ── batch quality policy ──────────────────────────────────────────────


def _report(key, num=0.92, span=0.85, error=""):
    return json.dumps(
        {
            "paper_key": key,
            "md_path": f"markdown/{key}.md",
            "pages": 10,
            "verified_pages": 9,
            "unverified_pages": 1,
            "numeric_match_rate": num,
            "span_pass_rate": span,
            "figures_kept": 4,
            "figures_dropped": 2,
            "seconds": 70.0,
            "error": error,
        }
    )


def _batch_inputs(keys):
    return {
        "paper_keys": keys,
        "working_directory": "/tmp/x",
        "mission_id": "m",
        "goal_id": "g",
        "flow_directive": "extract",
    }


@pytest.mark.asyncio
async def test_batch_pass_marks_extracted_with_quality_fields():
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("good")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = CommandResult(
        return_code=0, stdout=_report("good"), stderr="", command="x"
    )
    out = await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["good"]), effects=fx)
    )
    assert out.result["status"] == "success"
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    rec = bank["good"]
    assert rec["extraction_status"] == "extracted"
    assert rec["md_path"] == "databank/markdown/good.md"
    assert rec["figure_count"] == 4
    assert rec["extraction_quality"]["numeric_match_rate"] == 0.92


@pytest.mark.asyncio
async def test_batch_below_threshold_retries_then_fails():
    # First attempt: below numeric threshold → needs_reextract.
    fx = _fx([_bank_line("shaky")])
    cmd = CommandResult(
        return_code=0, stdout=_report("shaky", num=0.5), stderr="", command="x"
    )
    import os
    from agent.actions import extraction_actions as ea

    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    fx._commands[tool] = cmd
    out = await action_extract_pdf_batch(
        _si(inputs=_batch_inputs(["shaky"]), effects=fx)
    )
    assert out.result["status"] == "failed"
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["shaky"]["extraction_status"] == "needs_reextract"

    # Second attempt (still bad) → terminal extract_failed, flagged.
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["shaky"]), effects=fx))
    bank = await read_databank(fx)
    assert bank["shaky"]["extraction_status"] == "extract_failed"
    assert "below quality threshold" in bank["shaky"]["failure_reason"]


@pytest.mark.asyncio
async def test_batch_tool_error_and_missing_report():
    import os
    from agent.actions import extraction_actions as ea

    fx = _fx([_bank_line("a"), _bank_line("b")])
    tool = os.path.join(ea._repo_root(), ea._TOOL_PY)
    # 'a' errors in-tool; 'b' produces no report line at all.
    fx._commands[tool] = CommandResult(
        return_code=0,
        stdout=_report("a", error="MLXError: boom"),
        stderr="",
        command="x",
    )
    await action_extract_pdf_batch(_si(inputs=_batch_inputs(["a", "b"]), effects=fx))
    from agent.actions.scholarly_actions import read_databank

    bank = await read_databank(fx)
    assert bank["a"]["extraction_status"] == "needs_reextract"
    assert "MLXError" in bank["a"]["failure_reason"]
    assert bank["b"]["extraction_status"] == "needs_reextract"
    assert "no report" in bank["b"]["failure_reason"]


# ── gate ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_passes_when_all_terminal():
    fx = _fx(
        [
            _bank_line("x", status="extracted"),
            _bank_line("y", status="extract_failed"),
            _bank_line("z", access="closed", pdf=False),
        ]
    )
    out = await action_check_extraction_complete(_si(effects=fx))
    assert out.result["gate_passed"] is True


@pytest.mark.asyncio
async def test_gate_fails_with_pending_then_reopen():
    fx = _fx([_bank_line("x", status="extracted"), _bank_line("pend")])
    out = await action_check_extraction_complete(_si(effects=fx))
    assert out.result["gate_passed"] is False
    assert out.context_updates["pending_extractions"] == ["pend"]

    mission = _mission([_goal(status="complete")])
    out2 = await action_reopen_extraction_goal(
        _si({"mission": mission}, effects=MockEffects(mission=mission))
    )
    assert out2.result["reopened"] is True
    assert mission.goals[0].status == "incomplete"
