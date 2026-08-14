"""Acquisition and OCR, overlapped inside one action.

WHY. The 2026-08-11 spectra run spent 8 h to catalog 800 papers and extract
ZERO (`commands: 0` in the trace). Profile: 202 LLM calls against 1,866 external
API calls, and 714 HTTP 429s at a >=10s backoff floor — ~2.0 h asleep. The
scraper is I/O-bound and the GPU idles through almost all of it, while PDFs that
already landed wait for a human to run a second mission.

TWO INDEPENDENT WINS, ONE SEAM.

  * THROUGHPUT (the big one). `acquire_catalog` walked resolve -> download ->
    references serially, per record, each call paying its own politeness
    interval. Serial cost is sum(interval + latency); fanned out behind a
    per-host pacer it is max-over-hosts(n x interval) with all latency
    overlapped. With 4,756 candidates still pending at ~22 papers/h, this is
    worth ~100 h of wall clock — an order of magnitude more than the overlap.

  * OVERLAP. The whole OCR backlog is ~7.7 h of GPU work that currently happens
    in a different process, later. Gathered here it runs DURING the API waits.
    `effects.run_command` is `create_subprocess_exec` + `await communicate()` —
    it blocks the calling coroutine, never the event loop — so this genuinely
    overlaps rather than reordering.

NO LOGIC IS DUPLICATED. Each lane clones the StepInput and delegates to the
existing actions, so `action_resolve_oa_pdf`'s multi-location fallback,
`action_download_papers`' `oa_attempted` bookkeeping and
`action_extract_pdf_batch`'s quality policy stay in exactly one place and keep
their own tests. This module owns concurrency and nothing else.

THE INVARIANT (contract_swarm_actions.py:2036-2038): concurrent workers, SERIAL
post-hoc booking. Records are mutated per-record inside their own coroutine and
only merged back into the batch afterwards; the databank writes happen after
every lane has finished. The extractor writes `extraction.jsonl` and the scraper
writes `papers.jsonl` — the split that made this safe predates this file
(scholarly_actions.py:286-307).
"""

from __future__ import annotations

import asyncio
import logging
import os

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Socket-count bound only. Per-host politeness is enforced INSIDE
# polite_request by the pacer, so this does not need to know about hosts.
_ACQUIRE_CONCURRENCY = 5

# One OCR dispatch at a time. Not because the GPU can only do one thing —
# it demonstrably cannot be saturated by paddle alone (OCR against text decode
# overlaps at serialization 0.342, the best pair measured 2026-08-13) — but
# because a second dispatch would select the same pending records as the
# first. Nothing claims a paper before extraction: extraction_status is only
# written AFTER the fact, so two lanes would OCR the same PDFs twice. Widen
# this only behind a claim/lease.
_OCR_CONCURRENCY = 1

# Default PDFs per OCR dispatch.
#
# WAS 2 to amortise a server start (~20-40s for mlx_vlm.server, ~6.5s for
# llama-server). The fleet backend has NO server start — paddle is already
# hot inside LLMVP — so that reason is gone and the batch size is now purely
# about lane GRANULARITY: a smaller dispatch returns control to the
# acquisition lane sooner and lets the two interleave more finely, which is
# the whole point of the overlap.
_OVERLAP_PDFS_FLEET = 1
_OVERLAP_PDFS_SPAWNED = 2


def _overlap_pdfs() -> int:
    """How many PDFs to OCR per dispatch. 0 disables the OCR lane.

    Follows the VL backend, because the right answer depends on whether a
    dispatch pays a server start. An explicit env var beats both.
    """
    raw = os.environ.get("OUROBOROS_SCRAPER_OVERLAP_PDFS", "").strip()
    default = (
        _OVERLAP_PDFS_FLEET
        if os.environ.get("OUROBOROS_VL_BACKEND", "llmvp") == "llmvp"
        else _OVERLAP_PDFS_SPAWNED
    )
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("OUROBOROS_SCRAPER_OVERLAP_PDFS=%r not an integer", raw)
        return default


async def _acquire_one(step_input: StepInput, rec: dict) -> dict:
    """resolve -> download -> references for ONE record.

    Sequential within the record (download needs the urls resolve found),
    concurrent across records. Each sub-action gets a private one-record batch
    so nothing shares mutable state with a sibling coroutine.
    """
    from agent.actions.scholarly_actions import (
        action_download_papers,
        action_fetch_references,
        action_resolve_oa_pdf,
    )

    single = step_input.model_copy(update={"context": {"catalog_batch": [rec]}})
    for action in (
        action_resolve_oa_pdf,
        action_download_papers,
        action_fetch_references,
    ):
        try:
            await action(single)
        except Exception as exc:  # noqa: BLE001 — one record must not sink the batch
            logger.warning(
                "acquire: %s failed on %s: %s",
                action.__name__,
                rec.get("paper_key", "?"),
                exc,
            )
            rec.setdefault("failure_reason", f"{action.__name__}: {exc}"[:200])
    return rec


async def _ocr_lane(step_input: StepInput, max_pdfs: int) -> dict:
    """OCR PDFs that landed in EARLIER dispatches, while this one does HTTP.

    A one-dispatch lag is the finest granularity a serial runtime offers without
    background-task machinery: a PDF landing in dispatch N enters OCR at N+1 —
    minutes, not a separate mission. Returns a summary; never raises, because a
    failed OCR must not fail an acquisition batch.
    """
    from agent.actions.extraction_actions import (
        _extraction_pending,
        action_extract_pdf_batch,
    )
    from agent.actions.scholarly_actions import read_databank

    if max_pdfs <= 0:
        return {"attempted": 0, "reason": "disabled"}
    effects = step_input.effects
    working_dir = str(step_input.inputs.get("working_directory") or "")
    if not effects or not working_dir:
        return {"attempted": 0, "reason": "no working_directory"}

    databank = await read_databank(effects)
    # needs_reextract first — a bounded retry should not queue behind the
    # whole backlog (mirrors action_pdf_extract_sweep_next's ordering).
    pending = [
        (k, r)
        for k, r in databank.items()
        if _extraction_pending(r) and r.get("pdf_path")
    ]
    pending.sort(key=lambda kr: kr[1].get("extraction_status") != "needs_reextract")
    keys = [k for k, _ in pending[:max_pdfs]]
    if not keys:
        return {"attempted": 0, "reason": "nothing pending"}

    sub = step_input.model_copy(
        update={
            "inputs": {
                **dict(step_input.inputs or {}),
                "paper_keys": keys,
                "working_directory": working_dir,
            }
        }
    )
    try:
        out = await action_extract_pdf_batch(sub)
    except Exception as exc:  # noqa: BLE001 — lane boundary
        logger.warning("ocr lane failed: %s", exc)
        return {"attempted": len(keys), "error": str(exc)[:200]}
    return {"attempted": len(keys), "result": dict(out.result or {})}


async def action_acquire_batch(step_input: StepInput) -> StepOutput:
    """Acquire the batch concurrently while OCR drains earlier PDFs.

    Context: catalog_batch; Inputs: working_directory
    Result: downloaded, failed, ocr; Publishes: catalog_batch
    """
    from agent.actions.scholarly_actions import _pacer

    batch = list(step_input.context.get("catalog_batch") or [])
    if not batch:
        return StepOutput(
            result={"downloaded": 0, "failed": 0, "ocr": {"attempted": 0}},
            observations="Empty acquisition batch",
            context_updates={"catalog_batch": batch},
        )

    sem = asyncio.Semaphore(_ACQUIRE_CONCURRENCY)
    ocr_sem = asyncio.Semaphore(_OCR_CONCURRENCY)

    async def guarded(rec: dict) -> dict:
        async with sem:
            return await _acquire_one(step_input, rec)

    async def guarded_ocr() -> dict:
        async with ocr_sem:
            return await _ocr_lane(step_input, _overlap_pdfs())

    # THE OVERLAP. HTTP and the paddle subprocess run together; the pacer keeps
    # each host honest independently of how wide this fans out.
    records, ocr = await asyncio.gather(
        asyncio.gather(*(guarded(r) for r in batch)),
        guarded_ocr(),
    )

    # ── serial booking from here ──────────────────────────────────────
    #
    # REPAIR PASS, AFTER THE CHEAP PATH AND ACROSS THE WHOLE BATCH. Two
    # reasons it lives here rather than inside _acquire_one:
    #
    #   ORDERING — a plain download recovers 35% of landing-page failures for
    #   free (Springer 6/6 on live re-fetch), so navigation must see only what
    #   download could not get. Running it per-record before the batch settles
    #   would spend inference on papers already in hand.
    #
    #   THE CAP — _NAV_PER_DISPATCH is counted within one action call. Called
    #   from _acquire_one it would run once per RECORD, so a 5-record batch
    #   could fire five navigations against a cap that reads like two. Here it
    #   sees the batch and the ceiling means what it says.
    from agent.actions.scholarly_actions import action_navigate_landing_page

    nav = {"navigated": 0, "attempted": 0}
    try:
        nav_out = await action_navigate_landing_page(
            step_input.model_copy(update={"context": {"catalog_batch": records}})
        )
        nav = dict(nav_out.result or {})
        records = list(nav_out.context_updates.get("catalog_batch") or records)
    except Exception as exc:  # noqa: BLE001 — a repair must not sink the batch
        logger.warning("landing-page navigation failed: %s", exc)

    downloaded = sum(1 for r in records if r.get("pdf_path"))
    failed = len(records) - downloaded
    pacer_stats = _pacer().stats()
    throttled = sum(h.get("throttled", 0) for h in pacer_stats.values())
    requests = sum(h.get("requests", 0) for h in pacer_stats.values())

    ocr_note = ""
    if ocr.get("attempted"):
        ocr_note = f"; OCR {ocr['attempted']} pdf(s) in parallel"
    nav_note = ""
    if nav.get("attempted"):
        nav_note = f"; nav recovered {nav.get('navigated', 0)}/{nav['attempted']}"
    return StepOutput(
        result={
            "downloaded": downloaded,
            "failed": failed,
            "ocr": ocr,
            "nav": nav,
            "http": {"requests": requests, "throttled": throttled},
        },
        observations=(
            f"Acquired {downloaded}/{len(records)} concurrently"
            f"{ocr_note}{nav_note} — {requests} requests, {throttled} throttled"
        ),
        context_updates={"catalog_batch": list(records)},
    )
