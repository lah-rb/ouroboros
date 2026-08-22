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
import re
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

DATABANK_PATH = "databank/papers.jsonl"
# Extractor-owned sidecar — see read_databank for why it is separate.
EXTRACTION_PATH = "databank/extraction.jsonl"
# Append-only ledger of queries actually SENT to the search APIs. Not a
# cache and not state — an audit trail, so a later pass can be told what
# has already been tried instead of rediscovering it. See
# `record_search_queries` for why this is not folded into papers.jsonl.
QUERY_LEDGER_PATH = "databank/search_queries.jsonl"
PDF_DIR = "pdfs"
RELEVANCE_TIERS = ("exact", "close", "adjacent")
MAX_REFERENCE_DOIS = 200
# Papers per catalog dispatch. Raised 5 -> 8 with the acquire tag lane
# (2026-08-15): tagging streams in sub-batches on open seats instead of one
# monolithic post-acquire turn, so the dispatch size is an acquisition
# fan-out knob now, not a tag-quality one. The research gate's grounding
# check monitors tag quality independently.
CATALOG_BATCH_SIZE = int(os.environ.get("OUROBOROS_CATALOG_BATCH", "8"))

# Publisher hosts MEASURED to refuse a polite crawler: Cloudflare 403, unmoved
# by the browser User-Agent we already send or by following redirects. Used to
# skip them when harvesting alternate locations, and to keep the capped
# re-arm slots off records that cannot recover.
#
# HOST IS THE VARIABLE THAT PREDICTS RECOVERY — not the failure bucket, which
# is what the first version of the re-arm sorted on and why it returned 0/6 on
# its first live batch. Plain re-fetch yield, sampled per host 2026-08-13:
#
#   link.springer.com        4/6   (and 9/9, 4/4 in two earlier samples)
#   onlinelibrary.wiley.com  0/6
#   www.sciencedirect.com    0/6
#   doi.org                  0/6
#   www.mdpi.com             0/6
#
# link.springer.com was in this list and should NOT have been. It was added
# from a table of failure COUNTS (93 failures) without asking whether those
# failures were durable — they were transient, and Springer serves PDFs on a
# retry more often than not. Counting failures is not measuring refusal.
WALLED_HOSTS = (
    "sciencedirect.com",
    "onlinelibrary.wiley.com",
    "www.mdpi.com",
    "iopscience.iop.org",
    "pubs.rsc.org",
    # The DOI resolver itself: it lands on a meta-refresh stub that forwards
    # into the publisher (measured: linkinghub.elsevier.com -> sciencedirect),
    # so a doi.org URL in an unresolved record is a wall one hop away.
    "doi.org",
)


def classify_failure(failure_reason: str) -> str:
    """Which KIND of acquisition failure this was.

    `access_status: oa_unresolved` lumps together populations that need
    opposite responses, and the difference is already in `failure_reason` —
    it simply had no name. Measured over 485 unresolved records on
    2026-08-13:

      hard_wall     51%  403 WAF. Nothing client-side moves it.
      landing_page  41%  the fetch SUCCEEDED; we got a real page and failed
                         to find the PDF link on it. Tractable.
      async_pending  2%  202, publisher still preparing the file.
      wrong_asset    2%  we followed a link to a figure, not the paper.
      gone           1%  404.

    Order matters: 403 wins over everything, because a walled response is
    also served as text/html and would otherwise land in landing_page.

    Defined HERE, next to the code that writes failure_reason, so the string
    and its meaning cannot drift apart; tools/oa_triage.py imports it.
    """
    f = (failure_reason or "").lower()
    if "403" in f:
        return "hard_wall"
    if "text/html" in f:
        return "landing_page"
    if "not a pdf" in f or "magic" in f:
        return "wrong_asset"
    if "202" in f:
        return "async_pending"
    if "404" in f:
        return "gone"
    return "other"


# Buckets a re-attempt can actually help, best-yield first. Measured on plain
# re-fetch with our own production headers: landing_page 35% (7/20, Springer
# 6/6), hard_wall 5% (1/20).
RETRYABLE_BUCKETS = ("landing_page", "wrong_asset", "async_pending", "other")


# Days before a failed OA location is worth trying again.
#
# ONE DAY, AND THE NUMBER IS MEASURED RATHER THAN INTUITED. The reasoning that
# produced this feature said "weeks pass and transient blocks lift" — that was
# wrong about the timescale, and a 7-day default would have been DEAD CODE
# here. The whole retryable population of the spectra corpus was last touched
# between 0.05 and 2.16 days ago (median 0.81), because a live corpus keeps
# getting re-worked. Yet the 35% recovery (Springer 6/6) was measured against
# exactly those records. So the block lifts in HOURS, not weeks.
#
# Live filter chain on the spectra corpus right after a run (2026-08-13):
#   485 unresolved -> 261 actually had a URL tried -> 135 in a retryable
#   bucket -> 8 last touched >= 1 day ago. The other 127 sit at a median age
#   of 0.57 d and become eligible within a day, which is the point: the lane
#   drips rather than dumping, and a corpus worked daily keeps feeding it.
#
# It is still not IMMEDIATE retry, which was separately measured to yield
# nothing — the floor exists so a URL that just failed is not hammered inside
# the same run.
_RETRY_AFTER_DAYS_DEFAULT = 1.0

# After this many failed re-arms a record stops being offered a slot at all.
# 4 spans 1 + 2 + 4 + 8 = 15 days of patience under the default base, which is
# well past any transient block we have measured.
_MAX_OA_RETRIES = 4


def retry_after_days() -> float:
    """BASE age past which a failed OA location is worth trying again.

    FRACTIONAL, because integer days made the knob unusable on the corpus it
    was built for. A live corpus is re-worked daily, so its whole retryable
    population sits under 24h old (measured: max 0.94 d right after a run) —
    with an int floor of 1 the lane could only ever fire on a workspace that
    had been left alone, which is the opposite of the busy case it exists to
    serve. Also the only way to exercise it without waiting a day.

    0 disables the lane.
    """
    raw = os.environ.get("OUROBOROS_OA_RETRY_AFTER_DAYS", "").strip()
    if not raw:
        return _RETRY_AFTER_DAYS_DEFAULT
    try:
        return max(0.0, float(raw))
    except ValueError:
        logger.warning("OUROBOROS_OA_RETRY_AFTER_DAYS=%r not a number", raw)
        return _RETRY_AFTER_DAYS_DEFAULT


def retry_backoff_days(attempts: int) -> float:
    """How long THIS record must wait, given how often it has already failed.

    A FLAT horizon never gives up. The 2026-08-13 run re-armed 26 records and
    recovered none; under a flat cadence those same 26 would return every
    horizon, fail again, and re-consume the capped slots indefinitely —
    crowding out records that have not had their turn. Doubling makes a
    hopeless record cheap (it backs off to fortnightly then stops) while a
    genuinely transient one is still caught on the first or second pass, which
    is where every recovery we have measured actually happened.

    Returns inf once the attempt cap is spent: never eligible again.
    """
    base = retry_after_days()
    if base <= 0:
        return float("inf")
    if attempts >= _MAX_OA_RETRIES:
        return float("inf")
    return base * (2 ** max(0, attempts))


def _iso_age_days(value: str, *, missing: float = float("inf")) -> float:
    """Days since an ISO timestamp.

    THE DEFAULT FOR "MISSING" IS PER-FIELD, and getting it wrong once already
    broke an invariant this codebase relies on:

      `oa_retried_at` absent means NEVER RE-ARMED -> must read as infinitely
      old, so a first re-arm is allowed.

      `updated_at` absent means UNKNOWN AGE, not old. Every real write stamps
      it, so a record without one is a fixture or a hand-made row — and
      treating unknown as stale re-arms records whose locations were
      deliberately burned, which is exactly what
      `test_exhausted_locations_are_not_retried_forever` forbids.

    Hence the explicit ``missing`` parameter rather than one baked-in answer.
    """
    from datetime import datetime, timezone

    if not value:
        return missing
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return missing
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def _skip_hopeless() -> bool:
    """Skip the Unpaywall call when OpenAlex already found no OA route?

    OFF by default, deliberately. The measured saving is real (67.5% of what
    verification checks turns out closed) but OpenAlex is not the last word on
    open access, and a paper wrongly marked `closed` never gets looked at
    again. The safe half of this optimisation — ordering OA-likely records
    first — is always on and lives in the sweep selector.
    """
    return os.environ.get("OUROBOROS_SKIP_HOPELESS_UNPAYWALL", "") == "1"


def _openalex_found_nothing(rec: dict) -> bool:
    """OpenAlex looked at this paper and found no OA location.

    The `openalex_id` guard is load-bearing: records sourced from Semantic
    Scholar or CORE also carry no `oa_pdf_urls`, and treating those as
    hopeless would be skipping papers nobody has actually checked.
    """
    return bool(rec.get("openalex_id")) and not (rec.get("oa_pdf_urls") or [])


def is_stale_retry_candidate(rec: dict) -> bool:
    """Is this an old acquisition failure worth one more attempt?

    `oa_retried_at` is what stops a record being re-armed every cycle —
    without it the same handful would refill the batch reservation forever
    and the corpus goal could never complete.
    """
    if rec.get("access_status") != "oa_unresolved":
        return False
    if not (rec.get("oa_attempted") or rec.get("oa_pdf_urls")):
        return False
    horizon = retry_after_days()
    if horizon <= 0:
        return False
    if classify_failure(rec.get("failure_reason", "")) not in RETRYABLE_BUCKETS:
        return False  # hard_wall 5%, gone 0% — not worth a slot
    # HOST OVER BUCKET. The first live batch re-armed six landing_page records
    # and recovered ZERO, because bucket-only ordering happened to draw six
    # Wiley DOIs — and Wiley re-fetches at 0/6 where Springer runs 4/6. A
    # record still pointing at a walled host has nothing to gain from another
    # attempt, whatever its failure string says.
    if _is_walled(rec.get("oa_pdf_url") or ""):
        return False
    # PER-RECORD BACKOFF, not a flat cadence. A record that has already burned
    # attempts waits longer each time and eventually stops qualifying, so a
    # hopeless one cannot re-consume the capped slots forever.
    wait = retry_backoff_days(int(rec.get("oa_retry_count") or 0))
    if wait == float("inf"):
        return False
    return (
        # never re-armed -> eligible
        _iso_age_days(rec.get("oa_retried_at", "")) >= wait
        # unknown age -> NOT eligible; staleness must be positively established
        and _iso_age_days(rec.get("updated_at", ""), missing=-1.0) >= horizon
    )


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
    json_body=None,
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
        method,
        url,
        params=params,
        headers=headers,
        json_body=json_body,
        timeout=timeout,
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
            method,
            url,
            params=params,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )
    if result.status and result.status != 429:
        await _pacer().note_ok(url)
    return result


# ── Databank helpers ──────────────────────────────────────────────────


# A title shorter than this is not evidence of anything. "Editorial",
# "Introduction", "Raman spectroscopy" all recur across unrelated papers, and
# collapsing on one would suppress real work.
_TITLE_DUP_MIN_CHARS = 40


def _title_fingerprint(title: Any) -> str:
    """Normalized title, or "" when it is too short to identify a paper."""
    text = " ".join(str(title or "").lower().split())
    # Punctuation varies between sources for the same paper (en-dash vs
    # hyphen, curly vs straight quotes), so it cannot participate.
    text = "".join(c for c in text if c.isalnum() or c.isspace())
    text = " ".join(text.split())
    return text if len(text) >= _TITLE_DUP_MIN_CHARS else ""


def _title_index(databank: dict) -> dict[str, str]:
    """fingerprint -> paper_key, preferring the record worth keeping.

    When two existing records already share a title, the one with a retrieved
    PDF wins, then the one with a DOI: a later duplicate should be pointed at
    the copy we can actually use.
    """
    index: dict[str, str] = {}
    for key, rec in databank.items():
        fp = _title_fingerprint(rec.get("title"))
        if not fp:
            continue
        held = index.get(fp)
        if held is None:
            index[fp] = key
            continue
        current, other = databank.get(held) or {}, rec
        rank = lambda r: (  # noqa: E731 — local ordering, not an API
            bool(r.get("pdf_path")),
            r.get("access_status") == "oa_pdf",
            bool(r.get("doi")),
        )
        if rank(other) > rank(current):
            index[fp] = key
    return index


def corpus_languages(step_input: Any) -> list[str]:
    """Extra languages this mission collects in — normalized, English removed.

    Reads MissionConfig.corpus_languages, overridable per step via params so a
    single flow can scope a pass without changing the mission.

    English is stripped however it is spelled. A mission that listed it would
    run the English pass twice and, downstream, queue English papers to be
    translated into English.
    """
    mission = getattr(step_input, "context", {}).get("mission")
    configured = getattr(getattr(mission, "config", None), "corpus_languages", None)
    raw = step_input.params.get("corpus_languages") or configured or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    out: list[str] = []
    for code in raw:
        c = str(code).strip().lower()
        if c and c not in ("en", "eng", "english") and c not in out:
            out.append(c)
    return out


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


# The extraction sidecar's field ownership, ENFORCED AT THE WRITERS.
#
# read_databank overlays extraction.jsonl over papers.jsonl per key
# ({**base, **ext}), and within each file the LAST record replaces the
# previous one wholesale. Both writers used to accept whatever dict a
# caller passed — and callers naturally pass records that came from
# read_databank's MERGED view, silently freezing the other stage's fields
# into the wrong file. Live cost (2026-08-16): rebook scripts and the
# translation booking wrote full merged records into extraction.jsonl,
# whose stale scraper-side copies then SHADOWED every later papers.jsonl
# booking for ~40 papers — figtext_done bookings vanished on read and the
# figtext drain re-described the same paper forever. Filtering here makes
# the contract structural: no caller can cross the ownership line again.
EXTRACTION_OWNED_FIELDS = frozenset(
    {
        "paper_key",
        "updated_at",
        "extraction_status",
        "failure_reason",
        "md_path",
        "md_en_path",
        "figure_count",
        "extraction_method",
        "extraction_quality",
        "script_profile",
        "translated",
        "translation_quality",
        "translate_attempts",
        "book_progress",
        # Segment cursor for resumable extraction. WITHOUT THIS ENTRY the
        # field is silently filtered out on write, every resume starts at
        # page zero, and the durability it exists for is quietly absent —
        # the failure mode is invisible because nothing errors.
        "extract_progress",
        # The Latin-language vote's verdict. Catalog metadata also writes
        # a language on the papers side, but for the 168-paper blind-spot
        # cohort it was empty or wrong ("en" on Spanish text) — extraction
        # is the layer that actually READ the document, so its verdict is
        # the one translation should trust. Only set when detected, so an
        # absent key never shadows a real catalog value on overlay.
        "language",
    }
)


async def append_extraction_records(effects: Any, records: list[dict]) -> None:
    """Append extractor-owned fields to the sidecar, never to papers.jsonl.

    Keeps the extractor off the scraper's file so the two can run at the
    same time without losing each other's appends (see read_databank).
    Records are FILTERED to EXTRACTION_OWNED_FIELDS: a merged record from
    read_databank can be passed safely without freezing scraper-side
    fields into the sidecar, where they would shadow the scraper's file.
    """
    filtered = [
        {k: v for k, v in rec.items() if k in EXTRACTION_OWNED_FIELDS}
        for rec in records
    ]
    await _append_jsonl(effects, EXTRACTION_PATH, filtered)


async def append_records(effects: Any, records: list[dict]) -> None:
    """Append scraper records as JSONL lines (last-wins on read).

    Extraction-owned fields are DROPPED (except the shared key/timestamp):
    they live in the sidecar, which overlays this file — carrying stale
    copies here is at best noise and at worst a future shadowing bug in
    the other direction."""
    # failure_reason is the one genuinely shared field: acquisition books
    # download failures to THIS file, extraction books its own to the
    # sidecar (whose overlay wins when both are set — pre-existing
    # semantics, preserved).
    filtered = [
        {
            k: v
            for k, v in rec.items()
            if k not in EXTRACTION_OWNED_FIELDS
            # language is the second genuinely shared field: catalog
            # metadata writes it here, the extraction-side Latin-language
            # vote writes its verdict to the sidecar, and the overlay
            # rightly prefers the layer that actually READ the document.
            or k in ("paper_key", "updated_at", "failure_reason", "language")
        }
        for rec in records
    ]
    await _append_jsonl(effects, DATABANK_PATH, filtered)


async def _append_jsonl(effects: Any, path: str, records: list[dict]) -> None:
    if not records:
        return
    from agent.persistence.models import _now_iso

    lines = []
    for rec in records:
        rec = dict(rec)
        rec.setdefault("paper_key", paper_key(rec))
        rec["updated_at"] = _now_iso()
        lines.append(json.dumps(rec, ensure_ascii=False))
    payload = "\n".join(lines) + "\n"

    appender = getattr(effects, "append_file", None)
    if appender is not None:
        # True append (O_APPEND + per-path lock): concurrent bookers can
        # never drop each other's records. The old read-whole-file-then-
        # write_file idiom had a read-modify-write window that was safe only
        # while every booking happened to be serial.
        await appender(path, payload)
        return
    # Duck-typed test doubles without append_file: legacy read-modify-write.
    fc = await effects.read_file(path)
    existing = fc.content if getattr(fc, "exists", False) else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    await effects.write_file(path, existing + payload)


# ── search-query ledger ───────────────────────────────────────────────
#
# WHY A SEPARATE FILE. The queries are not a property of any paper, so
# they have no home in papers.jsonl, and the one place they DID appear —
# the `Extracted N search queries: [...]` observation string — is not
# persisted anywhere: traces record step metadata without payloads, and
# goal reports record counts. Ninety-five discovery rounds across six
# aspects therefore left no record of a single term that was tried.
#
# That is fine while a mission runs once. It stops being fine the moment
# a SECOND pass opens new goals over the same corpus, because the model
# refining queries cannot avoid ground it has already covered — the
# refine prompt has always had a "do NOT repeat these" section, and we
# had nothing to put in it beyond the original static seeds.


async def record_search_queries(effects: Any, aspect: str, entries: list[dict]) -> None:
    """Append one ledger row per query actually sent.

    Never raises: discovery must not fail because an audit write failed.
    A missing row costs a future duplicate query; a raised exception here
    would cost the round's candidates.
    """
    if not entries:
        return
    from agent.persistence.models import _now_iso

    now = _now_iso()
    lines = []
    for e in entries:
        lines.append(
            json.dumps(
                {
                    "aspect": aspect,
                    "query": e.get("query", ""),
                    "hits": int(e.get("hits") or 0),
                    "source": e.get("source", "refined"),
                    "ts": now,
                },
                ensure_ascii=False,
            )
        )
    payload = "\n".join(lines) + "\n"
    try:
        appender = getattr(effects, "append_file", None)
        if appender is not None:
            await appender(QUERY_LEDGER_PATH, payload)
            return
        fc = await effects.read_file(QUERY_LEDGER_PATH)
        existing = fc.content if getattr(fc, "exists", False) else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        await effects.write_file(QUERY_LEDGER_PATH, existing + payload)
    except Exception:  # noqa: BLE001 — an audit trail never breaks the run
        logger.warning("could not append to the query ledger", exc_info=True)


async def read_search_queries(effects: Any, aspect: str = "") -> list[dict]:
    """Ledger rows, oldest first; all aspects when `aspect` is empty."""
    try:
        fc = await effects.read_file(QUERY_LEDGER_PATH)
    except Exception:  # noqa: BLE001
        return []
    if not getattr(fc, "exists", False):
        return []
    rows: list[dict] = []
    for line in fc.content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if aspect and row.get("aspect") != aspect:
            continue
        rows.append(row)
    return rows


def _dedup_preserving_order(queries: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        k = " ".join(str(q).lower().split())
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(str(q).strip())
    return out


# ── API normalization ─────────────────────────────────────────────────

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_S2_SEARCH_FIELDS = (
    "title,abstract,externalIds,openAccessPdf,isOpenAccess,year,venue,"
    "authors,citationCount"
)
_OPENALEX_BASE = "https://api.openalex.org"
_CORE_BASE = "https://api.core.ac.uk/v3"
_EUROPEPMC_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
# `is_oa` alone is weak — it admits bronze OA (free to read on the publisher's
# own site, no PDF url), which is most of what it would keep. Pairing it with
# has_fulltext is what moved the retrievable-location share to 92%.
_OPENALEX_OA_FILTER = "is_oa:true,has_fulltext:true"


def _is_walled(url: str) -> bool:
    """Is this URL at a host measured to refuse a polite crawler?"""
    host = url.split("/")[2].lower() if "//" in (url or "") else ""
    return any(w in host for w in WALLED_HOSTS)


async def _alt_host_urls(effects: Any, doi: str, rec: dict) -> list:
    """PDF locations at hosts that are neither the publisher nor CORE.

    Two sources, both serving from their OWN domain, which is the only
    property that matters once the publisher has 403'd us:

      * OpenAlex `locations[]` minus the walled publisher hosts. Unpaywall
        ranks the version-of-record first and we take that ordering for
        figure fidelity; here we want exactly what it deprioritised.
      * Europe PMC full text, for anything that resolves to a PMCID.

    Measured 1/18 (6%) on live test 2026-08-13 — low, and worth having only
    because it is a few lines on an existing chain. Never raises: a bonus
    tier must not fail a resolution.
    """
    found: list = []
    try:
        oa = await polite_request(
            effects,
            "GET",
            f"{_OPENALEX_BASE}/works/doi:{doi}",
            params={"mailto": _contact_email(), "select": "locations"},
        )
        if oa.status == 200 and isinstance(oa.json_data, dict):
            for loc in oa.json_data.get("locations") or []:
                url = (loc or {}).get("pdf_url")
                if url and not _is_walled(url):
                    found.append(url)
    except Exception as exc:  # noqa: BLE001 — bonus tier, never fatal
        logger.debug("alt-host OpenAlex lookup failed for %s: %s", doi, exc)

    try:
        pmc = await polite_request(
            effects,
            "GET",
            f"{_EUROPEPMC_BASE}/search",
            params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "core"},
        )
        if pmc.status == 200 and isinstance(pmc.json_data, dict):
            results = (pmc.json_data.get("resultList") or {}).get("result") or []
            for item in results[:1]:
                pmcid = (item or {}).get("pmcid")
                if pmcid:
                    found.append(f"{_EUROPEPMC_BASE}/{pmcid}/fullTextPdf")
    except Exception as exc:  # noqa: BLE001
        logger.debug("alt-host EuropePMC lookup failed for %s: %s", doi, exc)

    if found:
        logger.info("alt-host: %d extra location(s) for %s", len(found), doi)
    return found


_OPENALEX_SELECT = (
    "id,doi,title,abstract_inverted_index,publication_year,primary_location,"
    "authorships,open_access,best_oa_location,locations,ids,language,"
    # The article's PAGE EXTENT, free in a request we already make. It is the
    # only signal that can tell a faithful extraction of the WRONG DOCUMENT
    # from a faithful extraction of the right one: one corpus paper is a
    # single-page PDF of an article that runs pages 37-41, and the extraction
    # is perfectly faithful to the fragment it was handed. No text-comparison
    # metric can see that, because there is nothing to compare against except
    # the fragment itself.
    "biblio,"
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
        # Page extent, when the publisher deposited one. Kept as STRINGS
        # because OpenAlex returns them that way and roman numerals, "S17"
        # supplement pages and "e01505" article numbers all occur — parsing
        # is the consumer's problem, and a consumer that cannot parse a pair
        # must decline rather than guess an extent.
        "first_page": str((work.get("biblio") or {}).get("first_page") or ""),
        "last_page": str((work.get("biblio") or {}).get("last_page") or ""),
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
    # OA-FIRST BY DEFAULT, breadth still reachable. Measured 2026-08-13 on one
    # aspect query: papers holding a retrievable OA location go 22% -> 92% of
    # the returned page under this filter, at a cost of 6% of matching corpus
    # (13,516 -> 12,652). Downstream fetch success is UNCHANGED (~17% either
    # way) — the filter does not beat the publisher wall, it stops us
    # discovering papers that were never going to yield a PDF.
    # Pass oa_only: false for a breadth / citation-graph pass.
    oa_only = step_input.params.get("oa_only")
    oa_only = True if oa_only is None else bool(oa_only)

    # MULTILINGUAL FAN-OUT, off unless the mission asks for it. Each extra
    # language is another full pass over every query against all three APIs,
    # so this is a real cost multiplier and belongs to the operator, not to a
    # default. English is always run and never listed (see
    # MissionConfig.corpus_languages).
    languages = corpus_languages(step_input)

    candidates: list[dict] = []
    s2_count = openalex_count = core_count = 0
    # Per-query yield, for the ledger. Counted as a delta on `candidates`
    # so it stays correct however many APIs the loop body grows to call.
    ledger_rows: list[dict] = []
    for query in queries:
        _hits_before = len(candidates)
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

        # One unfiltered pass (English and whatever else the query reaches),
        # then one narrowed pass per configured language. OpenAlex is the only
        # one of the three APIs that filters by language, so the fan-out lives
        # here alone rather than being faked against S2/CORE.
        for lang in [""] + languages:
            filters = [_OPENALEX_OA_FILTER] if oa_only else []
            if lang:
                filters.append(f"language:{lang}")
            oa = await polite_request(
                effects,
                "GET",
                f"{_OPENALEX_BASE}/works",
                params={
                    "search": query,
                    "per-page": max_per_query,
                    "select": _OPENALEX_SELECT,
                    "mailto": _contact_email(),
                    **({"filter": ",".join(filters)} if filters else {}),
                },
            )
            if oa.status == 200 and isinstance(oa.json_data, dict):
                for work in oa.json_data.get("results") or []:
                    candidates.append(_normalize_openalex(work, aspect_name))
                    openalex_count += 1
            else:
                logger.warning(
                    "OpenAlex search failed for %r%s: %s",
                    query,
                    f" [{lang}]" if lang else "",
                    oa.error or oa.status,
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
        ledger_rows.append(
            {
                "query": query,
                "hits": len(candidates) - _hits_before,
                # Whether the model refined this one or it fell back to the
                # aspect's static seeds — a second pass wants to know which
                # terms were CHOSEN and which were merely inherited.
                "source": (
                    "refined" if step_input.context.get("search_queries") else "seed"
                ),
            }
        )

    await record_search_queries(effects, aspect_name, ledger_rows)

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


# ── bibliography snowball: curator-approved references ───────────────
#
# QUALITY-MINDFUL EXPANSION (operator, 2026-08-21): grow the corpus from
# the bibliographies of papers the CURATOR ACCEPTED — a citation from a
# work that passed review is a better relevance signal than any search
# ranking, and a thesis's curated 300-entry bibliography is exactly the
# reading list a domain expert would hand us. Two halves, both budgeted:
#
#   mine  — extract DOIs from accepted papers' reference sections
#           (extraction-side: the metadata graph covers only 32 of 229
#           book-scale docs; the MARKDOWN has what OpenAlex does not)
#   walk  — promote DOIs cited by >=N accepted papers into candidates,
#           resolved through the OpenAlex batch filter
#
# INTERIM STOP CRITERION (pending the operator discussion): a hard cap on
# total biblio-sourced candidates (OUROBOROS_BIBLIO_MAX_CANDIDATES,
# default 2000). The walk declines once reached — expansion never
# outruns the conversation about how far it should go.

# Parens INCLUDED: pre-2000 SICI-format DOIs (10.1002/(SICI)1097-...,
# 10.1016/S1296-2074(00)01084-X) legitimately contain them, and those
# are exactly the heavily-cited classics — the first histogram's top
# entries were all SICI fragments truncated at '('. Trailing unbalanced
# close-parens are stripped after the match instead.
_DOI_IN_TEXT_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>\]};,]+", re.IGNORECASE)
# SICI-form DOIs (pre-2000 Wiley/era) additionally contain < > ; —
# characters far too dangerous for the general pattern in markdown, so
# they get their own scoped match, and their spans are removed from the
# text before the general pass (else the general pattern re-emits each
# one's truncated prefix as a phantom second DOI).
_SICI_DOI_RE = re.compile(r"10\.\d{4,9}/\(sici\)[^\s\"']+", re.IGNORECASE)


def extract_reference_dois(md: str, cap: int = MAX_REFERENCE_DOIS) -> list[str]:
    """DOIs from a paper's reference section (falls back to the tail).

    Uses the translation gate's heading locator; when no heading matches,
    scans only the FINAL THIRD of the document — bibliographies live at
    the end, and body DOIs (data citations, 'as in doi:...') would
    otherwise smuggle in references the paper never listed."""
    from agent.actions.translation_actions import _REFS_HEADING_RE

    m = _REFS_HEADING_RE.search(md or "")
    refs = md[m.start() :] if m else (md or "")[-max(2000, len(md or "") // 3) :]
    out: list[str] = []
    seen: set[str] = set()
    sici = _SICI_DOI_RE.findall(refs)
    refs = _SICI_DOI_RE.sub(" ", refs)
    for raw in sici + _DOI_IN_TEXT_RE.findall(refs):
        doi = raw.rstrip(".;,]}\"'").lower()
        # Strip a trailing close-paren only when UNBALANCED — Elsevier
        # PII DOIs end in ')...-X' legitimately, but '(10.xxxx/yyy)'
        # wrappers leave a stray one.
        while doi.endswith(")") and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        # Markdown image/link artifacts and figure paths are not DOIs.
        if doi.endswith((".png", ".jpg", ".jpeg", ".svg", ".gif")):
            continue
        if doi and doi not in seen:
            seen.add(doi)
            out.append(doi)
        if len(out) >= cap:
            break
    return out


async def action_mine_bibliographies(step_input: StepInput) -> StepOutput:
    """Mine reference DOIs from curator-ACCEPTED papers' markdown.

    Budgeted walk (params.budget / OUROBOROS_BIBLIO_MINE_PAPERS, default
    10); each paper mined once (biblio_mined_at). Prefers the English
    translation when one exists. Merges into reference_dois rather than
    replacing — the metadata graph's entries stay.
    """
    from agent.persistence.models import _now_iso

    effects = step_input.effects
    raw = os.environ.get("OUROBOROS_BIBLIO_MINE_PAPERS", "").strip()
    try:
        budget = int(step_input.params.get("budget") or (raw or 10))
    except ValueError:
        budget = 10
    if budget <= 0:
        return StepOutput(
            result={"mined": 0, "reason": "disabled"},
            observations="biblio mine disabled",
            context_updates={"biblio_mine_summary": {"mined": 0}},
        )
    databank = await read_databank(effects)
    pend = [
        r
        for r in databank.values()
        if r.get("review_status") == "accepted"
        and not r.get("biblio_mined_at")
        and (r.get("md_en_path") or r.get("md_path"))
    ]
    if not pend:
        return StepOutput(
            result={"mined": 0, "reason": "nothing unmined"},
            observations="biblio mine idle (nothing unmined)",
            context_updates={"biblio_mine_summary": {"mined": 0}},
        )
    pend.sort(key=lambda r: r.get("paper_key", ""))
    mined = 0
    new_dois = 0
    now = _now_iso()
    for rec in pend[:budget]:
        rec = dict(rec)
        path = rec.get("md_en_path") or rec.get("md_path")
        fc = await effects.read_file(str(path))
        text = fc.content if getattr(fc, "exists", False) else ""
        found = extract_reference_dois(text)
        merged = list(dict.fromkeys((rec.get("reference_dois") or []) + found))
        prior = len(rec.get("reference_dois") or [])
        rec["reference_dois"] = merged[:MAX_REFERENCE_DOIS]
        rec["biblio_mined_at"] = now
        new_dois += max(0, len(rec["reference_dois"]) - prior)
        mined += 1
        await append_records(effects, [rec])
    summary = {"mined": mined, "dois_total": new_dois, "remaining": len(pend) - mined}
    return StepOutput(
        result=summary,
        observations=(
            f"biblio mine: {mined} accepted paper(s), {summary['remaining']} remain"
        ),
        context_updates={"biblio_mine_summary": summary},
    )


async def action_biblio_snowball(step_input: StepInput) -> StepOutput:
    """Promote DOIs cited by >=N ACCEPTED papers into candidates.

    Provenance is stamped (discovery_method / cited_by_accepted) so the
    cohort's downstream accept-rate is measurable against search-sourced
    candidates — the number the stop-criteria discussion needs. Declines
    once the biblio-candidate cap is reached.
    """
    effects = step_input.effects
    min_cites = int(
        step_input.params.get("min_citations")
        or os.environ.get("OUROBOROS_BIBLIO_MIN_CITES", "2")
    )
    per_round = int(
        step_input.params.get("per_round")
        or os.environ.get("OUROBOROS_BIBLIO_PER_ROUND", "40")
    )
    cap_total = int(os.environ.get("OUROBOROS_BIBLIO_MAX_CANDIDATES", "2000"))

    def _decline(reason: str) -> StepOutput:
        summary = {"promoted": 0, "reason": reason}
        return StepOutput(
            result=summary,
            observations=f"biblio snowball idle ({reason})",
            context_updates={"biblio_summary": summary},
        )

    databank = await read_databank(effects)
    already = sum(
        1 for r in databank.values() if r.get("discovery_method") == "biblio_snowball"
    )
    if already >= cap_total:
        return _decline(
            f"cap reached ({already}/{cap_total}) — awaiting stop-criteria ruling"
        )
    have_dois = {
        str(r.get("doi") or "").lower() for r in databank.values() if r.get("doi")
    }
    counts: dict[str, int] = {}
    aspects: dict[str, list] = {}
    for r in databank.values():
        if r.get("review_status") != "accepted":
            continue
        strong = [
            t.get("aspect")
            for t in (r.get("tags") or [])
            if isinstance(t, dict) and t.get("relevance") in ("exact", "close")
        ]
        for doi in r.get("reference_dois") or []:
            d = str(doi).lower()
            if d in have_dois:
                continue
            counts[d] = counts.get(d, 0) + 1
            aspects.setdefault(d, []).extend(strong[:2])
    backlog = sorted(
        (d for d, c in counts.items() if c >= min_cites), key=lambda d: -counts[d]
    )
    if not backlog:
        return _decline(f"no unheld DOI cited {min_cites}+ times by accepted papers")
    batch = backlog[: min(per_round, cap_total - already)]

    promoted: list[dict] = []
    for start in range(0, len(batch), _BIBLIO_BATCH):
        chunk = batch[start : start + _BIBLIO_BATCH]
        resp = await polite_request(
            effects,
            "GET",
            f"{_OPENALEX_BASE}/works",
            params={
                "filter": "doi:" + "|".join(chunk),
                "per-page": _BIBLIO_BATCH,
                "select": _OPENALEX_SELECT,
                "mailto": _contact_email(),
            },
        )
        if resp.status != 200 or not isinstance(resp.json_data, dict):
            continue
        for work in resp.json_data.get("results") or []:
            d = str((work.get("doi") or "")).replace("https://doi.org/", "").lower()
            asp = aspects.get(d) or []
            top = max(set(asp), key=asp.count) if asp else ""
            rec = _normalize_openalex(work, top)
            if rec.get("doi") and rec["doi"].lower() in have_dois:
                continue
            rec["discovery_method"] = "biblio_snowball"
            rec["cited_by_accepted"] = counts.get(d, 0)
            promoted.append(rec)
    if promoted:
        await append_records(effects, promoted)
    summary = {
        "promoted": len(promoted),
        "backlog": len(backlog),
        "cap_used": already + len(promoted),
        "cap_total": cap_total,
    }
    return StepOutput(
        result=summary,
        observations=(
            f"biblio snowball: {len(promoted)} candidate(s) from "
            f"{len(backlog)} eligible (cap {already + len(promoted)}/{cap_total})"
        ),
        context_updates={"biblio_summary": summary},
    )


async def action_load_query_history(step_input: StepInput) -> StepOutput:
    """Publish the aspect's already-tried queries, for the refine prompt.

    The refine template has always carried a "do NOT repeat these" section;
    until the ledger existed the only thing available to fill it was the
    aspect's static seed list, so by round three the model was re-proposing
    terms it had already spent rounds on with no way to know.

    HITS ARE INCLUDED DELIBERATELY. A bare exclusion list tells the model
    only where not to go. The yield tells it which *directions* paid, so it
    can vary a phrasing that worked instead of only avoiding one that ran.

    Newest first and capped: the cap protects the prompt budget, and newest
    first means that when it does bite, what survives is the recent frontier
    rather than the opening rounds.
    """
    effects = step_input.effects
    aspect_name = str(step_input.params.get("aspect_name") or "")
    max_shown = int(step_input.params.get("max_shown") or 80)

    rows = await read_search_queries(effects, aspect_name)
    if not rows:
        return StepOutput(
            result={"prior_query_count": 0},
            observations="No prior queries recorded for this aspect",
            context_updates={"prior_queries": ""},
        )

    # Last write wins per distinct query, so a term tried twice is listed
    # once with its most recent yield.
    best: dict[str, dict] = {}
    for row in rows:
        key = " ".join(str(row.get("query") or "").lower().split())
        if key:
            best[key] = row
    ordered = list(best.values())[::-1][:max_shown]

    lines = [
        f"- {row.get('query')} ({int(row.get('hits') or 0)} hits)" for row in ordered
    ]
    block = "\n".join(lines)
    if len(best) > max_shown:
        block += f"\n(+{len(best) - max_shown} older queries not listed)"

    return StepOutput(
        result={"prior_query_count": len(best), "shown": len(ordered)},
        observations=(
            f"Loaded {len(ordered)} of {len(best)} prior queries for "
            f"{aspect_name or 'no aspect'}"
        ),
        context_updates={"prior_queries": block},
    )


_BIBLIO_BATCH = 40  # OR-filter length; 50 is fine for ids, 40 is safe for DOIs


async def action_enrich_paper_metadata(step_input: StepInput) -> StepOutput:
    """Fill page extent and license for retrieved papers that lack them.

    WHY THIS IS IN THE FLOW and not only in tools/backfill_biblio.py: 41% of
    acquired papers carry NO openalex_id — they reached us through S2 or CORE —
    so `_normalize_openalex` can never supply these fields for them however
    recent they are. Not a migration that shrinks to zero; a permanent gap for
    four papers in ten. The tool remains, for records already on disk.

    TWO FIELDS, ONE CALL. Both come from the same OpenAlex work and both have
    the same hole: measured on the live corpus, 128 of 270 curator-eligible
    papers (47%) carry NO license at all. License is what makes a corpus
    filterable before any training use, so an unlicensed paper is one that
    cannot safely be used — the pilot packed its first paper as
    `"license": "unknown"`.

    Placed at ACQUISITION because that is where the population is smallest and
    already known to matter: only papers whose PDF we actually hold, one
    batched lookup per dispatch, in a stage that already does polite HTTP.
    The extent is read later by extraction_actions.acquisition_is_truncated,
    which is the only thing that can tell a faithful extraction of the WRONG
    DOCUMENT from a faithful extraction of the right one.

    Context: catalog_batch (mutated in place; the tagging step persists it)
    Result: enriched, looked_up
    """
    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])

    # Only papers we actually hold, and only those still missing the field.
    def _needs_lookup(rec: dict) -> bool:
        # EITHER field missing is reason enough. Written out rather than
        # squeezed into one boolean: `not (a or b and c)` parses as
        # `not (a or (b and c))`, which silently skips every record that has a
        # page extent but no license — precisely the 237 the backfill had
        # already given an extent to.
        if not rec.get("pdf_path"):
            return False
        if not (rec.get("doi") or rec.get("openalex_id")):
            return False
        missing_extent = not (rec.get("first_page") or rec.get("last_page"))
        return missing_extent or not rec.get("license")

    todo = [r for r in batch if _needs_lookup(r)]
    if not todo or not effects:
        return StepOutput(
            result={"enriched": 0, "looked_up": 0},
            observations="No page extents to enrich",
            context_updates={"catalog_batch": batch},
        )

    by_doi = {str(r["doi"]).lower(): r for r in todo if r.get("doi")}
    by_id = {
        str(r["openalex_id"]).rsplit("/", 1)[-1]: r
        for r in todo
        if not r.get("doi") and r.get("openalex_id")
    }
    enriched = 0

    async def _apply(filter_value: str, lookup: dict, key_of) -> None:
        nonlocal enriched
        resp = await polite_request(
            effects,
            "GET",
            f"{_OPENALEX_BASE}/works",
            params={
                "filter": filter_value,
                "select": "id,doi,biblio,best_oa_location",
                "per-page": _BIBLIO_BATCH,
                "mailto": _contact_email(),
            },
        )
        if resp.status != 200 or not isinstance(resp.json_data, dict):
            logger.warning("page-extent lookup failed: %s", resp.error or resp.status)
            return
        for work in resp.json_data.get("results") or []:
            rec = lookup.get(key_of(work))
            if rec is None:
                continue
            gained = False
            biblio = work.get("biblio") or {}
            first = str(biblio.get("first_page") or "")
            last = str(biblio.get("last_page") or "")
            if (first or last) and not rec.get("first_page"):
                rec["first_page"], rec["last_page"] = first, last
                gained = True
            lic = str((work.get("best_oa_location") or {}).get("license") or "")
            if lic and not rec.get("license"):
                rec["license"] = lic
                gained = True
            enriched += 1 if gained else 0

    for i in range(0, len(by_doi), _BIBLIO_BATCH):
        chunk = list(by_doi)[i : i + _BIBLIO_BATCH]
        await _apply(
            "doi:" + "|".join(chunk),
            by_doi,
            lambda w: str(w.get("doi") or "").lower().split("doi.org/")[-1],
        )
    for i in range(0, len(by_id), _BIBLIO_BATCH):
        chunk = list(by_id)[i : i + _BIBLIO_BATCH]
        await _apply(
            "openalex_id:" + "|".join(chunk),
            by_id,
            lambda w: str(w.get("id") or "").rsplit("/", 1)[-1],
        )

    return StepOutput(
        result={"enriched": enriched, "looked_up": len(todo)},
        observations=f"Page extent: {enriched}/{len(todo)} paper(s) enriched",
        context_updates={"catalog_batch": batch},
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

    # SAME PAPER, DIFFERENT IDENTIFIER. Identifier dedup is already exact —
    # measured on a 5,556-paper corpus, zero DOIs and zero OpenAlex ids appear
    # on more than one key. What it cannot catch is one work reaching us
    # through two identities: a Research Square preprint beside its journal
    # DOI, an S2 record with no DOI beside the DOI record for the same paper,
    # the same article in a Spanish and an English venue. Measured: 55 title
    # groups, 58 surplus records.
    #
    # FLAGGED, NOT MERGED. A wrong merge destroys a record and cannot be
    # undone from the databank; a wrong flag costs one paper's acquisition and
    # is visible. Since the whole point is to avoid SPENDING on a paper we
    # already hold — fetch, OCR, and now translation — suppression buys the
    # entire benefit at none of the risk.
    title_index = _title_index(databank)
    dup_count = 0

    to_append: list[dict] = []
    new_count = merged_count = 0
    for key, cand in batch.items():
        existing = databank.get(key)
        if existing is None:
            twin = title_index.get(_title_fingerprint(cand.get("title")))
            if twin and twin != key:
                cand["duplicate_of"] = twin
                dup_count += 1
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
            f"{merged_count} existing paper(s) gained the aspect"
            + (
                f", {dup_count} flagged as a duplicate of a paper already held "
                f"(same title, different identifier — not acquired)"
                if dup_count
                else ""
            )
            + f"; the aspect now has {aspect_total} candidate(s) in the databank."
        ),
        "headline": (
            f"aspect '{aspect_name}': +{new_count} candidates ({aspect_total} total)"
            + (f", {dup_count} dup(s) suppressed" if dup_count else "")
        ),
        "checks_passed": [f"aspect:{aspect_name} candidates:{aspect_total}"],
    }
    return StepOutput(
        result={
            "new_candidates": new_count,
            "merged_existing": merged_count,
            "duplicates_flagged": dup_count,
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
        # THE STALE RE-ARM. Clearing oa_attempted makes known locations look
        # untried, so the normal resolve -> download path runs again. Nothing
        # is fetched here. Stamped so the record cannot be re-armed next cycle.
        if is_stale_retry_candidate(rec):
            from agent.persistence.models import _now_iso

            rec["oa_attempted"] = []
            rec["oa_retried_at"] = _now_iso()
            # Count BEFORE the attempt, not after a verdict: the download runs
            # in a later step and may not come back here at all. An attempt
            # that is not booked the moment it is granted is an attempt that
            # repeats for free.
            attempts = int(rec.get("oa_retry_count") or 0) + 1
            rec["oa_retry_count"] = attempts
            nxt = retry_backoff_days(attempts)
            logger.info(
                "OA re-arm: %s (%s, attempt %d/%d, last tried %s, next in %s)",
                rec.get("paper_key", "?"),
                classify_failure(rec.get("failure_reason", "")),
                attempts,
                _MAX_OA_RETRIES,
                rec.get("updated_at", "?"),
                "never" if nxt == float("inf") else f"{nxt:g}d",
            )

        urls = _dedup_urls(rec.get("oa_pdf_urls") or [rec.get("oa_pdf_url")])
        attempted = set(rec.get("oa_attempted") or [])
        # Ask Unpaywall when there is nothing to fall back to -- no location
        # at all, a single location, or a set we have already burned. A
        # paper that arrived with several live alternates needs no call.
        if len(urls) < 2 or not (set(urls) - attempted):
            doi = str(rec.get("doi") or "").strip()
            if doi and _skip_hopeless() and _openalex_found_nothing(rec):
                # OPT-IN ONLY. OpenAlex saying "no OA route" is not
                # authoritative -- Unpaywall sometimes knows better -- and a
                # wrong `closed` is a silent corpus loss, so the default keeps
                # the call and merely deprioritises these records in the
                # sweep. Set OUROBOROS_SKIP_HOPELESS_UNPAYWALL=1 to trade that
                # accuracy for throughput.
                rec["access_status"] = "closed"
                rec["oa_check"] = "openalex_only"
                closed += 1
                continue
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
            # LAST TIER: hosts that are neither the publisher nor CORE. Low
            # yield -- 1 of 18 on live test (6%) -- but it is the only route
            # left for a paper walled everywhere else, and it composes with
            # the stale re-arm above. Publisher hosts are filtered out: a
            # fifth URL at a domain measured to 403 us is not a candidate.
            if doi and not (set(urls) - attempted):
                urls = _dedup_urls(urls + await _alt_host_urls(effects, doi, rec))
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


# ── OA recovery: archive + aggregator + meta-tag routes ──────────────
#
# The oa_unresolved pool (2,244 papers at build time, 2026-08-20) is NOT
# retryable by re-fetching: 44% are publisher bot-walls (403) where a
# retry is the same request to the same wall, and 37% are landing pages
# whose declared PDF target refuses automated clients too. Live probes:
#
#   Wayback availability   PROVEN  (a 404'd Dovepress PDF -> live snapshot)
#   citation_pdf_url meta  works where the landing page itself serves us
#   CORE v3 by DOI (keyed) API works; full text lags for RECENT articles
#   MDPI direct            Akamai on landing AND pdf; OAI-PMH metadata-only
#
# So recovery = ASK SOMEONE ELSE (the Internet Archive, CORE's aggregated
# copies) or READ THE PAGE'S OWN DECLARATION (citation_pdf_url), never
# beat on the wall. Every candidate still goes through http_download's
# is-a-document checks and the polite per-host pacer.

_WAYBACK_API = "https://archive.org/wayback/available"
_WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
_CORE_SEARCH_POST = "https://api.core.ac.uk/v3/search/works"
# name-then-content and content-then-name attribute orders both occur.
_META_PDF_RE = re.compile(
    r'<meta[^>]+?(?:name|property)=["\']citation_pdf_url["\'][^>]*?'
    r'content=["\']([^"\']+)'
    r'|<meta[^>]+?content=["\']([^"\']+)["\'][^>]*?'
    r'(?:name|property)=["\']citation_pdf_url',
    re.IGNORECASE,
)


def _meta_pdf_url(html: str, base_url: str) -> str:
    """The page's own machine-declared PDF location, or ''.

    Cross-host targets are ALLOWED here, unlike LLM navigation: this is
    the publisher's structured self-declaration (Google Scholar indexing
    contract), not a model's guess — MDPI declares mdpi-res.com, Springer
    declares link.springer.com assets. http_download's document check
    remains the arbiter of what we accept.
    """
    m = _META_PDF_RE.search(html or "")
    if not m:
        return ""
    from urllib.parse import urljoin

    return urljoin(base_url, (m.group(1) or m.group(2) or "").strip())


async def _wayback_snapshot(effects: Any, url: str) -> str:
    """Newest archived PDF capture of `url`, or ''.

    CDX, not the availability API. The first sweep (2026-08-20) ran 427
    availability-based attempts to ZERO recoveries, for two reasons the
    post-mortem separated cleanly:

      * The availability API silently answered "nothing" for ~81% of
        walked papers — including one KNOWN-GOOD case (a 404'd Dovepress
        PDF probed by hand hours earlier) — and its empty answer is
        indistinguishable from "no snapshot".
      * Its `closest` snapshot is whatever was captured, which for
        walled publishers is the WALL: archived interstitials
        (text/html rejects), a figure JPEG where the source URL was a
        wrong asset, and archive-side 403 playback exclusions.

    CDX fixes both: it is the archive's real index, and
    `mimetype:application/pdf&statuscode:200` asks only for captures
    that ARE the document. Newest capture wins (latest version of the
    file); `id_` in the replay URL requests raw bytes rather than the
    toolbar-wrapped page.
    """
    r = await polite_request(
        effects,
        "GET",
        _WAYBACK_CDX,
        params={
            "url": url,
            "output": "json",
            "filter": ["statuscode:200", "mimetype:application/pdf"],
            "fl": "timestamp,original",
            "limit": "8",
        },
    )
    rows = r.json_data if r.status == 200 else None
    if not isinstance(rows, list) or len(rows) < 2:
        return ""
    ts, original = rows[-1][0], rows[-1][1]  # newest capture
    return f"https://web.archive.org/web/{ts}id_/{original}"


async def _core_fulltext_urls(effects: Any, doi: str) -> list[str]:
    """CORE's aggregated copies for a DOI: downloadUrl + source URLs.

    POST, not GET — the GET form of /search/works 500s (probed live
    2026-08-20); the JSON-body POST returns the record. Recent articles
    often index with no cached full text yet — an empty answer is lag,
    not absence, which is why recovery stamps are dated (see the action).
    """
    if not doi:
        return []
    r = await polite_request(
        effects,
        "POST",
        _CORE_SEARCH_POST,
        headers=_core_headers(),
        json_body={"q": f'doi:"{doi}"', "limit": 1},
    )
    if r.status != 200 or not isinstance(r.json_data, dict):
        return []
    hits = r.json_data.get("results") or []
    if not hits:
        return []
    h = hits[0]
    urls = []
    if h.get("downloadUrl"):
        urls.append(str(h["downloadUrl"]))
    for u in h.get("sourceFulltextUrls") or []:
        urls.append(str(u))
    return urls


async def action_recover_oa_locations(step_input: StepInput) -> StepOutput:
    """Recover oa_unresolved papers through archive/aggregator routes.

    Drain-shaped (budgeted, claim-free, declines with a reason): walks
    unrecovered oa_unresolved records — strong-tagged first, then
    deterministic — and tries, per record:

      1. Wayback snapshots of the stored OA locations
      2. the landing page's citation_pdf_url declaration (when the page
         itself serves us), plus Wayback of THAT target on a direct miss
      3. CORE's aggregated copy by DOI (keyed POST)

    A success flips the record to oa_pdf with pdf_path set, so the OCR
    lane picks it up with no further wiring. Each record is stamped
    `oa_recover_attempted_at` — one pass per record per e-poch; re-arming
    a miss is deliberate operator action (aggregators lag months for
    recent articles, so a later pass IS worth it — but on a calendar,
    not a loop).

    Params/env: budget (OUROBOROS_OA_RECOVER_PAPERS, default 6; 0 disables).
    """
    from agent.persistence.models import _now_iso

    effects = step_input.effects
    raw = os.environ.get("OUROBOROS_OA_RECOVER_PAPERS", "").strip()
    try:
        budget = int(step_input.params.get("budget") or (raw or 6))
    except ValueError:
        budget = 6

    def _decline(reason: str) -> StepOutput:
        summary = {"attempted": 0, "recovered": 0, "reason": reason}
        return StepOutput(
            result=summary,
            observations=f"oa recovery idle ({reason})",
            context_updates={"recover_summary": summary},
        )

    if budget <= 0:
        return _decline("disabled (budget 0)")
    databank = await read_databank(effects)
    pend = [
        r
        for r in databank.values()
        if r.get("access_status") == "oa_unresolved"
        and not r.get("oa_recover_attempted_at")
        and (r.get("oa_pdf_urls") or r.get("oa_pdf_url"))
    ]
    if not pend:
        return _decline("nothing unrecovered pending")

    def _prio(r: dict):
        strong = any(
            isinstance(t, dict) and t.get("relevance") in ("exact", "close")
            for t in (r.get("tags") or [])
        )
        return (0 if strong else 1, r.get("paper_key", ""))

    pend.sort(key=_prio)
    attempted = recovered = 0
    outcomes: list[dict] = []
    for rec in pend[:budget]:
        rec = dict(rec)
        attempted += 1
        rec["oa_recover_attempted_at"] = _now_iso()
        stored = list(
            rec.get("oa_pdf_urls")
            or ([rec["oa_pdf_url"]] if rec.get("oa_pdf_url") else [])
        )

        candidates: list[tuple[str, str]] = []
        for u in stored[:2]:
            snap = await _wayback_snapshot(effects, u)
            if snap:
                candidates.append(("wayback", snap))
        page = await polite_request(effects, "GET", stored[0]) if stored else None
        if page is not None and page.status == 200 and page.text:
            meta = _meta_pdf_url(page.text, stored[0])
            if meta and meta not in stored:
                candidates.append(("meta", meta))
                snap = await _wayback_snapshot(effects, meta)
                if snap:
                    candidates.append(("meta-wayback", snap))
        for u in await _core_fulltext_urls(effects, str(rec.get("doi") or "")):
            candidates.append(("core", u))

        tried = set(rec.get("oa_attempted") or [])
        seen: set[str] = set()
        for how, u in candidates:
            if not u or u in tried or u in seen:
                continue
            seen.add(u)
            key = rec.get("paper_key") or paper_key(rec)
            path = f"{PDF_DIR}/{key}.pdf"
            dl = await effects.http_download(u, path)
            rec.setdefault("oa_attempted", []).append(u)
            if dl.success:
                rec["pdf_path"] = path
                rec["oa_pdf_url"] = u
                rec["access_status"] = "oa_pdf"
                rec["failure_reason"] = ""
                recovered += 1
                outcomes.append({"paper_key": key, "via": how})
                break
        # Book PER RECORD: a recovered PDF must survive whatever stops the
        # round, and a stamped miss must not be re-walked next round.
        await append_records(effects, [rec])

    summary = {
        "attempted": attempted,
        "recovered": recovered,
        "outcomes": outcomes,
        "remaining": max(0, len(pend) - attempted),
    }
    return StepOutput(
        result=summary,
        observations=(
            f"oa recovery: {recovered}/{attempted} recovered "
            f"({', '.join(o['via'] for o in outcomes) or 'none'}); "
            f"{summary['remaining']} still unwalked"
        ),
        context_updates={"recover_summary": summary},
    )


# ── LLM landing-page navigation ───────────────────────────────────────
#
# WHAT THIS IS FOR, AND WHAT IT IS NOT. 41% of unresolved papers failed with
# the fetch SUCCEEDING: we hold a real landing page and simply did not find
# the PDF link on it. Every publisher lays that page out differently, which is
# exactly where fixed selectors lose and a model wins. This is BETTER
# NAVIGATION of a page we were served and are allowed to read.
#
# It is NOT a way past the 403s. Those return a 408-byte block page with no
# content to reason about, and the classifier below refuses to spend inference
# on them.

# One attempt per record, capped per dispatch, killable outright. Inference
# volume in the acquisition lane is the historical cause of run losses, so the
# ceiling is explicit rather than emergent.
_NAV_PER_DISPATCH = 2
_NAV_MAX_HTML_BYTES = 400_000
_NAV_MAX_CANDIDATES = 40

# Hosts that legitimately serve a PDF for a paper whose landing page lives
# elsewhere — repositories and aggregators. Anything else must match the
# landing page's own registrable domain.
_NAV_ALLOWED_HOSTS = (
    "core.ac.uk",
    "ebi.ac.uk",
    "europepmc.org",
    "ncbi.nlm.nih.gov",
    "arxiv.org",
    "zenodo.org",
    "figshare.com",
    "osti.gov",
    "hal.science",
)

_NAV_PROMPT = """You are given the links found on a scientific paper's landing page.

Return the URL that downloads the FULL-TEXT PDF of the paper itself.

Rules:
- Reply with the URL ONLY. No prose, no markdown, no quotes.
- If no link downloads the paper's full text, reply exactly: NONE
- Do NOT pick supplementary material, a figure, a cited reference, a different
  article, a citation-export file, or a "related articles" link.

Paper: {title}
Landing page: {page_url}

Links:
{links}"""


def _registrable(host: str) -> str:
    """Last two labels of a hostname — a cheap eTLD+1 stand-in.

    Good enough for "is this the same publisher": it treats
    agupubs.onlinelibrary.wiley.com and onlinelibrary.wiley.com as one site.
    It over-merges under multi-part suffixes like .co.uk, which for THIS use
    (deciding whether to follow a link the model picked, then requiring PDF
    magic before accepting it) fails safe.
    """
    parts = [p for p in (host or "").lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def _extract_links(html: str, page_url: str) -> list:
    """(absolute_url, anchor_text) for links plausibly leading to a PDF.

    The page is REDUCED before it ever reaches the model — a publisher page
    runs to hundreds of KB of navigation chrome, and sending it whole would
    cost a large prefill to answer a question about a handful of hrefs.
    """
    import re as _re
    from urllib.parse import urljoin

    out: list = []
    seen = set()
    for m in _re.finditer(
        r"<a\b[^>]*?href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        html[:_NAV_MAX_HTML_BYTES],
        _re.I | _re.S,
    ):
        href, text = m.group(1).strip(), _re.sub(r"<[^>]+>", " ", m.group(2))
        text = " ".join(text.split())[:80]
        if href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(page_url, href)
        if not url.lower().startswith(("http://", "https://")):
            continue
        blob = f"{url} {text}".lower()
        # Keep only links that LOOK paper-ish. A page has hundreds of anchors;
        # the model should be choosing among plausible ones, not reading a nav
        # bar. Anything mentioning pdf/download/fulltext/epdf qualifies.
        if not any(
            t in blob for t in ("pdf", "download", "fulltext", "full-text", "epdf")
        ):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, text))
        if len(out) >= _NAV_MAX_CANDIDATES:
            break
    return out


def _nav_url_is_allowed(candidate: str, page_url: str) -> bool:
    """May we fetch what the model returned?

    THE MODEL MUST NOT BE ABLE TO SEND US ANYWHERE. It is choosing from links
    on a page we fetched, but its output is free text and a prompt-injected
    page could name any URL at all. Restricting to the landing page's own site
    or a known repository keeps a compromised page from turning the crawler
    into a request generator against a third party.
    """
    from urllib.parse import urlparse

    try:
        cand, page = urlparse(candidate), urlparse(page_url)
    except ValueError:
        return False
    if cand.scheme not in ("http", "https") or not cand.netloc:
        return False
    if _registrable(cand.netloc) == _registrable(page.netloc):
        return True
    return any(h in cand.netloc.lower() for h in _NAV_ALLOWED_HOSTS)


async def action_navigate_landing_page(step_input: StepInput) -> StepOutput:
    """Find the PDF link on a landing page we already fetched successfully.

    Runs ONLY for records whose failure was `landing_page` — the bucket where
    the request succeeded and the page is in hand. Hard-walled papers cost no
    inference at all.

    Context: catalog_batch
    Result: navigated, attempted; Publishes: catalog_batch
    """
    from agent.persistence.models import _now_iso

    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    # OFF BY DEFAULT — the evidence says this feature is not worth its cost.
    #
    # Lifetime record: 30 attempts across four runs, 0 recoveries. The reason
    # is not the implementation; it is that the population it was built for
    # barely exists. The `landing_page` bucket was largely a CLIENT ARTIFACT —
    # httpx receiving a 3KB WAF interstitial where urllib receives the PDF —
    # and the transport fallback in http_download now takes those directly.
    #
    # Re-measured through the production path after that fix (n=30 unresolved):
    #   23%  RECOVERED outright (Springer 7/7, no inference involved)
    #   40%  hard_wall  (ScienceDirect, Wiley) — nothing to navigate
    #   27%  landing_page — but 6 of 8 are doi.org meta-refresh stubs that
    #        forward INTO a wall, not pages with a findable PDF link
    # Zero in that sample were the case this action exists for.
    #
    # Kept rather than deleted because the code is sound and tested, and a
    # publisher-mix shift could make it earn its place. Set
    # OUROBOROS_LLM_NAV=1 to re-enable — and re-measure before trusting it.
    if not effects or os.environ.get("OUROBOROS_LLM_NAV", "0") == "0":
        return StepOutput(
            result={"navigated": 0, "attempted": 0},
            observations="LLM landing-page navigation disabled",
            context_updates={"catalog_batch": batch},
        )

    navigated = attempted = 0
    for rec in batch:
        if attempted >= _NAV_PER_DISPATCH:
            break
        if rec.get("pdf_path") or rec.get("access_status") != "oa_unresolved":
            continue
        if classify_failure(rec.get("failure_reason", "")) != "landing_page":
            continue
        if rec.get("nav_attempted_at"):
            continue  # one attempt per record, ever — never a loop
        page_url = rec.get("oa_pdf_url") or ""
        if not page_url:
            continue

        attempted += 1
        rec["nav_attempted_at"] = _now_iso()

        page = await polite_request(effects, "GET", page_url)
        if page.status != 200 or not page.text:
            rec["failure_reason"] = f"nav: landing page unavailable ({page.status})"
            continue
        links = _extract_links(page.text, page_url)
        if not links:
            rec["failure_reason"] = "nav: no candidate links on the landing page"
            continue

        res = await effects.run_inference(
            _NAV_PROMPT.format(
                title=(rec.get("title") or "")[:200],
                page_url=page_url,
                links="\n".join(f"- {u}  [{t}]" for u, t in links),
            ),
            {"temperature": "0.1"},
        )
        answer = (getattr(res, "text", "") or "").strip().split()
        choice = answer[-1].strip("<>\"'`") if answer else ""
        if not choice or choice.upper() == "NONE":
            rec["failure_reason"] = "nav: model found no full-text link"
            continue
        if not _nav_url_is_allowed(choice, page_url):
            logger.warning(
                "nav: REJECTED off-domain URL %r for %s (page %s)",
                choice[:120],
                rec.get("paper_key", "?"),
                page_url,
            )
            rec["failure_reason"] = "nav: model returned an off-domain URL"
            continue

        key = rec.get("paper_key") or paper_key(rec)
        path = f"{PDF_DIR}/{key}.pdf"
        dl = await effects.http_download(choice, path)
        rec.setdefault("oa_attempted", []).append(choice)
        if dl.success:
            # http_download already refuses anything that is not a document;
            # accepting on its verdict keeps ONE definition of "is a PDF".
            rec["pdf_path"] = path
            rec["oa_pdf_url"] = choice
            rec["access_status"] = "oa_pdf"
            rec["status"] = "acquired"
            rec["failure_reason"] = ""
            rec["retrieval_method"] = "llm_nav"
            navigated += 1
            logger.info("nav: recovered %s via %s", key, choice[:120])
        else:
            rec["failure_reason"] = f"nav: {dl.error or dl.status}"

    return StepOutput(
        result={"navigated": navigated, "attempted": attempted},
        observations=(
            f"Landing-page navigation: {navigated}/{attempted} recovered"
            if attempted
            else "Landing-page navigation: nothing eligible"
        ),
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


def validate_and_stamp_tags(
    batch: list, inference_text: str, valid_aspects: set
) -> tuple[int, int]:
    """Parse a tag response and stamp valid tags onto the batch IN MEMORY.

    Tag JSON: {paper_key: [{aspect, relevance, justification}]}.
    Aspect names validate against ``valid_aspects``; relevance against
    the exact/close/adjacent enum (invalid entries dropped, counted).
    Papers with parsed tags become "cataloged"; papers the response
    skipped keep their current status untouched — already-cataloged
    records (e.g., stamped by the acquire tag lane) are therefore never
    clobbered by a later response that omits them.

    No I/O: booking stays with the caller (the serial post-hoc booking
    invariant — this function is shared by the apply_tags step and the
    acquire step's concurrent tag lane).

    Returns (cataloged, dropped_tags).
    """
    from agent.llm_json import parse_llm_json

    parsed = parse_llm_json(str(inference_text or ""))
    tag_map = parsed if isinstance(parsed, dict) else {}
    # DOT-TOLERANT LOOKUP. DOI-derived keys can end in a literal dot
    # (doi_10.6092_..._2266.) and the model reliably emits them WITHOUT it
    # — trailing periods read as punctuation. Live: one such paper was
    # re-offered 140+ dispatches, skipped every time, and blocked the
    # catalog phase from ever completing. Normalize both sides.
    norm_map = {str(k).rstrip("."): v for k, v in tag_map.items()}

    cataloged = dropped = 0
    for rec in batch:
        key = rec.get("paper_key") or paper_key(rec)
        raw_tags = tag_map.get(key)
        if raw_tags is None:
            raw_tags = norm_map.get(str(key).rstrip("."))
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
    return cataloged, dropped


def mission_valid_aspects(mission) -> set:
    """Aspect-name whitelist from a mission's research plan (empty = any)."""
    plan = getattr(mission, "research_plan", None) if mission else None
    return {a.name for a in (plan.aspects if plan else [])}


async def action_apply_paper_tags(step_input: StepInput) -> StepOutput:
    """Validate the batch tag JSON via validate_and_stamp_tags; persist.

    Papers already stamped "cataloged" (the acquire tag lane) pass
    through unchanged and are booked here — this action remains the
    single databank booking point for the batch. Builds the
    acquire_catalog directive_report.

    Context: catalog_batch, inference_response, mission
    Result: cataloged, tag_parse_failed, dropped_tags
    Publishes: directive_report
    """
    effects = step_input.effects
    batch = list(step_input.context.get("catalog_batch") or [])
    mission = step_input.context.get("mission")
    valid_aspects = mission_valid_aspects(mission)

    _, dropped = validate_and_stamp_tags(
        batch, str(step_input.context.get("inference_response", "")), valid_aspects
    )
    # BOUNDED RE-OFFERS. "Skipped papers stay in the worklist" assumed
    # transient skips; a record the model can never tag (or whose key never
    # matches) would otherwise be re-dispatched forever. Three strikes →
    # force-catalog with empty tags: it simply matches no aspect, which is
    # an honest outcome, and the sweep can finally complete.
    for rec in batch:
        if rec.get("status") == "cataloged":
            continue
        attempts = int(rec.get("tag_attempts") or 0) + 1
        rec["tag_attempts"] = attempts
        if attempts >= 3:
            rec["status"] = "cataloged"
            rec["tags"] = []
            rec["failure_reason"] = (
                f"tagging: skipped by the model {attempts}x — force-cataloged "
                "with no aspect tags"
            )
    # Counted from batch state, not summed from the stamp call: records the
    # acquire tag lane already cataloged aren't in this response, and a
    # record both stamped must not count twice.
    cataloged = sum(1 for r in batch if r.get("status") == "cataloged")

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
