"""Extractor flow set actions — PDF → markdown+figures over a databank.

Scraper v2, stage two of the corpus pipeline (scrape → extract →
dataset). Operates on an EXISTING databank produced by a scraper
mission; every action here is deterministic — the set contains zero
LLM turns. The OCR work happens in the isolated tools/pdf_extract
toolchain (own venv, own mlx server child, one process per dispatch);
these actions are the policy layer: worklists, quality thresholds,
record bookkeeping, and the gate verdict.

Record fields owned by this stage. They are written to the sidecar
databank/extraction.jsonl (last-wins) and overlaid onto the scraper's
papers.jsonl by read_databank, so this stage never writes that file:
  extraction_status: "" | "needs_reextract" | "extracted" | "extract_failed"
  md_path, figure_count, extraction_method, extraction_quality{...}
"""

from __future__ import annotations

import json
import logging
import os

from agent.models import StepInput, StepOutput
from agent.paths import repo_root as _repo_root

logger = logging.getLogger(__name__)

EXTRACT_BATCH_SIZE = 3  # ~33 pages/paper × ~7s/page ≈ 12 min/dispatch
EXTRACT_TIMEOUT_S = 1800

# Quality policy, calibrated on live corpus extractions (milestone 1):
# faithful papers measured numeric 0.89-0.95 and span 0.83-0.88
# (corpus-weighted, truth-recall direction); broken extractions score
# near zero. Thresholds sit below the faithful band to avoid false
# flags while still catching real failures.
MIN_NUMERIC_RATE = 0.85
MIN_SPAN_RATE = 0.75

EXTRACTION_METHOD = "paddleocr-vl-1.6-mlx-8bit"
CORPUS_GOAL_SIGNATURE = "corpus-pdf-extract"

_TOOL_PY = "tools/pdf_extract/.venv/bin/python"
_TOOL_SCRIPT = "tools/pdf_extract/extract_batch.py"
_TOOL_MODEL = "tools/pdf_extract/models/PaddleOCR-VL-1.6-MLX-8bit"


def _extraction_pending(record: dict) -> bool:
    """A record this stage still owes work to."""
    return (
        record.get("access_status") == "oa_pdf"
        and bool(record.get("pdf_path"))
        and record.get("extraction_status") not in ("extracted", "extract_failed")
    )


async def action_derive_extraction_goals(step_input: StepInput) -> StepOutput:
    """Bootstrap the single corpus-level pdf_extract goal (idempotent).

    Mirrors derive_research_goals' signature-idempotency: one goal,
    created only when the databank holds at least one OA PDF and no
    goal with the corpus signature exists. No planning turn — the
    databank itself is the plan.
    """
    from agent.actions.scholarly_actions import read_databank
    from agent.persistence.models import GoalRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or not effects:
        return StepOutput(
            result={"goals_ready": False},
            observations="No mission/effects — cannot derive extraction goals",
        )

    if any(g.finding_signature == CORPUS_GOAL_SIGNATURE for g in mission.goals):
        return StepOutput(
            result={"goals_ready": True, "created": 0},
            observations="Extraction goal already present",
        )

    databank = await read_databank(effects)
    eligible = sum(1 for r in databank.values() if _extraction_pending(r))
    already_done = sum(
        1
        for r in databank.values()
        if r.get("extraction_status") in ("extracted", "extract_failed")
    )
    if not eligible and not already_done:
        return StepOutput(
            result={"goals_ready": False, "created": 0},
            observations=(
                "Databank has no OA PDFs to extract — nothing for this "
                "flow set to do (run a scraper mission first?)"
            ),
        )

    mission.goals.append(
        GoalRecord(
            description=(
                f"Extract markdown + figures from all OA PDFs in the "
                f"databank ({eligible} pending)"
            ),
            type="pdf_extract",
            status="incomplete",
            finding_signature=CORPUS_GOAL_SIGNATURE,
        )
    )
    await effects.save_mission(mission)
    return StepOutput(
        result={"goals_ready": True, "created": 1},
        observations=f"Derived corpus extraction goal ({eligible} PDFs pending)",
    )


async def action_pdf_extract_sweep_next(step_input: StepInput) -> StepOutput:
    """Dispatch the next extraction batch from the databank worklist.

    needs_reextract records (one prior below-threshold attempt) take
    priority over fresh ones. Empty worklist completes the corpus goal.
    Mirrors catalog_sweep_next.
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission or not effects:
        return StepOutput(
            result={"sweep_complete": True}, observations="No mission/effects"
        )

    goal = next(
        (
            g
            for g in mission.goals
            if g.type == "pdf_extract" and g.status == "incomplete"
        ),
        None,
    )
    if goal is None:
        return StepOutput(
            result={"sweep_complete": True},
            observations="No incomplete pdf_extract goal — sweep complete",
        )

    databank = await read_databank(effects)
    retry = sorted(
        k
        for k, r in databank.items()
        if _extraction_pending(r) and r.get("extraction_status") == "needs_reextract"
    )
    fresh = sorted(
        k
        for k, r in databank.items()
        if _extraction_pending(r) and not r.get("extraction_status")
    )
    batch = (retry + fresh)[:EXTRACT_BATCH_SIZE]

    if not batch:
        goal.status = "complete"
        await effects.save_mission(mission)
        return StepOutput(
            result={"sweep_complete": True},
            observations="Extraction worklist empty — corpus goal complete",
        )

    dispatch_config = {
        "goal_id": goal.id,
        "paper_keys": batch,
        "flow_directive": (
            f"Extract markdown and figures from {len(batch)} PDF(s): "
            + ", ".join(batch)
        ),
    }
    remaining = len(retry) + len(fresh)
    return StepOutput(
        result={"needs_extract": True},
        observations=(
            f"Extraction sweep: dispatching {len(batch)} of {remaining} "
            f"pending paper(s)" + (f" ({len(retry)} retries first)" if retry else "")
        ),
        context_updates={"dispatch_config": dispatch_config},
    )


async def action_extract_pdf_batch(step_input: StepInput) -> StepOutput:
    """Run the toolchain over one batch and apply the quality policy.

    The tool computes metrics; THIS action judges. Per paper:
      pass (numeric ≥ MIN_NUMERIC_RATE and span ≥ MIN_SPAN_RATE)
        → extracted (+ md_path/figure_count/quality/method fields)
      below threshold or tool error
        → needs_reextract on the first attempt, extract_failed after
          (flagged, never silently included in the corpus).
    """
    from agent.actions.scholarly_actions import (
        append_extraction_records,
        read_databank,
    )

    effects = step_input.effects
    keys = list(step_input.inputs.get("paper_keys") or [])
    working_dir = str(step_input.inputs.get("working_directory") or "")
    if not effects or not keys:
        return StepOutput(
            result={"status": "failed"},
            observations="No effects or empty batch",
            context_updates={
                "directive_report": {
                    "flow": "extract_pdfs",
                    "status": "failed",
                    "summary": "Empty extraction batch",
                }
            },
        )

    databank = await read_databank(effects)
    pdfs, resolved_keys = [], []
    for k in keys:
        rec = databank.get(k) or {}
        rel = rec.get("pdf_path") or ""
        if rel:
            pdfs.append(os.path.join(working_dir, rel))
            resolved_keys.append(k)

    root = _repo_root()
    cmd = [
        os.path.join(root, _TOOL_PY),
        os.path.join(root, _TOOL_SCRIPT),
        "--pdfs",
        *pdfs,
        "--keys",
        *resolved_keys,
        "--databank-dir",
        os.path.join(working_dir, "databank"),
        "--model",
        os.path.join(root, _TOOL_MODEL),
    ]
    result = await effects.run_command(cmd, timeout=EXTRACT_TIMEOUT_S)

    reports: dict[str, dict] = {}
    for line in (result.stdout or "").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("paper_key"):
            reports[r["paper_key"]] = r

    updates: list[dict] = []
    extracted = retried = failed = 0
    for k in resolved_keys:
        rec = dict(databank.get(k) or {"paper_key": k})
        rep = reports.get(k)
        prior_retry = rec.get("extraction_status") == "needs_reextract"
        ok = (
            rep is not None
            and not rep.get("error")
            # ZERO verified pages passes the rate thresholds vacuously
            # (nothing checkable -> nothing missed). Live: a JPEG served
            # as the "PDF" produced a 1-page, 0-verified, 276-byte
            # markdown that scored 1.00/1.00. Unverifiable output is a
            # claim we refuse, not one we wave through.
            and rep.get("verified_pages", 0) > 0
            and rep.get("numeric_match_rate", 0) >= MIN_NUMERIC_RATE
            and rep.get("span_pass_rate", 0) >= MIN_SPAN_RATE
        )
        if ok:
            rec["extraction_status"] = "extracted"
            rec["md_path"] = os.path.join("databank", rep["md_path"])
            rec["figure_count"] = rep.get("figures_kept", 0)
            rec["extraction_method"] = EXTRACTION_METHOD
            rec["extraction_quality"] = {
                "numeric_match_rate": round(rep.get("numeric_match_rate", 0), 4),
                "span_pass_rate": round(rep.get("span_pass_rate", 0), 4),
                "verified_pages": rep.get("verified_pages", 0),
                "unverified_pages": rep.get("unverified_pages", 0),
                "pages": rep.get("pages", 0),
                "seconds": rep.get("seconds", 0),
            }
            extracted += 1
        else:
            reason = (
                rep.get("error")
                if rep and rep.get("error")
                else (
                    "below quality threshold "
                    f"(numeric={rep.get('numeric_match_rate', 0):.2f}, "
                    f"span={rep.get('span_pass_rate', 0):.2f})"
                    if rep
                    else "no report from toolchain"
                    + (" (command timed out)" if result.timed_out else "")
                )
            )
            if prior_retry:
                rec["extraction_status"] = "extract_failed"
                rec["failure_reason"] = f"extraction: {reason}"
                failed += 1
            else:
                rec["extraction_status"] = "needs_reextract"
                rec["failure_reason"] = f"extraction (will retry): {reason}"
                retried += 1
        updates.append(rec)

    # SIDECAR, not papers.jsonl — the scraper owns that file and both
    # writers do a whole-file read-modify-write, so sharing it loses
    # appends. Disjoint files let acquisition and OCR run at once.
    await append_extraction_records(effects, updates)

    status = "success" if extracted == len(resolved_keys) else "partial"
    if extracted == 0:
        status = "failed"
    summary = (
        f"Extracted {extracted}/{len(resolved_keys)} paper(s)"
        + (f", {retried} queued for retry" if retried else "")
        + (f", {failed} failed terminally" if failed else "")
    )
    return StepOutput(
        result={"status": status},
        observations=summary,
        context_updates={
            "directive_report": {
                "flow": "extract_pdfs",
                "status": status,
                "summary": summary,
                "headline": summary[:80],
            }
        },
    )


async def action_check_extraction_complete(step_input: StepInput) -> StepOutput:
    """Gate check: every OA-PDF record reached a terminal state.

    Derived verdict (same doctrine as check_aspect_coverage): pass iff
    no record is still pending. Failure returns the pending list so the
    gate-fail path can reopen the corpus goal.
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    if not effects:
        return StepOutput(result={"gate_passed": False}, observations="No effects")
    databank = await read_databank(effects)
    pending = sorted(k for k, r in databank.items() if _extraction_pending(r))
    extracted = sum(
        1 for r in databank.values() if r.get("extraction_status") == "extracted"
    )
    failed = sum(
        1 for r in databank.values() if r.get("extraction_status") == "extract_failed"
    )
    if pending:
        return StepOutput(
            result={"gate_passed": False},
            observations=(
                f"Extraction gate: {len(pending)} paper(s) still pending — "
                + ", ".join(pending[:5])
            ),
            context_updates={"pending_extractions": pending},
        )
    return StepOutput(
        result={"gate_passed": True},
        observations=(
            f"Extraction gate PASSED: {extracted} extracted, "
            f"{failed} flagged extract_failed, 0 pending"
        ),
    )


async def action_reopen_extraction_goal(step_input: StepInput) -> StepOutput:
    """Gate-fail path: reopen the corpus goal so the sweep resumes."""
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"reopened": False}, observations="No mission")
    reopened = 0
    for g in mission.goals:
        if g.type == "pdf_extract" and g.status == "complete":
            g.status = "incomplete"
            reopened += 1
    if effects and reopened:
        await effects.save_mission(mission)
    return StepOutput(
        result={"reopened": bool(reopened)},
        observations=f"Reopened {reopened} extraction goal(s) after gate failure",
    )
