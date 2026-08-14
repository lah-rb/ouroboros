"""Extractor flow set actions — PDF → markdown+figures over a databank.

Scraper v2, stage two of the corpus pipeline (scrape → extract →
dataset). Operates on an EXISTING databank produced by a scraper
mission; every action here is deterministic — the set contains zero
LLM turns. The OCR work happens in the isolated tools/pdf_extract
toolchain (own venv, own VLM server child, one process per dispatch);
these actions are the policy layer: worklists, quality thresholds,
record bookkeeping, and the gate verdict.

Record fields owned by this stage. They are written to the sidecar
databank/extraction.jsonl (last-wins) and overlaid onto the scraper's
papers.jsonl by read_databank, so this stage never writes that file:
  extraction_status: "" | "needs_reextract" | "extracted" | "extract_failed"
                     | "extract_unverified"  (OCR ran, no text layer to check
                       it against. The status is NOT rewritten when such a
                       paper is later accepted — it is the only record that
                       the paper was admitted on curator judgement rather than
                       machine verification, and an audit needs it.)
                     | "extract_oversize"    (a book: referred for a human
                       decision instead of attempted, because a dispatch
                       shares one timeout across its batch)
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

# Statuses this stage will not revisit. extract_unverified belongs here:
# another OCR pass over a scan with no text layer yields the same
# unverifiable result, so re-queuing it burns GPU forever.
_TERMINAL_EXTRACTION = (
    "extracted",
    "extract_failed",
    "extract_unverified",
    # A book, referred for a human decision rather than attempted. Terminal
    # here so the sweep stops offering it; it is a REVIEW QUEUE, not a
    # rejection — see the oversize branch below.
    "extract_oversize",
)

# QUALITY POLICY — recalibrated 2026-08-14 against blind judgement.
#
# 48 extractions (29 the gate rejected, 19 it passed, shuffled and
# de-identified) were tiered by independent judges reading each markdown
# against its source PDF, 10 spot-checks apiece. The result overturned the
# previous calibration: the rate thresholds had been fitted to a "faithful
# band" of 0.89-0.95 that was itself an artifact of two measurement bugs in
# the truth oracle (margin line numbers, publisher furniture — both fixed in
# extract_batch._prose_text). With the oracle corrected, the numeric rate
# correlates with judged quality at r = +0.02. It does not rank quality and no
# threshold on it can.
#
# So the rates are now a FLOOR AGAINST CATASTROPHE, not a quality bar, and sit
# where they cost nothing: at 0.75/0.70 every paper judged fit to train is
# admitted, while the four worst documents in the sample are still caught. The
# old 0.85/0.75 rejected 42% of trainable papers, including three of the five
# judged fully faithful.
#
# Full record: dev/EXTRACTION_GATE_CALIBRATION_2026-08-14.md
MIN_NUMERIC_RATE = 0.75
MIN_SPAN_RATE = 0.70

# DEGENERATE DECODE. The rates are RECALL — "does this number appear anywhere
# on the page" — so a decode that falls into a loop still scores clean while
# the document is ruined. This catches what they cannot see. The longest
# back-to-back repeat across all 19 papers judged fit to train was 19 words;
# the two degenerate documents scored 675 and 1598, and the worst non-
# degenerate defect scored 107. The cut sits in that empty gap.
MAX_REPEAT_WORDS = 200

# TRUNCATED ACQUISITION. A PDF holding at most this fraction of the article's
# published page extent is a fragment, not the paper. Half rather than any
# shortfall, because a publisher asset legitimately differs from the print
# extent by a page.
_TRUNCATED_FRACTION = 0.5

CORPUS_GOAL_SIGNATURE = "corpus-pdf-extract"

_TOOL_PY = "tools/pdf_extract/.venv/bin/python"
_TOOL_SCRIPT = "tools/pdf_extract/extract_batch.py"

# WHICH ENGINE READ THE PAGE IS PROVENANCE, so extraction_method carries the
# backend rather than a constant.
#
# THE FLEET SERVER IS THE DEFAULT (2026-08-13). paddle is held hot inside
# LLMVP as a Phase 2b secondary and reached over its endpoint, instead of the
# tool spawning a private llama-server per batch.
#
# THE REASON IS NOT SPEED, and pretending otherwise would misdirect the next
# person to profile this. The A/B on two papers came out a WASH: verification
# rates identical to sixteen decimal places on both papers, paper time +4.6%
# for the resident path, wall +1.0% once the ~6.5s spawn it no longer pays is
# netted out — inside the noise. What the move buys is that model choice
# lives in LLMVP's config rather than a constant in a tool, the OCR stage is
# visible to the fleet's model management and telemetry, Ouroboros stops
# owning a server lifecycle, and the stage travels with the fleet to CUDA.
# Full record: dev/PARALLEL_LANES_2026-08-13.md §7e.
#
# `llamacpp` spawns the private server exactly as before and is the fallback
# when no fleet server is running; `mlx` remains the station-dependent
# opt-in. Records written earlier keep their own method string — they WERE
# extracted that way, and the field is only ever written, never filtered on.
_VL_BACKEND = os.environ.get("OUROBOROS_VL_BACKEND", "llmvp")
_EXTRACTION_METHODS = {
    # Same weights, same quant, three ways of reaching them — the suffix says
    # which, because that is the part a later audit cannot reconstruct.
    "llmvp": "paddleocr-vl-1.6-q8_0-llmvp",
    "llamacpp": "paddleocr-vl-1.6-q8_0-llamacpp",
    "mlx": "paddleocr-vl-1.6-mlx-8bit",
}
EXTRACTION_METHOD = _EXTRACTION_METHODS.get(
    _VL_BACKEND, f"paddleocr-vl-1.6-{_VL_BACKEND}"
)


def expected_page_extent(record: dict) -> int:
    """Pages the PUBLISHER says this article runs to, or 0 if unknowable.

    OpenAlex `biblio` gives first/last page as free-form strings: roman
    numerals for front matter, "S17" for supplements, "e01505" for article
    numbers that are not pages at all. Only a clean numeric pair yields an
    extent; everything else returns 0, which every caller must read as "no
    opinion" rather than "zero pages".
    """
    first, last = record.get("first_page") or "", record.get("last_page") or ""
    if not (first.strip().isdigit() and last.strip().isdigit()):
        return 0
    extent = int(last) - int(first) + 1
    # A negative or absurd span means the deposit is wrong, not the PDF.
    return extent if 1 <= extent <= 200 else 0


def acquisition_is_truncated(record: dict, pdf_pages: int) -> bool:
    """The PDF holds materially less than the article it claims to be.

    THE DEFECT NO QUALITY METRIC CAN SEE. Verification compares our markdown
    against the PDF's own text layer, so an extraction of page 1 of a 5-page
    article is *perfectly faithful* — to a fragment. One corpus paper is
    exactly this: a single-page PDF of an article running pages 37-41, which
    scored 0.88/1.00 and was judged unusable by a human on sight.

    Only the publisher's own page extent can catch it, and only when the
    publisher deposited one, so this is a targeted check and not a general
    one: it fires only on a clean numeric extent and a real shortfall.
    """
    expected = expected_page_extent(record)
    if not expected or pdf_pages <= 0:
        return False
    # Half. Not "any shortfall": a PDF legitimately differs from the print
    # extent — a final page carrying only references may be dropped by the
    # publisher's own asset, and offprints re-paginate. Losing half or more
    # of an article is not that.
    return pdf_pages <= expected * _TRUNCATED_FRACTION


def _extraction_pending(record: dict) -> bool:
    """A record this stage still owes work to."""
    return (
        record.get("access_status") == "oa_pdf"
        and bool(record.get("pdf_path"))
        # extract_unverified is TERMINAL for this stage. Re-running OCR on a
        # paper with no text layer produces the same unverifiable output and
        # the same refusal — it needs a human, not another pass.
        and record.get("extraction_status") not in _TERMINAL_EXTRACTION
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
        if r.get("extraction_status") in _TERMINAL_EXTRACTION
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
        "--vl-backend",
        _VL_BACKEND,
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
        truncated = bool(rep) and acquisition_is_truncated(rec, rep.get("pages", 0))
        oversize = bool(rep) and bool(rep.get("oversize"))
        ok = (
            rep is not None
            and not rep.get("error")
            and not oversize
            # The asset is a fragment of the article. The extraction may be
            # flawless and still must not enter the corpus.
            and not truncated
            # ZERO verified pages passes the rate thresholds vacuously
            # (nothing checkable -> nothing missed). Live: a JPEG served
            # as the "PDF" produced a 1-page, 0-verified, 276-byte
            # markdown that scored 1.00/1.00. Unverifiable output is a
            # claim we refuse, not one we wave through.
            and rep.get("verified_pages", 0) > 0
            and rep.get("numeric_match_rate", 0) >= MIN_NUMERIC_RATE
            and rep.get("span_pass_rate", 0) >= MIN_SPAN_RATE
            # A looped decode keeps every number and so passes both rates.
            # An older report has no such field; absent reads as 0 = clean,
            # which is the right default for a metric that did not exist.
            and rep.get("max_repeat_words", 0) <= MAX_REPEAT_WORDS
        )
        if ok:
            rec["extraction_status"] = "extracted"
            rec["md_path"] = os.path.join("databank", rep["md_path"])
            rec["figure_count"] = rep.get("figures_kept", 0)
            rec["extraction_method"] = EXTRACTION_METHOD
            rec["extraction_quality"] = {
                "numeric_match_rate": round(rep.get("numeric_match_rate", 0), 4),
                "span_pass_rate": round(rep.get("span_pass_rate", 0), 4),
                "max_repeat_words": rep.get("max_repeat_words", 0),
                "verified_pages": rep.get("verified_pages", 0),
                "unverified_pages": rep.get("unverified_pages", 0),
                "pages": rep.get("pages", 0),
                "seconds": rep.get("seconds", 0),
            }
            extracted += 1
        else:
            # NAME THE GATE THAT ACTUALLY REJECTED. The `ok` test above has
            # THREE conditions, and this message used to report only two — so
            # a paper failed for having no verifiable text layer was told it
            # was "below quality threshold (numeric=1.00, span=1.00)". Both
            # rates perfect and rejected anyway reads as a broken gate; it is
            # in fact a correct rejection with a false explanation, and it
            # cost a live investigation to unpick. 8 of 81 lifetime failures
            # carry that misleading string.
            #
            # A vacuous 1.00 is what an unverifiable extraction SCORES: the
            # rates are hit/total with an empty total, so nothing checkable
            # means nothing missed. The rejection is right. The reason has to
            # say so.
            if rep and rep.get("error"):
                reason = rep["error"]
            elif oversize:
                reason = (
                    f"oversize — {rep.get('pages', 0)} pages, referred for "
                    f"review before any OCR pass (a dispatch shares one "
                    f"timeout, so a book takes its batch down with it)"
                )
            elif truncated:
                reason = (
                    f"truncated acquisition — the PDF holds {rep.get('pages', 0)} "
                    f"page(s) of an article published across "
                    f"{expected_page_extent(rec)} (pp. {rec.get('first_page')}-"
                    f"{rec.get('last_page')}); the extraction is faithful to a "
                    "fragment"
                )
            elif rep and rep.get("verified_pages", 0) <= 0:
                reason = (
                    f"no verifiable text layer ({rep.get('pages', 0)} page(s), "
                    f"0 verified) — rates are vacuous, not earned"
                )
            elif rep and rep.get("max_repeat_words", 0) > MAX_REPEAT_WORDS:
                reason = (
                    "degenerate decode — "
                    f"{rep['max_repeat_words']} words of back-to-back repetition "
                    f"(limit {MAX_REPEAT_WORDS}); the rates are clean because a "
                    "loop keeps every number"
                )
            elif rep:
                reason = (
                    "below quality threshold "
                    f"(numeric={rep.get('numeric_match_rate', 0):.2f}, "
                    f"span={rep.get('span_pass_rate', 0):.2f})"
                )
            else:
                reason = "no report from toolchain" + (
                    " (command timed out)" if result.timed_out else ""
                )
            # WHAT THE RUN PRODUCED IS RECORDED EVEN WHEN THE GATE REJECTS IT.
            # figure_count used to be written only on the success path, so a
            # rejected paper carried figure_count=0 while its figures sat on
            # disk — 345 orphaned PNGs across 9 papers on one live run, three
            # of them holding 118, 112 and 48 figures. Nothing downstream
            # could see them, and an analysis of the failures read "0 figures"
            # as a property of the papers rather than of the bookkeeping.
            #
            # The status still gates the curator; this only stops the record
            # from misdescribing what happened.
            if rep:
                rec["figure_count"] = rep.get("figures_kept", 0)
                if rep.get("md_path"):
                    rec["md_path"] = os.path.join("databank", rep["md_path"])
                rec["extraction_quality"] = {
                    "numeric_match_rate": round(rep.get("numeric_match_rate", 0), 4),
                    "span_pass_rate": round(rep.get("span_pass_rate", 0), 4),
                    "max_repeat_words": rep.get("max_repeat_words", 0),
                    "verified_pages": rep.get("verified_pages", 0),
                    "unverified_pages": rep.get("unverified_pages", 0),
                    "pages": rep.get("pages", 0),
                    "seconds": rep.get("seconds", 0),
                }
            # A PAPER WITH NO TEXT LAYER IS WHAT OCR IS FOR. Verification
            # compares our markdown against the PDF's own text layer; when
            # there is no layer there is nothing to compare, so the paper
            # scores a vacuous 1.00 and gets refused. But that is exactly the
            # scanned-document case OCR exists to handle, and refusing it
            # throws away the extraction that worked.
            #
            # Measured on the spectra corpus: 81 failures held 64 usable
            # markdowns totalling 5.0M characters and 1,353 extracted
            # figures, none of which any downstream stage could see —
            # _fig_pending gates on extraction_status == "extracted".
            #
            # So unverifiable gets its OWN terminal state rather than sharing
            # one with "the model got it wrong". It is still NOT promoted
            # automatically: unverifiable means unproven, and quietly
            # admitting it would put unvetted text in the corpus. It is
            # preserved, distinguishable, and queued for the spot check that
            # tools/extract_triage.py exists to serve.
            unverifiable = bool(rep) and rep.get("verified_pages", 0) <= 0
            if oversize:
                # NOT a quality verdict. The document is intact and probably
                # valuable; it is simply too large to feed a shared dispatch
                # budget, and that is a decision for a person.
                rec["extraction_status"] = "extract_oversize"
                rec["failure_reason"] = f"extraction: {reason}"
                failed += 1
            elif truncated:
                # TERMINAL on the first attempt, and deliberately so: another
                # OCR pass reads the same fragment and reaches the same
                # verdict. The fix is re-ACQUISITION, which is the scraper's
                # job, so burning a second GPU pass here buys nothing.
                rec["extraction_status"] = "extract_failed"
                rec["failure_reason"] = f"extraction: {reason}"
                failed += 1
            elif unverifiable:
                rec["extraction_status"] = "extract_unverified"
                rec["failure_reason"] = f"extraction: {reason}"
                failed += 1
            elif prior_retry:
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
