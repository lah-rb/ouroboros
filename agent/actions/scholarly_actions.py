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
      "oa_pdf_urls": [str],         # every known location, best first
      "oa_attempted": [str],        # locations already tried and failed
      "tags": [{"aspect": str,
                "relevance": "exact"|"close"|"adjacent",
                "justification": str}],
      "reference_dois": [str],      # capped, from S2 references
      "referenced_works": [str],    # OpenAlex ids — snowball fuel
      "language": str,              # OpenAlex code ("en", "zh", ...)
      "license": str,               # best-OA license ("cc-by", ...)
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
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

DATABANK_PATH = "databank/papers.jsonl"
# Extractor-owned sidecar — see read_databank for why it is separate.
EXTRACTION_PATH = "databank/extraction.jsonl"
PDF_DIR = "pdfs"
RELEVANCE_TIERS = ("exact", "close", "adjacent")
MAX_REFERENCE_DOIS = 200
CATALOG_BATCH_SIZE = 5

# ── Politeness ────────────────────────────────────────────────────────

_HTTP_STATE_KEY = "scraper_http_state"

# TOTAL-REQUEST BUDGET — a DEVELOPMENT guardrail, not a corpus control.
#
# Politeness is enforced by _HOST_MIN_INTERVAL pacing and Retry-After
# backoff below; those are what protect the APIs and they are always on.
# This is a separate thing: a hard cap on how many requests a mission may
# ever make. For a scraper intended to run continuously that cap is not a
# safety property, it is an arbitrary stopping point — and it stopped a
# real corpus at 86 of 624 available OA PDFs (14%), because reference
# expansion had already spent 541 of the 600 on calls that largely 429'd.
#
# Default is now UNLIMITED. Set OUROBOROS_SCRAPER_HTTP_BUDGET to a positive
# integer for development runs where a hard stop is wanted.
_DEFAULT_REQUEST_BUDGET = 0  # 0 = unlimited

# When a budget IS set, keep this share of it for acquisition. Reference
# expansion is a nice-to-have that produces no dataset on its own; PDFs
# are the artifact. Without a reserve, expansion drains the allowance
# batch by batch and later batches never reach a download.
_ACQUISITION_RESERVE = 0.60


def _request_budget() -> int:
    """Total-request cap for this mission; 0 means unlimited."""
    raw = os.environ.get("OUROBOROS_SCRAPER_HTTP_BUDGET", "").strip()
    if not raw:
        return _DEFAULT_REQUEST_BUDGET
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "OUROBOROS_SCRAPER_HTTP_BUDGET=%r is not an integer — ignoring", raw
        )
        return _DEFAULT_REQUEST_BUDGET


_DEFAULT_MIN_INTERVAL = 2.0
# Per-host floors. The PACER may widen any of these at runtime when a host
# 429s (agent/actions/http_pacer.py) — these are the configured starting
# points and the values it decays back toward, never a rate it will exceed.
_HOST_MIN_INTERVAL = {
    # Unauthenticated shared pool is ~100 req / 5 min.
    "api.semanticscholar.org": 3.5,
    # Polite pool (mailto param) tolerates ~10 rps; stay well under.
    "api.openalex.org": 0.6,
    "api.unpaywall.org": 1.0,
    # Keyless CORE 429s within a handful of calls; a registered key lifts
    # that but the aggregator is doing us a favour, so stay unhurried.
    "api.core.ac.uk": 1.5,
}


_pacer_singleton = None


def _pacer():
    """The one pacer for this process.

    A mission owns a thread with its own event loop, so module scope IS the
    single owner. Built lazily so the S2 key (which buys a dedicated ~1 rps
    quota, vs 3.5s for the shared unauthenticated pool) is resolved once the
    environment is settled rather than at import.
    """
    global _pacer_singleton
    if _pacer_singleton is None:
        from agent.actions.http_pacer import HostPacer

        intervals = dict(_HOST_MIN_INTERVAL)
        if _s2_key():
            intervals["api.semanticscholar.org"] = 1.1
        _pacer_singleton = HostPacer(
            default_interval=_DEFAULT_MIN_INTERVAL, intervals=intervals
        )
    return _pacer_singleton


def _contact_email() -> str:
    """Contact email for polite-pool params (OpenAlex mailto, Unpaywall).

    Set OUROBOROS_CONTACT_EMAIL; the fallback is detectable as a default
    so operators notice it in API logs rather than silently impersonating
    nobody.
    """
    return os.environ.get("OUROBOROS_CONTACT_EMAIL", "ouroboros-agent@invalid.local")


_S2_KEY_FILE = "~/.s2_key"
_s2_key_cache: str | None = None  # resolved once per process


def _s2_key() -> str:
    """Semantic Scholar API key: env var, else ~/.s2_key, else ''.

    The unauthenticated shared pool 429s under contention (31 bounces in
    three discovery cycles, live); a key moves requests to a dedicated
    quota. The key file keeps the credential out of shell history and
    harness scripts — env var still wins when both are set.
    """
    global _s2_key_cache
    if _s2_key_cache is not None:
        return _s2_key_cache
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "").strip()
    if not key:
        try:
            # Sanctioned raw read: host-level API-key file OUTSIDE the workspace
            # root — the workspace-scoped effects seam cannot reach it by design.
            key = open(os.path.expanduser(_S2_KEY_FILE)).read().strip()
        except OSError:
            key = ""
    _s2_key_cache = key
    return key


def _s2_headers() -> dict | None:
    """Optional Semantic Scholar API key headers (see _s2_key)."""
    key = _s2_key()
    return {"x-api-key": key} if key else None


_CORE_KEY_FILE = "~/.coreacuk_key"
_core_key_cache: str | None = None  # resolved once per process


def _core_key() -> str:
    """CORE API key: env var, else ~/.coreacuk_key, else '' (see _s2_key)."""
    global _core_key_cache
    if _core_key_cache is not None:
        return _core_key_cache
    key = os.environ.get("OUROBOROS_CORE_API_KEY", "").strip()
    if not key:
        try:
            # Sanctioned raw read, same seam exemption as _s2_key.
            key = open(os.path.expanduser(_CORE_KEY_FILE)).read().strip()
        except OSError:
            key = ""
    _core_key_cache = key
    return key


def _core_headers() -> dict | None:
    """Optional CORE bearer headers (see _core_key)."""
    key = _core_key()
    return {"Authorization": f"Bearer {key}"} if key else None


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

    budget = _request_budget()
    if budget and _pacer().total_requests() >= budget:
        logger.warning(
            "Scraper HTTP budget exhausted (%d requests)", _pacer().total_requests()
        )
        return HttpResult(
            status=0,
            url=url,
            error=f"mission request budget exhausted ({budget})",
        )

    await _pacer().reserve(url)
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
        delay = await _pacer().note_throttled(url, retry_after)
        await asyncio.sleep(delay)
        await _pacer().reserve(url)
        result = await effects.http_request(
            method, url, params=params, headers=headers, timeout=timeout
        )
    if result.status and result.status != 429:
        await _pacer().note_ok(url)
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


async def _read_jsonl_records(effects: Any, path: str) -> dict[str, dict]:
    """paper_key -> record for one JSONL file, last-record-wins."""
    fc = await effects.read_file(path)
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


async def read_databank(effects: Any) -> dict[str, dict]:
    """paper_key -> record, last-record-wins, with the extraction sidecar
    overlaid so callers see ONE merged view and need no changes.

    TWO WRITERS, TWO FILES. append_records is a read-modify-write of the
    WHOLE file, so a scraper appending candidates and an extractor
    appending extraction_status to the same path lose each other's work —
    whichever writes second wins. That is the one thing standing between
    here and running acquisition and OCR concurrently, which is worth a
    lot: the extractor flow set contains ZERO LLM turns, so gpt-oss idles
    for the entire OCR stage (~12 min per 3-PDF dispatch).

    The stages own disjoint fields, so they get disjoint files. The
    scraper owns papers.jsonl; the extractor owns extraction.jsonl and
    overlays it here. Sidecar keys with no base record are kept rather
    than dropped, so an orphan is visible instead of silently missing.
    """
    records = await _read_jsonl_records(effects, DATABANK_PATH)
    for key, ext in (await _read_jsonl_records(effects, EXTRACTION_PATH)).items():
        base = records.get(key)
        records[key] = {**base, **ext} if base else ext
    return records


async def append_extraction_records(effects: Any, records: list[dict]) -> None:
    """Append extractor-owned fields to the sidecar, never to papers.jsonl.

    Keeps the extractor off the scraper's file so the two can run at the
    same time without losing each other's appends (see read_databank).
    """
    await _append_jsonl(effects, EXTRACTION_PATH, records)


async def append_records(effects: Any, records: list[dict]) -> None:
    """Append records as JSONL lines (last-wins semantics on read)."""
    await _append_jsonl(effects, DATABANK_PATH, records)


async def _append_jsonl(effects: Any, path: str, records: list[dict]) -> None:
    if not records:
        return
    from agent.persistence.models import _now_iso

    fc = await effects.read_file(path)
    existing = fc.content if getattr(fc, "exists", False) else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    lines = []
    for rec in records:
        rec = dict(rec)
        rec.setdefault("paper_key", paper_key(rec))
        rec["updated_at"] = _now_iso()
        lines.append(json.dumps(rec, ensure_ascii=False))
    await effects.write_file(path, existing + "\n".join(lines) + "\n")


# ── API normalization ─────────────────────────────────────────────────

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_SEARCH_FIELDS = (
    "title,abstract,externalIds,openAccessPdf,isOpenAccess,year,venue,"
    "authors,citationCount"
)
_OPENALEX_BASE = "https://api.openalex.org"
_CORE_BASE = "https://api.core.ac.uk/v3"
_OPENALEX_SELECT = (
    "id,doi,title,abstract_inverted_index,publication_year,primary_location,"
    "authorships,open_access,best_oa_location,locations,ids,language,"
    # Snowball fuel, free in a request we already make. S2's references
    # endpoint 429s hard -- it reached only 96 of 620 papers on the first
    # corpus run -- and these are OpenAlex ids, which the expansion can
    # re-fetch in batches of 50 without any DOI-resolution step.
    "referenced_works"
)


def _dedup_urls(urls: Any) -> list[str]:
    """Order-preserving dedup of a URL list, dropping blanks."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls or []:
        text = str(url or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


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
        # EVERY known OA location, best first, not just the best one. The
        # publisher copy is the version of record and is tried first, but
        # major publishers front their PDFs with commercial bot management
        # (Radware, Cloudflare) that a script cannot pass and should not
        # try to. Measured on the first spectra run: 224 of 325 resolved
        # OA papers failed to download, 128 on HTTP 403 and 79 on a
        # returned landing page -- yet 57% of a sampled 30 had a
        # repository copy (PMC, Zenodo, DOAJ, HAL, institutional) that
        # serves scripts by design. Falling back through this list uses
        # the OA infrastructure as intended; defeating the bot wall is
        # not an option we take.
        "oa_pdf_urls": [],
        # Urls already tried and failed. Retry is then idempotent: a
        # re-resolve only re-arms a paper when it turns up a location we
        # have NOT burned, so a dead link is never re-fetched forever.
        "oa_attempted": [],
        "pdf_path": "",
        "tags": [],
        "reference_dois": [],
        # OpenAlex work ids this paper cites (see _OPENALEX_SELECT).
        "referenced_works": [],
        # Dataset hooks: language routes the future translation/OCR
        # stage (zh -> Paddle-native per the bake-off); license makes
        # the corpus filterable before any training use. OpenAlex and
        # Unpaywall fill these; S2 doesn't provide them.
        "language": "",
        "license": "",
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
        "oa_pdf_urls": _dedup_urls([oa.get("url")]),
    }
    rec["paper_key"] = paper_key(rec)
    return rec


def _normalize_core(work: dict, aspect_name: str) -> dict:
    """CORE work -> candidate record.

    CORE is the acquisition source that matters: it aggregates full text
    from ~10k repositories and serves the PDF from core.ac.uk itself, so
    a paper whose publisher copy sits behind bot management is still
    reachable. Verified against a Spectrochimica Acta B paper that 403'd
    at Elsevier and downloaded as a 444 KB PDF from CORE.
    """
    doi = str(work.get("doi") or "").strip()
    if "doi.org/" in doi:
        doi = doi.split("doi.org/")[-1]
    lang = work.get("language")
    rec = {
        **_candidate_base(aspect_name),
        "title": str(work.get("title") or ""),
        "abstract": str(work.get("abstract") or ""),
        "year": work.get("yearPublished") or 0,
        "venue": str(work.get("publisher") or ""),
        "authors": [
            str(a.get("name") or "")
            for a in (work.get("authors") or [])
            if isinstance(a, dict)
        ][:25],
        "doi": doi,
        "arxiv_id": str(work.get("arxivId") or ""),
        "s2_id": "",
        "openalex_id": "",
        "oa_pdf_url": str(work.get("downloadUrl") or ""),
        "oa_pdf_urls": _dedup_urls(
            [work.get("downloadUrl")] + list(work.get("sourceFulltextUrls") or [])
        ),
        "language": str(lang.get("code") or "") if isinstance(lang, dict) else "",
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
        # locations[] rides along in the same request as best_oa_location,
        # so the alternates cost no extra call.
        "oa_pdf_urls": _dedup_urls(
            [best_oa.get("pdf_url")]
            + [
                loc.get("pdf_url")
                for loc in (work.get("locations") or [])
                if isinstance(loc, dict) and loc.get("is_oa")
            ]
        ),
        "language": str(work.get("language") or ""),
        "license": str(best_oa.get("license") or ""),
        "referenced_works": [
            str(w) for w in (work.get("referenced_works") or [])[:MAX_REFERENCE_DOIS]
        ],
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
    s2_count = openalex_count = core_count = 0
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

        # CORE last: S2 and OpenAlex are the metadata authorities, but
        # CORE is the one that brings a fetchable PDF with it, and the
        # merge pools every source's locations onto one record.
        core = await polite_request(
            effects,
            "GET",
            f"{_CORE_BASE}/search/works",
            params={"q": query, "limit": max_per_query},
            headers=_core_headers(),
        )
        if core.status == 200 and isinstance(core.json_data, dict):
            for work in core.json_data.get("results") or []:
                candidates.append(_normalize_core(work, aspect_name))
                core_count += 1
        else:
            logger.warning(
                "CORE search failed for %r: %s%s",
                query,
                core.error or core.status,
                "" if _core_key() else " (no API key — keyless CORE 429s early)",
            )

    return StepOutput(
        result={
            "results_found": len(candidates),
            "s2_count": s2_count,
            "openalex_count": openalex_count,
            "core_count": core_count,
        },
        observations=(
            f"Scholarly search ({aspect_name or 'no aspect'}): "
            f"{len(candidates)} hits across {len(queries)} query(ies) "
            f"(S2 {s2_count}, OpenAlex {openalex_count}, CORE {core_count})"
        ),
        context_updates={"raw_candidates": candidates},
    )


_SNOWBALL_BATCH = 50  # OpenAlex OR-filter ceiling per request


async def action_snowball_expand(step_input: StepInput) -> StepOutput:
    """Turn repeatedly-cited but uncollected references into candidates.

    The corpus cites far more than it contains -- measured on the first
    spectra run, 8,812 distinct referenced works against 39 collected
    (0.4%), with 204 cited two or more times and absent. A reference the
    corpus reaches for twice is a stronger relevance signal than any
    query we could write, and it costs one batched lookup.

    Scoped to the dispatching aspect: only papers already carrying that
    aspect contribute references, so expansion counts toward the coverage
    it actually serves. Appends to raw_candidates, so the normal merge
    dedups it into the databank.

    Context: raw_candidates (optional)
    Params: aspect_name, min_citations (default 2), max_expand (default 50)
    Publishes: raw_candidates
    """
    effects = step_input.effects
    aspect_name = str(step_input.params.get("aspect_name") or "")
    min_citations = int(step_input.params.get("min_citations") or 2)
    max_expand = int(step_input.params.get("max_expand") or 50)
    existing = list(step_input.context.get("raw_candidates") or [])

    databank = await read_databank(effects)
    have_dois = {
        str(rec.get("doi") or "").lower() for rec in databank.values() if rec.get("doi")
    }
    have_works = {
        str(rec.get("openalex_id") or "")
        for rec in databank.values()
        if rec.get("openalex_id")
    }
    counts: dict[str, int] = {}
    for rec in databank.values():
        if aspect_name and aspect_name not in (rec.get("source_aspects") or []):
            continue
        for work_id in rec.get("referenced_works") or []:
            wid = str(work_id)
            if wid and wid not in have_works:
                counts[wid] = counts.get(wid, 0) + 1

    backlog = sorted(
        (w for w, n in counts.items() if n >= min_citations),
        key=lambda w: -counts[w],
    )[:max_expand]
    if not backlog:
        return StepOutput(
            result={"expanded": 0, "backlog": 0},
            observations=(
                f"Snowball: nothing cited {min_citations}+ times that the "
                f"corpus does not already hold"
            ),
            context_updates={"raw_candidates": existing},
        )

    found: list[dict] = []
    for start in range(0, len(backlog), _SNOWBALL_BATCH):
        chunk = backlog[start : start + _SNOWBALL_BATCH]
        resp = await polite_request(
            effects,
            "GET",
            f"{_OPENALEX_BASE}/works",
            params={
                "filter": "openalex_id:" + "|".join(chunk),
                "per-page": _SNOWBALL_BATCH,
                "select": _OPENALEX_SELECT,
                "mailto": _contact_email(),
            },
        )
        if resp.status == 200 and isinstance(resp.json_data, dict):
            for work in resp.json_data.get("results") or []:
                rec = _normalize_openalex(work, aspect_name)
                # A reference already held under a different id is not new.
                if rec.get("doi") and rec["doi"].lower() in have_dois:
                    continue
                found.append(rec)
        else:
            logger.warning("Snowball lookup failed: %s", resp.error or resp.status)

    return StepOutput(
        result={"expanded": len(found), "backlog": len(counts)},
        observations=(
            f"Snowball ({aspect_name or 'no aspect'}): {len(found)} candidate(s) "
            f"from {len(backlog)} work(s) cited {min_citations}+ times "
            f"({len(counts)} uncollected reference(s) known)"
        ),
        context_updates={"raw_candidates": existing + found},
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
            # S2 and OpenAlex often know DIFFERENT locations for the same
            # paper, so pool their urls rather than letting one win.
            pooled = _dedup_urls(
                (existing.get("oa_pdf_urls") or [existing.get("oa_pdf_url")])
                + (cand.get("oa_pdf_urls") or [cand.get("oa_pdf_url")])
            )
            if not existing.get("abstract") and cand.get("abstract"):
                cand["source_aspects"] = existing.get("source_aspects", [])
                batch[key] = {**existing, **{k: v for k, v in cand.items() if v}}
                existing = batch[key]
            elif not existing.get("oa_pdf_url") and cand.get("oa_pdf_url"):
                existing["oa_pdf_url"] = cand["oa_pdf_url"]
            existing["oa_pdf_urls"] = pooled
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
    """Assign access_status per paper; gather EVERY OA location via Unpaywall.

    oa_pdf (at least one untried location) / oa_unresolved (locations
    exist but all have been tried and failed) / closed (no OA location
    anywhere). Closed is an ACCESS state, never a failure.

    Re-running this re-arms a previously failed paper only when it finds
    a location not already in oa_attempted, so the step is safe to sweep
    repeatedly over the whole databank.

    Context: catalog_batch
    Result: resolved, closed; Publishes: catalog_batch
    """
    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    resolved = closed = 0
    for rec in batch:
        urls = _dedup_urls(rec.get("oa_pdf_urls") or [rec.get("oa_pdf_url")])
        attempted = set(rec.get("oa_attempted") or [])
        # Ask Unpaywall when there is nothing to fall back to -- no location
        # at all, a single location, or a set we have already burned. A
        # paper that arrived with several live alternates needs no call.
        if len(urls) < 2 or not (set(urls) - attempted):
            doi = str(rec.get("doi") or "").strip()
            if doi:
                up = await polite_request(
                    effects,
                    "GET",
                    f"https://api.unpaywall.org/v2/{doi}",
                    params={"email": _contact_email()},
                )
                if up.status == 200 and isinstance(up.json_data, dict):
                    best = up.json_data.get("best_oa_location") or {}
                    # oa_locations arrives best-first, which puts the
                    # publisher's version of record ahead of repository
                    # copies -- the order we want for figure fidelity.
                    urls = _dedup_urls(
                        urls
                        + [best.get("url_for_pdf")]
                        + [
                            loc.get("url_for_pdf")
                            for loc in (up.json_data.get("oa_locations") or [])
                            if isinstance(loc, dict)
                        ]
                    )
                    if best.get("license") and not rec.get("license"):
                        rec["license"] = str(best.get("license"))
            # Still nothing fetchable. CORE aggregates repository full
            # text and serves it from its OWN domain, so it is the one
            # route that reaches a paper walled at the publisher.
            if doi and not (set(urls) - attempted):
                core = await polite_request(
                    effects,
                    "GET",
                    f"{_CORE_BASE}/search/works",
                    params={"q": f'doi:"{doi}"', "limit": 3},
                    headers=_core_headers(),
                )
                if core.status == 200 and isinstance(core.json_data, dict):
                    urls = _dedup_urls(
                        urls
                        + [
                            work.get("downloadUrl")
                            for work in (core.json_data.get("results") or [])
                            if isinstance(work, dict)
                        ]
                    )
        if urls:
            rec["oa_pdf_urls"] = urls
            rec["oa_pdf_url"] = urls[0]
        if set(urls) - attempted:
            rec["access_status"] = "oa_pdf"
            resolved += 1
        elif urls:
            # Every known location has been tried. Not closed -- open in
            # principle, unreachable in practice, and distinguishing the
            # two keeps the corpus honest about why a paper is missing.
            rec["access_status"] = "oa_unresolved"
        else:
            rec["access_status"] = "closed"
            closed += 1
    return StepOutput(
        result={"resolved": resolved, "closed": closed},
        observations=f"OA resolution: {resolved} open, {closed} closed",
        context_updates={"catalog_batch": batch},
    )


async def action_download_papers(step_input: StepInput) -> StepOutput:
    """Download oa_pdf papers to pdfs/<paper_key>.pdf.

    Tries every known OA location in turn, recording each in oa_attempted
    so a later sweep neither repeats a dead link nor gives up on a paper
    that has gained a new one. Exhausting them all downgrades
    access_status to oa_unresolved; the paper still proceeds to tagging —
    metadata + abstract are enough for the catalog.

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
        attempted = list(rec.get("oa_attempted") or [])
        candidates = [
            url
            for url in _dedup_urls(rec.get("oa_pdf_urls") or [rec.get("oa_pdf_url")])
            if url not in set(attempted)
        ]
        # Walk the locations until one yields a PDF. The publisher copy
        # leads and is usually blocked by bot management; the repository
        # copies behind it are what actually land.
        last_error = ""
        for url in candidates:
            dl = await effects.http_download(url, path)
            attempted.append(url)
            if dl.success:
                rec["pdf_path"] = path
                rec["oa_pdf_url"] = url
                rec["status"] = "acquired"
                rec["failure_reason"] = ""
                break
            last_error = str(dl.error or f"HTTP {dl.status}")
        rec["oa_attempted"] = attempted
        if rec.get("pdf_path"):
            downloaded += 1
        else:
            rec["access_status"] = "oa_unresolved"
            rec["failure_reason"] = (
                f"{last_error} (tried {len(attempted)} location(s))"
                if last_error
                else rec.get("failure_reason", "")
            )
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

    # ACQUISITION OUTRANKS EXPANSION. Reference expansion produces no
    # dataset by itself; downloaded PDFs are the artifact. When a budget is
    # in force, decline once the remaining allowance is inside the
    # acquisition reserve, so later batches can still fetch their papers.
    # Measured on the run that motivated this: expansion took 541 of a
    # 600-request budget — most of it 429'd — and acquisition finished at
    # 86 of 624 available OA PDFs.
    budget = _request_budget()
    if budget:
        # Counted by the pacer now, not mission state — same number, one owner.
        used = _pacer().total_requests()
        if used >= budget * (1.0 - _ACQUISITION_RESERVE):
            logger.info(
                "Reference expansion yielding to acquisition "
                "(%d/%d requests used, %.0f%% reserved for PDFs)",
                used,
                budget,
                _ACQUISITION_RESERVE * 100,
            )
            return StepOutput(
                result={"reference_dois": [], "yielded_to_acquisition": True},
                observations=(
                    f"Reference expansion skipped — {used}/{budget} requests used "
                    f"and the remaining allowance is reserved for PDF acquisition, "
                    f"which is what produces the corpus."
                ),
                context_updates={"reference_dois": []},
            )

    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    fetched = skipped = 0
    for rec in batch:
        if rec.get("reference_dois"):
            continue
        # OPENALEX ALREADY ANSWERED THIS, FOR FREE. `referenced_works` rides
        # along in the search response (_OPENALEX_SELECT) and _normalize_openalex
        # stores it, so calling S2's references endpoint for the same paper buys
        # nothing and spends the scarcest quota we have: S2 bounced 296 of 669
        # requests (44%) on the 2026-08-11 run, and this endpoint is most of its
        # volume — one call per paper. Snowball already consumes referenced_works
        # directly, so the corpus loses nothing by preferring it here too.
        if rec.get("referenced_works"):
            skipped += 1
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
