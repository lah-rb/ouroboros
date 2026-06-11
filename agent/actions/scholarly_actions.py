"""Scholarly API actions: discovery, acquisition, and the paper databank.

The scraper flow set's I/O layer. Network goes through the first-class
HTTP effect; politeness (per-host min-interval + a mission request
budget) lives HERE, not in the effect — the effect is a dumb pipe.

## The databank (workspace files — never in mission state)

``databank/papers.jsonl`` — one JSON object per line; the LAST record
for a ``paper_key`` wins (append-only updates). Record contract:

    {
      "paper_key":  "doi_10.1063_5.0123456" | "arxiv_2401.01234" | "s2_<id>",
      "status":     "candidate" | "acquired" | "cataloged" | "failed"
                    | "needs_retag",
      "access_status": "" | "oa_pdf" | "oa_unresolved" | "closed",
      "title": str, "abstract": str, "year": int, "venue": str,
      "authors": [str], "doi": str, "arxiv_id": str, "s2_id": str,
      "openalex_id": str,
      "source_aspects": [str],      # which aspect queries found it
      "oa_pdf_url": str, "pdf_path": str,   # "" when not downloaded
      "tags": [{"aspect": str,
                "relevance": "exact"|"close"|"adjacent",
                "justification": str}],
      "reference_dois": [str],      # capped, from S2 references
      "failure_reason": str, "updated_at": iso8601
    }

Closed papers are CATALOGED, not dropped — access_status="closed" is an
access state, never a failure state; those records double as the v2
acquisition worklist. ``databank/links.json`` (corpus-internal citation
edges) is written by the research gate's finalize_crosslinks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlsplit

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

DATABANK_PATH = "databank/papers.jsonl"
PDF_DIR = "pdfs"
RELEVANCE_TIERS = ("exact", "close", "adjacent")
MAX_REFERENCE_DOIS = 200
CATALOG_BATCH_SIZE = 5

# ── Politeness ────────────────────────────────────────────────────────

_HTTP_STATE_KEY = "scraper_http_state"
_MISSION_REQUEST_BUDGET = 600
_DEFAULT_MIN_INTERVAL = 2.0
_HOST_MIN_INTERVAL = {
    # Unauthenticated shared pool is ~100 req / 5 min.
    "api.semanticscholar.org": 3.5,
    # Polite pool (mailto param) tolerates ~10 rps; stay well under.
    "api.openalex.org": 0.6,
    "api.unpaywall.org": 1.0,
}


def _contact_email() -> str:
    """Contact email for polite-pool params (OpenAlex mailto, Unpaywall).

    Set OUROBOROS_CONTACT_EMAIL; the fallback is detectable as a default
    so operators notice it in API logs rather than silently impersonating
    nobody.
    """
    return os.environ.get("OUROBOROS_CONTACT_EMAIL", "ouroboros-agent@invalid.local")


def _s2_headers() -> dict | None:
    """Optional Semantic Scholar API key (SEMANTIC_SCHOLAR_API_KEY).

    The unauthenticated shared pool 429s under contention (live-
    observed); a free key moves requests to a dedicated quota. Without
    one, discovery degrades gracefully — OpenAlex carries the round.
    """
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    return {"x-api-key": key} if key else None


async def polite_request(
    effects: Any,
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
):
    """http_request with per-host min-interval + mission budget.

    State (effects.read_state/write_state, key ``scraper_http_state``):
    ``{"hosts": {host: {"last_ts": epoch}}, "total_requests": int}``.
    Budget exhaustion returns a status=0 HttpResult-shaped failure
    rather than raising — callers treat it like any transport error.
    """
    from agent.effects.protocol import HttpResult

    state = await effects.read_state(_HTTP_STATE_KEY) or {}
    hosts = state.get("hosts") or {}
    total = int(state.get("total_requests") or 0)

    if total >= _MISSION_REQUEST_BUDGET:
        logger.warning("Scraper HTTP budget exhausted (%d requests)", total)
        return HttpResult(
            status=0,
            url=url,
            error=f"mission request budget exhausted ({_MISSION_REQUEST_BUDGET})",
        )

    host = urlsplit(url).netloc
    min_interval = _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL)
    last_ts = float((hosts.get(host) or {}).get("last_ts") or 0.0)
    wait = min_interval - (time.time() - last_ts)
    if wait > 0:
        await asyncio.sleep(wait)

    result = await effects.http_request(
        method, url, params=params, headers=headers, timeout=timeout
    )
    if result.status == 429:
        # Shared-pool contention (live-observed on S2's unauthenticated
        # pool). One respectful retry honoring Retry-After, then accept
        # the miss — the round/budget machinery absorbs thin rounds.
        try:
            retry_after = float((result.headers or {}).get("retry-after") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        await asyncio.sleep(min(max(retry_after, 10.0), 60.0))
        result = await effects.http_request(
            method, url, params=params, headers=headers, timeout=timeout
        )

    hosts[host] = {"last_ts": time.time()}
    await effects.write_state(
        _HTTP_STATE_KEY, {"hosts": hosts, "total_requests": total + 1}
    )
    return result


# ── Databank helpers ──────────────────────────────────────────────────


def paper_key(record: dict) -> str:
    """Stable identity: normalized DOI, else arXiv id, else S2 id."""
    doi = str(record.get("doi") or "").strip().lower()
    if doi:
        return "doi_" + doi.replace("/", "_")
    arxiv = str(record.get("arxiv_id") or "").strip().lower()
    if arxiv:
        return "arxiv_" + arxiv
    s2 = str(record.get("s2_id") or "").strip()
    if s2:
        return "s2_" + s2
    # Last resort: title slug (deterministic, collision-tolerant).
    title = str(record.get("title") or "untitled").strip().lower()
    return "title_" + "".join(c if c.isalnum() else "_" for c in title)[:80]


async def read_databank(effects: Any) -> dict[str, dict]:
    """paper_key -> record, last-record-wins."""
    fc = await effects.read_file(DATABANK_PATH)
    records: dict[str, dict] = {}
    if not getattr(fc, "exists", False):
        return records
    for line in fc.content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = rec.get("paper_key") or paper_key(rec)
        records[key] = rec
    return records


async def append_records(effects: Any, records: list[dict]) -> None:
    """Append records as JSONL lines (last-wins semantics on read)."""
    if not records:
        return
    from agent.persistence.models import _now_iso

    fc = await effects.read_file(DATABANK_PATH)
    existing = fc.content if getattr(fc, "exists", False) else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    lines = []
    for rec in records:
        rec = dict(rec)
        rec.setdefault("paper_key", paper_key(rec))
        rec["updated_at"] = _now_iso()
        lines.append(json.dumps(rec, ensure_ascii=False))
    await effects.write_file(DATABANK_PATH, existing + "\n".join(lines) + "\n")


# ── API normalization ─────────────────────────────────────────────────

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_SEARCH_FIELDS = (
    "title,abstract,externalIds,openAccessPdf,isOpenAccess,year,venue,"
    "authors,citationCount"
)
_OPENALEX_BASE = "https://api.openalex.org"
_OPENALEX_SELECT = (
    "id,doi,title,abstract_inverted_index,publication_year,primary_location,"
    "authorships,open_access,best_oa_location,ids"
)


def _deinvert_abstract(idx: Any) -> str:
    """OpenAlex ships abstracts as {word: [positions]} — re-invert."""
    if not isinstance(idx, dict) or not idx:
        return ""
    positions: dict[int, str] = {}
    for word, posns in idx.items():
        if not isinstance(posns, list):
            continue
        for p in posns:
            if isinstance(p, int):
                positions[p] = word
    return " ".join(positions[p] for p in sorted(positions))


def _candidate_base(aspect_name: str) -> dict:
    return {
        "status": "candidate",
        "access_status": "",
        "source_aspects": [aspect_name] if aspect_name else [],
        "oa_pdf_url": "",
        "pdf_path": "",
        "tags": [],
        "reference_dois": [],
        "failure_reason": "",
    }


def _normalize_s2(paper: dict, aspect_name: str) -> dict:
    ext = paper.get("externalIds") or {}
    oa = paper.get("openAccessPdf") or {}
    rec = {
        **_candidate_base(aspect_name),
        "title": str(paper.get("title") or ""),
        "abstract": str(paper.get("abstract") or ""),
        "year": paper.get("year") or 0,
        "venue": str(paper.get("venue") or ""),
        "authors": [str(a.get("name") or "") for a in (paper.get("authors") or [])][
            :25
        ],
        "doi": str(ext.get("DOI") or ""),
        "arxiv_id": str(ext.get("ArXiv") or ""),
        "s2_id": str(paper.get("paperId") or ""),
        "openalex_id": "",
        "oa_pdf_url": str(oa.get("url") or ""),
    }
    rec["paper_key"] = paper_key(rec)
    return rec


def _normalize_openalex(work: dict, aspect_name: str) -> dict:
    doi_url = str(work.get("doi") or "")
    doi = doi_url.split("doi.org/")[-1] if "doi.org/" in doi_url else doi_url
    ids = work.get("ids") or {}
    best_oa = work.get("best_oa_location") or {}
    # host_venue was removed from the OpenAlex schema (live-verified
    # 400); the venue now lives at primary_location.source.
    source = (work.get("primary_location") or {}).get("source") or {}
    venue = source.get("display_name") or ""
    rec = {
        **_candidate_base(aspect_name),
        "title": str(work.get("title") or ""),
        "abstract": _deinvert_abstract(work.get("abstract_inverted_index")),
        "year": work.get("publication_year") or 0,
        "venue": str(venue),
        "authors": [
            str(((a.get("author") or {}).get("display_name")) or "")
            for a in (work.get("authorships") or [])
        ][:25],
        "doi": doi,
        "arxiv_id": "",
        "s2_id": "",
        "openalex_id": str(work.get("id") or ids.get("openalex") or ""),
        "oa_pdf_url": str(best_oa.get("pdf_url") or ""),
    }
    rec["paper_key"] = paper_key(rec)
    return rec


# ── Actions ───────────────────────────────────────────────────────────


async def action_scholarly_search(step_input: StepInput) -> StepOutput:
    """Run each query against Semantic Scholar AND OpenAlex.

    Context: search_queries (list[str])
    Params: aspect_name, max_per_query (default 20)
    Result: results_found, s2_count, openalex_count
    Publishes: raw_candidates (normalized candidate records)
    """
    effects = step_input.effects
    queries = [
        str(q).strip()
        for q in (step_input.context.get("search_queries") or [])
        if str(q).strip()
    ]
    if not queries:
        # Query refinement failed/was skipped — the aspect's seed
        # queries from the research plan are the fallback.
        queries = [
            str(q).strip()
            for q in (step_input.params.get("seed_queries") or [])
            if str(q).strip()
        ]
    aspect_name = str(step_input.params.get("aspect_name") or "")
    max_per_query = int(step_input.params.get("max_per_query") or 20)

    candidates: list[dict] = []
    s2_count = openalex_count = 0
    for query in queries:
        s2 = await polite_request(
            effects,
            "GET",
            f"{_S2_BASE}/paper/search",
            params={
                "query": query,
                "limit": max_per_query,
                "fields": _S2_SEARCH_FIELDS,
            },
            headers=_s2_headers(),
        )
        if s2.status == 200 and isinstance(s2.json_data, dict):
            for paper in s2.json_data.get("data") or []:
                candidates.append(_normalize_s2(paper, aspect_name))
                s2_count += 1
        else:
            logger.warning("S2 search failed for %r: %s", query, s2.error or s2.status)

        oa = await polite_request(
            effects,
            "GET",
            f"{_OPENALEX_BASE}/works",
            params={
                "search": query,
                "per-page": max_per_query,
                "select": _OPENALEX_SELECT,
                "mailto": _contact_email(),
            },
        )
        if oa.status == 200 and isinstance(oa.json_data, dict):
            for work in oa.json_data.get("results") or []:
                candidates.append(_normalize_openalex(work, aspect_name))
                openalex_count += 1
        else:
            logger.warning(
                "OpenAlex search failed for %r: %s", query, oa.error or oa.status
            )

    return StepOutput(
        result={
            "results_found": len(candidates),
            "s2_count": s2_count,
            "openalex_count": openalex_count,
        },
        observations=(
            f"Scholarly search ({aspect_name or 'no aspect'}): "
            f"{len(candidates)} hits across {len(queries)} query(ies) "
            f"(S2 {s2_count}, OpenAlex {openalex_count})"
        ),
        context_updates={"raw_candidates": candidates},
    )


async def action_merge_candidates(step_input: StepInput) -> StepOutput:
    """Dedup candidates into the databank; build the discover report.

    Same paper from both APIs (or a re-run query) collapses onto one
    paper_key; existing records only re-emit when source_aspects grew.

    Context: raw_candidates; Params: aspect_name
    Result: new_candidates, merged_existing, aspect_total
    Publishes: discovery_stats, directive_report
    """
    effects = step_input.effects
    raw = step_input.context.get("raw_candidates") or []
    aspect_name = str(step_input.params.get("aspect_name") or "")

    databank = await read_databank(effects)

    # First collapse the raw batch itself (S2 + OpenAlex overlap heavily).
    batch: dict[str, dict] = {}
    for cand in raw:
        key = cand.get("paper_key") or paper_key(cand)
        if key in batch:
            existing = batch[key]
            # Prefer the record with an abstract / OA url.
            if not existing.get("abstract") and cand.get("abstract"):
                cand["source_aspects"] = existing.get("source_aspects", [])
                batch[key] = {**existing, **{k: v for k, v in cand.items() if v}}
            elif not existing.get("oa_pdf_url") and cand.get("oa_pdf_url"):
                existing["oa_pdf_url"] = cand["oa_pdf_url"]
        else:
            batch[key] = dict(cand)

    to_append: list[dict] = []
    new_count = merged_count = 0
    for key, cand in batch.items():
        existing = databank.get(key)
        if existing is None:
            to_append.append(cand)
            databank[key] = cand
            new_count += 1
            continue
        aspects = set(existing.get("source_aspects") or [])
        if aspect_name and aspect_name not in aspects:
            aspects.add(aspect_name)
            existing["source_aspects"] = sorted(aspects)
            to_append.append(existing)
            merged_count += 1

    await append_records(effects, to_append)

    aspect_total = sum(
        1
        for rec in databank.values()
        if aspect_name in (rec.get("source_aspects") or [])
    )
    report = {
        "flow": "discover",
        "status": "success" if (new_count or merged_count or not raw) else "partial",
        "summary": (
            f"Discovery for aspect '{aspect_name}': {new_count} new candidate(s), "
            f"{merged_count} existing paper(s) gained the aspect; the aspect now "
            f"has {aspect_total} candidate(s) in the databank."
        ),
        "headline": f"aspect '{aspect_name}': +{new_count} candidates ({aspect_total} total)",
        "checks_passed": [f"aspect:{aspect_name} candidates:{aspect_total}"],
    }
    return StepOutput(
        result={
            "new_candidates": new_count,
            "merged_existing": merged_count,
            "aspect_total": aspect_total,
        },
        observations=report["headline"],
        context_updates={
            "discovery_stats": {
                "aspect": aspect_name,
                "new": new_count,
                "total": aspect_total,
            },
            "directive_report": report,
        },
    )


async def action_catalog_batch_next(step_input: StepInput) -> StepOutput:
    """Load the dispatch's paper records from the databank.

    Input/params: paper_keys (list[str], chosen by catalog_sweep_next)
    Result: batch_size; Publishes: catalog_batch
    """
    effects = step_input.effects
    keys = list(
        step_input.params.get("paper_keys")
        or step_input.context.get("paper_keys")
        or []
    )
    databank = await read_databank(effects)
    batch = [databank[k] for k in keys if k in databank]
    return StepOutput(
        result={"batch_size": len(batch)},
        observations=f"Catalog batch: {len(batch)} paper(s)",
        context_updates={"catalog_batch": batch},
    )


async def action_resolve_oa_pdf(step_input: StepInput) -> StepOutput:
    """Assign access_status per paper; resolve missing OA urls via Unpaywall.

    oa_pdf (an OA location is known) / closed (no OA location anywhere).
    oa_unresolved is assigned at download time when a known location
    fails to fetch. Closed is an ACCESS state, never a failure.

    Context: catalog_batch
    Result: resolved, closed; Publishes: catalog_batch
    """
    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    resolved = closed = 0
    for rec in batch:
        if rec.get("oa_pdf_url"):
            rec["access_status"] = "oa_pdf"
            resolved += 1
            continue
        doi = str(rec.get("doi") or "").strip()
        if doi:
            up = await polite_request(
                effects,
                "GET",
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": _contact_email()},
            )
            url = ""
            if up.status == 200 and isinstance(up.json_data, dict):
                best = up.json_data.get("best_oa_location") or {}
                url = str(best.get("url_for_pdf") or "")
            if url:
                rec["oa_pdf_url"] = url
                rec["access_status"] = "oa_pdf"
                resolved += 1
                continue
        rec["access_status"] = "closed"
        closed += 1
    return StepOutput(
        result={"resolved": resolved, "closed": closed},
        observations=f"OA resolution: {resolved} open, {closed} closed",
        context_updates={"catalog_batch": batch},
    )


async def action_download_papers(step_input: StepInput) -> StepOutput:
    """Download oa_pdf papers to pdfs/<paper_key>.pdf.

    Fetch failure downgrades access_status to oa_unresolved (retryable
    in v2); the paper still proceeds to tagging — metadata + abstract
    are enough for the catalog.

    Context: catalog_batch
    Result: downloaded, failed; Publishes: catalog_batch
    """
    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    downloaded = failed = 0
    for rec in batch:
        if rec.get("access_status") != "oa_pdf" or rec.get("pdf_path"):
            continue
        key = rec.get("paper_key") or paper_key(rec)
        path = f"{PDF_DIR}/{key}.pdf"
        dl = await effects.http_download(rec["oa_pdf_url"], path)
        if dl.success:
            rec["pdf_path"] = path
            rec["status"] = "acquired"
            downloaded += 1
        else:
            rec["access_status"] = "oa_unresolved"
            rec["failure_reason"] = str(dl.error or f"HTTP {dl.status}")
            failed += 1
    return StepOutput(
        result={"downloaded": downloaded, "failed": failed},
        observations=f"Downloads: {downloaded} fetched, {failed} unresolved",
        context_updates={"catalog_batch": batch},
    )


async def action_fetch_references(step_input: StepInput) -> StepOutput:
    """Store each paper's reference DOIs (S2 references API, capped).

    Corpus-internal edges are computed at gate time (finalize_crosslinks)
    so links to later-acquired papers aren't missed.

    Context: catalog_batch
    Result: fetched, skipped; Publishes: catalog_batch
    """
    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    fetched = skipped = 0
    for rec in batch:
        if rec.get("reference_dois"):
            continue
        if rec.get("doi"):
            ident = f"DOI:{rec['doi']}"
        elif rec.get("arxiv_id"):
            ident = f"ARXIV:{rec['arxiv_id']}"
        elif rec.get("s2_id"):
            ident = rec["s2_id"]
        else:
            skipped += 1
            continue
        resp = await polite_request(
            effects,
            "GET",
            f"{_S2_BASE}/paper/{ident}/references",
            params={"fields": "externalIds", "limit": 500},
            headers=_s2_headers(),
        )
        dois: list[str] = []
        if resp.status == 200 and isinstance(resp.json_data, dict):
            for entry in resp.json_data.get("data") or []:
                cited = entry.get("citedPaper") or {}
                doi = ((cited.get("externalIds") or {}).get("DOI") or "").lower()
                if doi:
                    dois.append(doi)
                if len(dois) >= MAX_REFERENCE_DOIS:
                    break
            rec["reference_dois"] = dois
            fetched += 1
        else:
            skipped += 1
    return StepOutput(
        result={"fetched": fetched, "skipped": skipped},
        observations=f"References: {fetched} fetched, {skipped} skipped",
        context_updates={"catalog_batch": batch},
    )


async def action_apply_paper_tags(step_input: StepInput) -> StepOutput:
    """Parse the batch tag JSON; validate; persist cataloged records.

    Tag JSON: {paper_key: [{aspect, relevance, justification}]}.
    Aspect names validate against mission.research_plan; relevance
    against the exact/close/adjacent enum (invalid entries dropped,
    counted). Papers with parsed tags become "cataloged"; papers the
    model skipped stay at their current status and re-enter a later
    batch. Builds the acquire_catalog directive_report.

    Context: catalog_batch, inference_response, mission
    Result: cataloged, tag_parse_failed, dropped_tags
    Publishes: directive_report
    """
    from agent.llm_json import parse_llm_json

    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    mission = step_input.context.get("mission")
    plan = getattr(mission, "research_plan", None) if mission else None
    valid_aspects = {a.name for a in (plan.aspects if plan else [])}

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    tag_map = parsed if isinstance(parsed, dict) else {}

    cataloged = dropped = 0
    for rec in batch:
        key = rec.get("paper_key") or paper_key(rec)
        raw_tags = tag_map.get(key)
        if not isinstance(raw_tags, list):
            continue
        tags = []
        for t in raw_tags:
            if not isinstance(t, dict):
                dropped += 1
                continue
            aspect = str(t.get("aspect") or "").strip()
            relevance = str(t.get("relevance") or "").strip().lower()
            if (
                valid_aspects and aspect not in valid_aspects
            ) or relevance not in RELEVANCE_TIERS:
                dropped += 1
                continue
            tags.append(
                {
                    "aspect": aspect,
                    "relevance": relevance,
                    "justification": str(t.get("justification") or "")[:500],
                }
            )
        rec["tags"] = tags
        rec["status"] = "cataloged"
        cataloged += 1

    await append_records(effects, batch)

    downloaded = sum(1 for r in batch if r.get("pdf_path"))
    closed = sum(1 for r in batch if r.get("access_status") == "closed")
    report = {
        "flow": "acquire_catalog",
        "status": "success" if cataloged == len(batch) else "partial",
        "summary": (
            f"Cataloged {cataloged}/{len(batch)} paper(s): {downloaded} PDF(s) "
            f"downloaded, {closed} closed-access (cataloged from metadata), "
            f"{dropped} invalid tag(s) dropped."
        ),
        "headline": f"cataloged {cataloged}/{len(batch)} ({downloaded} PDFs)",
        "files_affected": [r.get("pdf_path", "") for r in batch if r.get("pdf_path")],
        "checks_passed": [f"cataloged:{cataloged}", f"downloaded:{downloaded}"],
        "checks_failed": (
            [] if cataloged == len(batch) else [f"untagged:{len(batch) - cataloged}"]
        ),
    }
    return StepOutput(
        result={
            "cataloged": cataloged,
            "tag_parse_failed": len(batch) - cataloged,
            "dropped_tags": dropped,
        },
        observations=report["headline"],
        context_updates={"directive_report": report},
    )
