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
    # FALL BACK TO THE MISSION. This read `inputs["working_directory"]` alone,
    # and the acquire step is dispatched with params {} — so on a flow that
    # does not thread that input through, the OCR lane declined every dispatch
    # and the whole overlap silently did nothing. Live: 421 successful
    # acquire_catalog reports, 97 papers pending extraction with PDFs on disk,
    # and the second GPU at 0% for an hour and a half.
    #
    # The mission always knows its own working directory, so ask it when the
    # input is absent rather than giving up.
    working_dir = str(step_input.inputs.get("working_directory") or "")
    if not working_dir:
        mission = step_input.context.get("mission")
        working_dir = str(
            getattr(getattr(mission, "config", None), "working_directory", "") or ""
        )
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


# ── tag lane ──────────────────────────────────────────────────────────
#
# READY AT T=0. A record's tag inputs are title + abstract, both present in
# the databank record before acquisition starts (discovery stored them), so
# the tag lane fans out over the WHOLE batch the moment the step begins and
# muse seats work through the entire HTTP window instead of idling until the
# single post-acquire turn. The batched engine admits the concurrent turns as
# separate seats (measured text+text serialization 0.047).
#
# INFER CONCURRENTLY, STAMP SERIALLY. The lane returns (sub_batch, response)
# pairs and mutates NOTHING: download and navigation write
# rec["status"]="acquired", so a concurrent "cataloged" stamp would be
# clobbered depending on which coroutine finished last. Stamping happens in
# the action's serial booking section, after navigation and enrichment have
# settled the records — same invariant as every other lane
# (contract_swarm_actions.py:2036-2038).

# Sub-batch size for one tag turn. Smaller = finer streaming granularity and
# tighter per-paper attention; larger = fewer turns. The research gate's
# grounding check is the quality monitor for this knob.
_TAG_SUBBATCH = 2
# Concurrent tag turns. 3 of the 4 batched seats — the OCR lane's vision
# work rides paddle, not a muse seat, but leaving one seat clear keeps the
# engine responsive if anything else (health probes, a stray session) needs
# a slot mid-dispatch.
_TAG_CONCURRENCY = 3


def _tag_lane_params() -> tuple[int, int]:
    """(sub_batch_size, concurrency), env-overridable; 0 disables the lane."""

    def _env_int(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            return max(0, int(raw))
        except ValueError:
            logger.warning("%s=%r not an integer", name, raw)
            return default

    return (
        _env_int("OUROBOROS_SCRAPER_TAG_SUBBATCH", _TAG_SUBBATCH),
        _env_int("OUROBOROS_SCRAPER_TAG_SEATS", _TAG_CONCURRENCY),
    )


def _render_tag_prompt(mission: object, sub_batch: list) -> str:
    """Render scraper/tag_paper exactly as the tag_papers step does — same
    template, same formatters — for a sub-batch."""
    from agent.formatters import format_aspect_definitions, format_catalog_batch
    from agent.runtime import _get_prompt_renderer

    plan = getattr(mission, "research_plan", None) if mission else None
    namespaces = {
        "input": {},
        "context": {
            "aspects_block": format_aspect_definitions({"source": plan}, {}),
            "papers_block": format_catalog_batch({"source": sub_batch}, {}),
        },
        "meta": {"flow_name": "acquire_catalog", "step_id": "acquire.tag_lane"},
    }
    static_prefix, dynamic = _get_prompt_renderer().render_with_cache_split(
        "scraper/tag_paper", namespaces
    )
    return static_prefix + dynamic


async def _tag_lane(step_input: StepInput, batch: list) -> list:
    """Run concurrent tag turns over the batch; return (sub_batch, text) pairs.

    Never raises and never mutates records: a failed turn simply yields no
    pair, its records stay untagged, and the flow's fallback tag_papers turn
    covers them. Empty-response and inference-error turns are logged.
    """
    sub_size, seats = _tag_lane_params()
    effects = step_input.effects
    mission = step_input.context.get("mission")
    if sub_size <= 0 or seats <= 0:
        return []
    if effects is None or not hasattr(effects, "run_inference"):
        return []
    if mission is None or not getattr(mission, "research_plan", None):
        # No plan means no aspect whitelist — tags would validate against
        # nothing. Leave the batch for the fallback turn's step context.
        return []

    sub_batches = [batch[i : i + sub_size] for i in range(0, len(batch), sub_size)]
    sem = asyncio.Semaphore(seats)

    async def one(sub: list):
        async with sem:
            try:
                prompt = _render_tag_prompt(mission, sub)
                result = await effects.run_inference(prompt, {"temperature": "t*0.3"})
            except Exception as exc:  # noqa: BLE001 — lane boundary
                logger.warning("tag lane turn failed (%d paper(s)): %s", len(sub), exc)
                return None
            err = getattr(result, "error", None)
            text = getattr(result, "text", "") or ""
            if err or not text:
                logger.warning(
                    "tag lane turn empty (%d paper(s)): %s", len(sub), err or "no text"
                )
                return None
            return (sub, str(text))

    pairs = await asyncio.gather(*(one(s) for s in sub_batches))
    return [p for p in pairs if p is not None]


async def action_acquire_batch(step_input: StepInput) -> StepOutput:
    """Acquire the batch concurrently while OCR drains earlier PDFs and the
    tag lane streams tag turns onto open muse seats.

    Context: catalog_batch, mission; Inputs: working_directory
    Result: downloaded, failed, ocr, tags, untagged; Publishes: catalog_batch
    """
    from agent.actions.scholarly_actions import _pacer

    batch = list(step_input.context.get("catalog_batch") or [])
    if not batch:
        return StepOutput(
            result={
                "downloaded": 0,
                "failed": 0,
                "ocr": {"attempted": 0},
                "tags": {"turns": 0, "cataloged": 0, "dropped": 0},
                "untagged": 0,
            },
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

    # THE OVERLAP. Three lanes matched to what is ready: HTTP acquisition
    # (per record), paddle OCR (earlier PDFs), and tag turns on open muse
    # seats (title+abstract are ready at t=0). The pacer keeps each host
    # honest independently of how wide this fans out; the tag lane returns
    # responses only — stamping is serial, below.
    records, ocr, tag_pairs = await asyncio.gather(
        asyncio.gather(*(guarded(r) for r in batch)),
        guarded_ocr(),
        _tag_lane(step_input, batch),
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

    # PAGE EXTENT AND LICENSE, once the batch has settled. Only papers whose PDF we now
    # hold need it, so it runs after the repair pass rather than before: a
    # record navigation just rescued is exactly one we want the extent for,
    # and enriching earlier would look up papers we never got.
    from agent.actions.scholarly_actions import action_enrich_paper_metadata

    extent = {"enriched": 0, "looked_up": 0}
    try:
        ext_out = await action_enrich_paper_metadata(
            step_input.model_copy(update={"context": {"catalog_batch": records}})
        )
        extent = dict(ext_out.result or {})
        records = list(ext_out.context_updates.get("catalog_batch") or records)
    except Exception as exc:  # noqa: BLE001 — enrichment must not sink a batch
        logger.warning("page-extent enrichment failed: %s", exc)

    # TAG STAMPING, serial and last: navigation/download write
    # rec["status"]="acquired", so stamping "cataloged" any earlier would be
    # clobbered by whichever repair finished after it. Stamps are applied to
    # the SETTLED records (matched by paper_key — a repair pass replacing
    # dict instances must not orphan a stamp), using the same validator the
    # apply_tags step uses.
    tags_summary = {"turns": len(tag_pairs), "cataloged": 0, "dropped": 0}
    untagged = len(records)
    if tag_pairs:
        from agent.actions.scholarly_actions import (
            mission_valid_aspects,
            validate_and_stamp_tags,
        )

        by_key = {r.get("paper_key"): r for r in records if r.get("paper_key")}
        valid_aspects = mission_valid_aspects(step_input.context.get("mission"))
        for sub, text in tag_pairs:
            settled = [by_key.get(r.get("paper_key"), r) for r in sub]
            cataloged, dropped = validate_and_stamp_tags(settled, text, valid_aspects)
            tags_summary["cataloged"] += cataloged
            tags_summary["dropped"] += dropped
    untagged = sum(1 for r in records if r.get("status") != "cataloged")

    downloaded = sum(1 for r in records if r.get("pdf_path"))
    failed = len(records) - downloaded
    pacer_stats = _pacer().stats()
    throttled = sum(h.get("throttled", 0) for h in pacer_stats.values())
    requests = sum(h.get("requests", 0) for h in pacer_stats.values())

    # A LANE THAT DECLINES MUST SAY SO. Previously only a lane that RAN was
    # reported, so "disabled", "no working_directory" and "nothing pending"
    # were indistinguishable from a healthy overlap in every log and report —
    # which is why an hour and a half of zero OCR looked normal.
    ocr_note = ""
    if ocr.get("attempted"):
        ocr_note = f"; OCR {ocr['attempted']} pdf(s) in parallel"
    elif ocr.get("reason"):
        ocr_note = f"; OCR lane idle ({ocr['reason']})"
    nav_note = ""
    if nav.get("attempted"):
        nav_note = f"; nav recovered {nav.get('navigated', 0)}/{nav['attempted']}"
    extent_note = ""
    if extent.get("looked_up"):
        extent_note = f"; page extent {extent.get('enriched', 0)}/{extent['looked_up']}"
    tag_note = ""
    if tags_summary["turns"]:
        tag_note = (
            f"; tagged {tags_summary['cataloged']}/{len(records)} in "
            f"{tags_summary['turns']} concurrent turn(s)"
        )
    return StepOutput(
        result={
            "downloaded": downloaded,
            "failed": failed,
            "ocr": ocr,
            "nav": nav,
            "page_extent": extent,
            "tags": tags_summary,
            "untagged": untagged,
            "http": {"requests": requests, "throttled": throttled},
        },
        observations=(
            f"Acquired {downloaded}/{len(records)} concurrently"
            f"{ocr_note}{nav_note}{extent_note}{tag_note} — {requests} requests, "
            f"{throttled} throttled"
        ),
        context_updates={"catalog_batch": list(records)},
    )
