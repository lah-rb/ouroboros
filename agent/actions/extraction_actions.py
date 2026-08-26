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
import re
import time

from agent.models import StepInput, StepOutput
from agent.paths import repo_root as _repo_root

logger = logging.getLogger(__name__)

EXTRACT_BATCH_SIZE = 3  # ~33 pages/paper × ~7s/page ≈ 12 min/dispatch
# WHOLE-ROUND budget. Checked between papers, never mid-paper: a running
# extraction is left to finish or hit its own item timeout.
EXTRACT_TIMEOUT_S = 1800
# PER-PAPER budget. Extraction runs one subprocess per paper (see
# _extract_one), so the round budget above cannot also serve as the item
# budget — reused directly it would silently become N x itself. A German
# dissertation at ~4.3 s/page (measured on this rig) needs ~15 min for 200
# pages; 600 s covers the sub-oversize population with margin, and anything
# larger routes to the book lane instead.
_EXTRACT_ITEM_TIMEOUT_S = int(os.environ.get("OUROBOROS_EXTRACT_ITEM_TIMEOUT_S", "600"))

# Statuses this stage will not revisit. extract_unverified belongs here:
# another OCR pass over a scan with no text layer yields the same
# unverifiable result, so re-queuing it burns GPU forever.
_TERMINAL_EXTRACTION = (
    "extracted",
    "extract_failed",
    "extract_unverified",
    # A non-Latin paper whose numerics verified but whose span score is an
    # artifact of measuring English prose that isn't there. OCR-terminal
    # (another pass reads the same script and scores the same); pending for
    # the TRANSLATION drain, which owns _translation_pending below. Excluded
    # from the curator until translation books it back to "extracted".
    "extract_lingual",
    # Translation exhausted its retry — reasons recorded, human review.
    "translate_failed",
    # A book, referred for a human decision rather than attempted. Terminal
    # here so the sweep stops offering it; it is a REVIEW QUEUE, not a
    # rejection — see the oversize branch below.
    "extract_oversize",
    # Pre-OCR triage read the first page and found no geological subject —
    # a polymer FTIR study, a sensor-network paper, a nuclear-institute
    # annual report. Measured: 25 of a hand-labelled 40 in the queue.
    #
    # A DISTINCT status, deliberately, and not extract_failed: nothing here
    # failed. This is a REVIEW QUEUE like extract_oversize — the verdict and
    # its reason are recorded, so a human can list them
    # (`extraction_status == "extract_off_topic"`) and clear the field to
    # send any of them back through. Reviews are NOT skipped: their
    # reference lists feed citation mining, so they stay in the queue at
    # low priority.
    "extract_off_topic",
    # The curate stage's park for a doc whose DEEPEST compression still
    # exceeds the engine's per-stream seat (measured script-aware, or
    # refused by the engine itself at admission). Terminal HERE so the OCR
    # sweep never re-selects a paper whose markdown already exists — the
    # problem is curation geometry, not extraction. A REVIEW QUEUE like the
    # two above: reason recorded, clearable by hand. Distinct from
    # extract_oversize (a PDF too big to OCR), which the book lane segments;
    # this status must NOT be picked up by that lane.
    "curate_oversize",
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
# Verdict routing for non-Latin papers: when numerics verify but span fails
# AND at least this fraction of the markdown's letters are non-Latin, the
# span deficit is the instrument (an English-prose metric), not the
# extraction — book extract_lingual for the translation drain instead of
# burning the retry ladder. Census basis: the clearly-lingual failures ran
# 0.35–0.99 non-Latin; the English high-numeric failures (real fidelity
# issues) ran < 0.05.
LINGUAL_NONLATIN_MIN = 0.15

# Unverified-but-clean scans (operator policy, 2026-08-22) still need a
# floor of REAL CONTENT: the incident this guards is a JPEG served as
# the "PDF" that produced a 276-byte, 1-page markdown scoring a vacuous
# 1.00/1.00. Two thousand bytes is well under one real OCR'd page and
# far over any stub.
UNVERIFIED_MIN_MD_BYTES = 2000

# TABLE-DOMINANT documents (operator policy, 2026-08-22): the span rate
# is a PROSE metric — it matches flowing text against the layer — and a
# band-assignment table rendered as <td> markup fails it while carrying
# exactly the numbers the corpus wants. Measured over the failed pool:
# span-only failures hold a median 45% of their bytes in table markup vs
# 2% for numeric failures, and the 42 rescued papers' numeric rates run
# p50 0.94. The NUMERIC bar is never waived — numbers are the point.
# (A math-dense analogue exists — 10 span-only fails with heavy $..$ and
# no tables — recorded but not yet acted on; the evidence is thinner.)
TABLE_DOMINANT_MIN_FRAC = 0.30


def _table_fraction(text: str) -> float:
    """Fraction of bytes on table-markup lines (td/tr/table or pipe rows)."""
    if not text:
        return 0.0
    tbl = sum(
        len(l)
        for l in text.splitlines()
        if "<td" in l or "<tr" in l or "<table" in l or l.count("|") >= 4
    )
    return tbl / max(1, len(text))


def script_nonlatin_frac(profile: dict) -> float:
    """Non-Latin letter fraction from a report's script_profile ({} -> 0)."""
    try:
        return float((profile or {}).get("nonlatin") or 0.0)
    except (TypeError, ValueError):
        return 0.0


_MD_TEXT_CACHE: dict = {}


def _batch_md_text(working_dir: str, rep: dict):
    """The tool-written markdown for a report, cached per path, None when
    unreadable. Bounded read — table fraction stabilizes long before 800KB."""
    md_rel = rep.get("md_path")
    if not (working_dir and md_rel):
        return None
    path = os.path.join(working_dir, "databank", str(md_rel))
    if path not in _MD_TEXT_CACHE:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                _MD_TEXT_CACHE[path] = fh.read(800_000)
        except OSError:
            _MD_TEXT_CACHE[path] = None
        if len(_MD_TEXT_CACHE) > 64:
            _MD_TEXT_CACHE.pop(next(iter(_MD_TEXT_CACHE)))
    return _MD_TEXT_CACHE[path]


def _md_size(working_dir: str, md_rel) -> int:
    """On-disk size of a tool-written markdown, 0 when unreadable."""
    if not (working_dir and md_rel):
        return 0
    try:
        return os.path.getsize(os.path.join(working_dir, "databank", str(md_rel)))
    except OSError:
        return 0


def markdown_script_profile(text: str) -> dict:
    """Letter-class fractions of markdown text. Mirrors
    tools/pdf_extract/extract_batch.py::_script_profile (separate venvs —
    keep in sync); used by the one-time rebook and any consumer without a
    fresh tool report."""
    ranges = (
        ("latin", ((0x0041, 0x024F),)),
        ("cyrillic", ((0x0400, 0x04FF),)),
        ("greek", ((0x0370, 0x03FF),)),
        (
            "cjk",
            (
                (0x3000, 0x30FF),
                (0x3400, 0x4DBF),
                (0x4E00, 0x9FFF),
                (0xFF00, 0xFFEF),
            ),
        ),
        ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),
        (
            "arabic",
            ((0x0600, 0x06FF), (0x0750, 0x077F), (0xFB50, 0xFDFF), (0xFE70, 0xFEFF)),
        ),
        ("hebrew", ((0x0590, 0x05FF),)),
        ("thai", ((0x0E00, 0x0E7F),)),
        ("devanagari", ((0x0900, 0x097F),)),
    )
    counts = {name: 0 for name, _ in ranges}
    total = 0
    for ch in text:
        cp = ord(ch)
        for name, rs in ranges:
            if any(lo <= cp <= hi for lo, hi in rs):
                counts[name] += 1
                total += 1
                break
    if not total:
        return {**{k: 0.0 for k in counts}, "nonlatin": 0.0}
    out = {k: round(v / total, 3) for k, v in counts.items()}
    out["nonlatin"] = round(1.0 - counts["latin"] / total, 3)
    return out


# SCRIPT IS NOT LANGUAGE. The lingual gate above keys on non-Latin
# LETTERS, so Spanish, French, Portuguese and German sail through it as
# "English" — the 2026-08-20 census found 168 extracted papers
# predominantly in those languages with no lingual verdict, 22 already
# accepted and packed. The packs feed a 1B trainee that should not be
# handed mixed-language record cards, so Latin-script language gets its
# own vote: distinctive stopwords, counted against English's own.
_LATIN_STOPWORDS = {
    "de": {
        "und",
        "der",
        "die",
        "das",
        "nicht",
        "wurde",
        "werden",
        "durch",
        "mit",
        "für",
        "eine",
        "ist",
    },
    "fr": {
        "les",
        "des",
        "dans",
        "une",
        "est",
        "être",
        "avec",
        "pour",
        "sont",
        "cette",
        "par",
        "nous",
    },
    "es": {
        "los",
        "las",
        "una",
        "está",
        "como",
        "para",
        "por",
        "con",
        "del",
        "han",
        "más",
        "entre",
    },
    "pt": {
        "não",
        "uma",
        "são",
        "com",
        "para",
        "dos",
        "das",
        "foi",
        "como",
        "mais",
        "pela",
    },
    "it": {
        "della",
        "delle",
        "sono",
        "con",
        "per",
        "una",
        "più",
        "anche",
        "come",
        "nel",
        "alla",
    },
    "en": {
        "the",
        "and",
        "with",
        "were",
        "was",
        "from",
        "this",
        "that",
        "which",
        "have",
        "been",
    },
}
# Census separation was clean: flagged papers voted 0.51-0.79 for their
# language; English papers vote ~0.9+ for "en". 0.5 sits in the gap.
LINGUAL_LATIN_MIN_CONF = 0.5

_LATIN_WORD_RE = re.compile(r"[a-zA-Z\u00c0-\u00ff\u0100-\u017f]+")


def latin_language_vote(text: str) -> tuple[str, float]:
    """(language, confidence) by stopword vote over the first ~4k words.

    ("", 0.0) when the text is too short to judge — never guess a
    language from a page of table furniture.
    """
    words = _LATIN_WORD_RE.findall((text or "").lower())[:4000]
    if len(words) < 200:
        return "", 0.0
    from collections import Counter

    counts = Counter(words)
    scores = {lang: sum(counts[w] for w in ws) for lang, ws in _LATIN_STOPWORDS.items()}
    total = sum(scores.values())
    if not total:
        return "", 0.0
    best = max(scores, key=scores.get)
    return best, scores[best] / total


def collapse_degenerate_runs(
    text: str, limit: int = 200, max_period: int = 24
) -> tuple[str, int]:
    """Collapse back-to-back periodic word repeats longer than ``limit``
    into one unit plus an explicit marker. Returns (text, words_collapsed).

    A decode loop is a localized artifact: the census of every degenerate
    extract_failed paper (2026-08-16) showed faithful documents — numeric
    0.93–0.94, span 0.70–0.79, both PASSING — condemned wholesale for one
    looping region (a Korean paper looping 300× on stray hanzi; a Spanish
    thesis with 3,813 junk words across 186 otherwise-clean pages). The
    loop marks where the model read NOTHING — the marker says so instead
    of the junk pretending to be content. Mirrored in
    tools/pdf_extract/extract_batch.py (separate venvs — keep in sync).
    """
    import re as _re

    toks = list(_re.finditer(r"\S+", text))
    if len(toks) < limit:
        return text, 0
    words = [t.group() for t in toks]
    # Find runs (junk_start_tok, end_tok, period).
    spans: list[tuple[int, int, int]] = []
    for period in range(1, max_period + 1):
        run = 0
        for i in range(period, len(words) + 1):
            if i < len(words) and words[i] == words[i - period]:
                run += 1
            else:
                if run + period > limit:
                    # Keep the first unit; junk = the repeats after it.
                    spans.append((i - run, i, period))
                run = 0
    if not spans:
        return text, 0
    # Merge overlaps (different periods can flag the same region); splice
    # the ORIGINAL string by character positions, from the end, so the
    # document's newlines/tables/headings outside the junk stay intact.
    spans.sort()
    merged = [list(spans[0])]
    for s, e, p in spans[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, p])
    collapsed = 0
    out = text
    for s, e, p in reversed(merged):
        unit = " ".join(words[s : s + p])
        marker = (
            f"*[degenerate OCR run collapsed: {e - s} repeated words of "
            f"{unit[:40]!r} — content at this location was not read]*"
        )
        out = out[: toks[s].start()] + marker + out[toks[e - 1].end() :]
        collapsed += e - s
    return out, collapsed


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

# TOOLCHAIN FAULTS, as they appear in a report's `error` string. These name
# the machinery — an HTTP status from the VLM server, a Python exception that
# escaped the worker, a dead connection — never the document. Matching on text
# is crude, and deliberately narrow: a phrase here wrongly makes a paper
# immortal in the worklist, so the list holds only strings that cannot describe
# a PDF.
#
# Checked against every `error` string the corpus has recorded (81 lifetime
# extraction failures): these match the 2 real server faults and none of the
# document-quality ones.
_TOOLCHAIN_FAULT_MARKERS = (
    "error code:",  # `Error code: 500 - {...}` from the VLM server
    # urllib's spelling of a server-side failure (`HTTPError: HTTP Error
    # 500`). 5xx only: the server broke, which says nothing about the
    # document. A 4xx names the request and stays a real verdict.
    "http error 5",
    "runtimeerror",
    "connectionerror",
    "connection refused",
    "connection reset",
    # "timed out", never the noun "timeout": the oversize verdict's own prose
    # says "a dispatch shares one timeout, so a book takes its batch down with
    # it", and a bare noun match made that 560-page book a permanent resident
    # of the worklist. Caught by running this matcher over all 1,003 recorded
    # failure strings, not by the unit fixtures — which used a short oversize
    # string that happened not to contain the word.
    "timed out",
    "traceback",
    "oserror",
    "brokenpipeerror",
)


def is_toolchain_fault(error: str) -> bool:
    """The machinery failed, not the paper.

    A paper rejected on its content has been JUDGED and the verdict stands.
    A paper whose extraction hit a 500 has not been judged at all, and the
    difference decides whether it stays in the corpus's future.
    """
    low = (error or "").lower()
    return any(marker in low for marker in _TOOLCHAIN_FAULT_MARKERS)


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


def _translation_pending(record: dict) -> bool:
    """A record the TRANSLATION drain owes work to: lingual verdict with a
    markdown on disk to translate."""
    return record.get("extraction_status") == "extract_lingual" and bool(
        record.get("md_path")
    )


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


# ── OCR claims ────────────────────────────────────────────────────────
#
# Nothing marks a paper as in-flight: extraction_status is written AFTER
# the fact, so two concurrent selectors (the acquire overlap lane and the
# parallel ocr_drain branch) would OCR the same PDFs twice. The claim set
# closes that window. Plain set ops suffice — every selector runs on the
# one asyncio loop with no awaits between check and claim. IN-PROCESS
# ONLY: a second agent process would need a flock'd claim file (the events
# queue is the precedent); until then, one mission = one process stands.
_OCR_CLAIMS: set[str] = set()


def select_ocr_batch(databank: dict, max_pdfs: int) -> list[str]:
    """Pick up to max_pdfs unclaimed pending keys and CLAIM them. Callers
    must release_ocr_keys() in a finally.

    Order: papers already PART-EXTRACTED first, then needs_reextract, then
    by CONTENT PRIORITY, then the rest. Finish-first matters more than it
    looks — a half-extracted paper is holding banked part files and a page
    cursor, and every round that starts something else instead leaves that
    work unfinished on disk while the queue grows around it. A bounded retry
    still outranks fresh work for the original reason: it should not queue
    behind the backlog.

    `content_priority` is the pre-OCR triage's verdict (0 thin-bin, 1 normal,
    2 review). It sorts BELOW the two durability rules on purpose: finishing
    banked work and honouring a retry both beat starting a better paper.
    Untriaged papers default to 1, so a queue with no triage keeps exactly
    its previous order.
    """
    pending = [
        (k, r)
        for k, r in databank.items()
        if _extraction_pending(r) and r.get("pdf_path") and k not in _OCR_CLAIMS
    ]

    def _content_priority(rec: dict) -> int:
        try:
            v = rec.get("content_priority")
            return int(v) if v is not None and str(v) != "" else 1
        except (TypeError, ValueError):
            return 1

    pending.sort(
        key=lambda kr: (
            not (kr[1].get("extract_progress") or {}).get("parts"),
            kr[1].get("extraction_status") != "needs_reextract",
            _content_priority(kr[1]),
        )
    )
    keys = [k for k, _ in pending[:max_pdfs]]
    _OCR_CLAIMS.update(keys)
    return keys


def release_ocr_keys(keys: list[str]) -> None:
    _OCR_CLAIMS.difference_update(keys)


# ── book segments (the oversize interleave) ───────────────────────────
#
# The oversize referral exists because a book monopolizes a shared dispatch
# timeout — NOT because paddle cannot read it: OCR is page-by-page, so a
# 560-page volume stresses the model exactly as much as a 5-page paper.
# The interleave therefore splits TEMPORALLY: with the regular queue empty,
# each drain round extracts one ~OUROBOROS_BOOK_PAGES segment (default 40,
# ~4 min; 0 disables) via the tool's --page-range mode, records progress on
# the record (book_progress, extraction-owned), and on the final segment
# assembles the part markdowns and applies the standard verdict.


def _extract_pages() -> int:
    """Pages per extraction segment.

    THE UNIT OF LOSS. Extraction is resumable at segment granularity: a
    kill, a pause, or a deadline costs at most the segment in hand, and
    everything before it is already banked. So this number is really two
    answers at once — how much work a stop can throw away, and how long a
    pause takes to take effect.

    25 pages is ~110 s at the 4.3 s/page measured on this rig. Most papers
    (median ~12 pages) still finish in a single segment, so the common case
    pays nothing for the property; a 250-page dissertation becomes ten
    resumable pieces instead of one 18-minute all-or-nothing gamble.
    """
    raw = os.environ.get("OUROBOROS_EXTRACT_PAGES", "").strip()
    try:
        return max(1, int(raw)) if raw else 25
    except ValueError:
        return 25


async def _mission_paused(effects) -> bool:
    """Has the operator asked the mission to stop?

    Checked BETWEEN segments so a pause lands at a page boundary with
    progress banked, rather than killing an extraction mid-document. Any
    failure reads as "not paused": a mission we cannot load is not a
    reason to stop doing work.
    """
    try:
        mission = await effects.load_mission()
    except Exception:  # noqa: BLE001
        return False
    return str(getattr(mission, "status", "") or "") == "paused"


def _part_from(rep: dict, start: int, seg: int) -> dict:
    """One banked segment, in the shape aggregate_book_parts consumes."""
    return {
        "range": rep.get("page_range") or [start, start + seg],
        "md_path": rep.get("md_path") or "",
        "verified_pages": rep.get("verified_pages", 0),
        "unverified_pages": rep.get("unverified_pages", 0),
        "numeric_match_rate": rep.get("numeric_match_rate", 0),
        "span_pass_rate": rep.get("span_pass_rate", 0),
        "max_repeat_words": rep.get("max_repeat_words", 0),
        "figures_kept": rep.get("figures_kept", 0),
    }


def _assemble_segments(working_dir: str, key: str, parts: list[dict]) -> str:
    """Join banked part files in page order and write the paper's markdown.

    Shared by both segmented lanes. Parts are ordered by their START PAGE
    rather than by insertion, so a resumed run that re-banked a segment
    out of order still assembles the document in reading order.
    """
    md_dir = os.path.join(working_dir, "databank", "markdown")
    texts = []
    for p in sorted(parts, key=lambda q: int((q.get("range") or [0])[0])):
        pp = os.path.join(working_dir, "databank", p.get("md_path") or "")
        if os.path.isfile(pp):
            texts.append(open(pp, encoding="utf-8", errors="replace").read())
    assembled = "\n\n---\n\n".join(texts)
    os.makedirs(md_dir, exist_ok=True)
    with open(os.path.join(md_dir, f"{key}.md"), "w", encoding="utf-8") as f:
        f.write(assembled)
    return assembled


def _book_pages() -> int:
    raw = os.environ.get("OUROBOROS_BOOK_PAGES", "").strip()
    try:
        return max(0, int(raw)) if raw else 40
    except ValueError:
        return 40


def select_book(databank: dict) -> str | None:
    """One unclaimed oversize book with a PDF on disk (deterministic order)."""
    for key in sorted(databank):
        r = databank[key]
        if (
            r.get("extraction_status") == "extract_oversize"
            and r.get("pdf_path")
            and key not in _OCR_CLAIMS
        ):
            return key
    return None


def aggregate_book_parts(parts: list[dict]) -> dict:
    """Weighted aggregate of per-segment report metrics (weights: verified
    pages; a part with none contributes only its unverified count)."""
    vp = sum(int(p.get("verified_pages") or 0) for p in parts)
    up = sum(int(p.get("unverified_pages") or 0) for p in parts)

    def wavg(field):
        if not vp:
            return 0.0
        return (
            sum(
                float(p.get(field) or 0.0) * int(p.get("verified_pages") or 0)
                for p in parts
            )
            / vp
        )

    return {
        "verified_pages": vp,
        "unverified_pages": up,
        "numeric_match_rate": round(wavg("numeric_match_rate"), 4),
        "span_pass_rate": round(wavg("span_pass_rate"), 4),
        "max_repeat_words": max(
            (int(p.get("max_repeat_words") or 0) for p in parts), default=0
        ),
        "pages": vp + up,
    }


async def _book_segment_round(
    step_input: StepInput, working_dir: str, seg_override: int | None = None
) -> dict:
    """Extract ONE segment of one claimed book; assemble + book on the
    final segment. Returns a summary dict; never raises. ``seg_override``
    shrinks the slice when the round also ran a regular batch — paddle is
    shared, and a full 40-page segment under contention overran its
    timeout every time (measured: zero in-run segments across 1.5 h while
    a direct uncontended run finished 40 pages cleanly)."""
    from agent.actions.scholarly_actions import (
        append_extraction_records,
        read_databank,
    )

    effects = step_input.effects
    seg = seg_override if seg_override else _book_pages()
    if seg <= 0:
        return {"book": "", "reason": "disabled"}
    databank = await read_databank(effects)
    key = select_book(databank)
    if key is None:
        return {"book": "", "reason": "no unclaimed book"}
    rec = dict(databank[key])
    _OCR_CLAIMS.add(key)
    try:
        progress = dict(rec.get("book_progress") or {"next_page": 0, "parts": []})
        start = int(progress.get("next_page") or 0)
        root = _repo_root()
        pdf = rec["pdf_path"]
        if not os.path.isabs(pdf):
            pdf = os.path.join(working_dir, pdf)
        cmd = [
            os.path.join(root, _TOOL_PY),
            os.path.join(root, _TOOL_SCRIPT),
            "--pdfs",
            pdf,
            "--keys",
            key,
            "--databank-dir",
            os.path.join(working_dir, "databank"),
            "--vl-backend",
            _VL_BACKEND,
            "--page-range",
            f"{start}:{start + seg}",
        ]
        result = await effects.run_command(cmd, timeout=seg * 12 + 300)
        rep = None
        for line in (result.stdout or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("paper_key") == key:
                rep = r
        if rep is None or rep.get("error"):
            # Transport-shaped: leave progress untouched; the next round
            # retries the same segment (mirrors the toolchain-fault rule).
            return {
                "book": key,
                "segment": [start, start + seg],
                "reason": (rep or {}).get("error") or "no report from tool",
            }

        total = int(rep.get("total_pages") or 0)
        part = {
            "range": rep.get("page_range") or [start, start + seg],
            "md_path": rep.get("md_path") or "",
            "verified_pages": rep.get("verified_pages", 0),
            "unverified_pages": rep.get("unverified_pages", 0),
            "numeric_match_rate": rep.get("numeric_match_rate", 0),
            "span_pass_rate": rep.get("span_pass_rate", 0),
            "max_repeat_words": rep.get("max_repeat_words", 0),
            "figures_kept": rep.get("figures_kept", 0),
        }
        progress["parts"] = list(progress.get("parts") or []) + [part]
        progress["next_page"] = min(start + seg, total)
        progress["total_pages"] = total
        rec["book_progress"] = progress

        if progress["next_page"] < total:
            await append_extraction_records(effects, [rec])
            return {
                "book": key,
                "segment": [start, start + seg],
                "done_pages": progress["next_page"],
                "total_pages": total,
            }

        # FINAL SEGMENT — assemble parts and apply the standard verdict.
        md_dir = os.path.join(working_dir, "databank", "markdown")
        texts = []
        for p in sorted(
            progress["parts"], key=lambda q: int((q.get("range") or [0])[0])
        ):
            pp = os.path.join(working_dir, "databank", p.get("md_path") or "")
            if os.path.isfile(pp):
                texts.append(open(pp, encoding="utf-8", errors="replace").read())
        assembled = "\n\n---\n\n".join(texts)
        out_md = os.path.join(md_dir, f"{key}.md")
        with open(out_md, "w", encoding="utf-8") as f:
            f.write(assembled)
        agg = aggregate_book_parts(progress["parts"])
        profile = markdown_script_profile(assembled)
        figures = 0
        figdir = os.path.join(working_dir, "databank", "figures", key)
        if os.path.isdir(figdir):
            figures = sum(1 for f in os.listdir(figdir) if f.startswith("fig_"))

        # UNVERIFIED-BUT-CLEAN SCANS PASS (operator policy, 2026-08-22):
        # a scanned PDF has no embedded text to verify against, so its
        # rates are vacuous — but paddle's track record earns trust when
        # the DEGENERATION checks (the only ones that still measure
        # anything on a scan) are clean. The curator judges content;
        # `unverified_text_layer` rides in extraction_quality so the
        # provenance is never laundered. 144 terminal "failures" (140
        # strong-tagged) were sitting in this class when the policy
        # changed.
        unverified_clean = (
            agg["verified_pages"] <= 0
            and agg["pages"] > 0
            and agg["max_repeat_words"] <= MAX_REPEAT_WORDS
            and len(assembled) >= UNVERIFIED_MIN_MD_BYTES
        )
        table_dominant = (
            agg["verified_pages"] > 0
            and agg["numeric_match_rate"] >= MIN_NUMERIC_RATE
            and agg["span_pass_rate"] < MIN_SPAN_RATE
            and agg["max_repeat_words"] <= MAX_REPEAT_WORDS
            and _table_fraction(assembled) >= TABLE_DOMINANT_MIN_FRAC
        )
        ok = (
            unverified_clean
            or table_dominant
            or (
                agg["verified_pages"] > 0
                and agg["numeric_match_rate"] >= MIN_NUMERIC_RATE
                and agg["span_pass_rate"] >= MIN_SPAN_RATE
                and agg["max_repeat_words"] <= MAX_REPEAT_WORDS
            )
        )
        # Latin-script language vote: an otherwise-clean Spanish book is
        # translation work, not corpus text (see _LATIN_STOPWORDS).
        latin_lang, latin_conf = ("", 0.0)
        if ok and profile["nonlatin"] < LINGUAL_NONLATIN_MIN:
            latin_lang, latin_conf = latin_language_vote(assembled)
        latin_lingual = (
            latin_lang not in ("", "en") and latin_conf >= LINGUAL_LATIN_MIN_CONF
        )
        ok = ok and not latin_lingual
        lingual = latin_lingual or (
            not ok
            and agg["verified_pages"] > 0
            and agg["max_repeat_words"] <= MAX_REPEAT_WORDS
            and agg["numeric_match_rate"] >= MIN_NUMERIC_RATE
            and profile["nonlatin"] >= LINGUAL_NONLATIN_MIN
        )
        rec["md_path"] = os.path.join("databank", "markdown", f"{key}.md")
        rec["figure_count"] = figures
        rec["extraction_method"] = EXTRACTION_METHOD + "+book-segments"
        rec["extraction_quality"] = {
            **{
                k: agg[k]
                for k in (
                    "numeric_match_rate",
                    "span_pass_rate",
                    "max_repeat_words",
                    "verified_pages",
                    "unverified_pages",
                    "pages",
                )
            }
        }
        rec["script_profile"] = profile
        if ok:
            rec["extraction_status"] = "extracted"
            rec["failure_reason"] = ""
            if unverified_clean:
                rec["extraction_quality"]["unverified_text_layer"] = True
            if table_dominant:
                rec["extraction_quality"]["table_dominant"] = True
        elif agg["verified_pages"] <= 0:
            rec["extraction_status"] = "extract_unverified"
            rec["failure_reason"] = (
                "extraction (book): no verifiable text layer across "
                f"{agg['pages']} pages — rates vacuous, queued for triage"
            )
        elif lingual:
            rec["extraction_status"] = "extract_lingual"
            if latin_lingual and not rec.get("language"):
                rec["language"] = latin_lang
            rec["failure_reason"] = (
                "extraction (book): lingual — numerics verified "
                f"({agg['numeric_match_rate']:.2f}) on a "
                + (
                    f"Latin-script {latin_lang} volume ({latin_conf:.2f} vote); "
                    if latin_lingual
                    else f"{int(100 * profile['nonlatin'])}% non-Latin volume; "
                )
                + "queued for translation"
            )
        else:
            rec["extraction_status"] = "extract_failed"
            rec["failure_reason"] = (
                "extraction (book): below quality threshold "
                f"(numeric={agg['numeric_match_rate']:.2f}, "
                f"span={agg['span_pass_rate']:.2f})"
            )
        rec["book_progress"] = None
        await append_extraction_records(effects, [rec])
        for p in progress["parts"]:
            pp = os.path.join(working_dir, "databank", p.get("md_path") or "")
            if pp.endswith(".md") and ".part_" in pp and os.path.isfile(pp):
                os.unlink(pp)
        return {
            "book": key,
            "assembled": True,
            "status": rec["extraction_status"],
            "pages": agg["pages"],
            "figures": figures,
        }
    finally:
        release_ocr_keys([key])


async def _triage_claimed(step_input, effects, databank: dict, keys: list[str]) -> dict:
    """Triage claimed papers; return {'keep': [...], 'counts': {...}}.

    NEVER raises into the drain. Triage is an optimisation: if the vision
    model is unreachable, the prompt changes, or anything else goes wrong,
    every paper keeps its place and the round proceeds exactly as it did
    before this existed. A triage outage must not become an OCR outage.

    Books two fields to the sidecar for each paper it judges — `content_bin`
    and `content_priority` — plus a terminal `extract_off_topic` for papers
    whose first page is confidently not geological. That status is a REVIEW
    QUEUE, not a rejection: distinct from extract_failed, reason recorded,
    clearable by hand.
    """
    from agent.actions import preocr_triage as pt
    from agent.actions.scholarly_actions import append_extraction_records

    budget = pt.TRIAGE_BUDGET
    counts = {"triaged": 0, "off_topic": 0, "unknown": 0, "corrupt": 0}
    if budget <= 0 or not keys:
        return {"keep": keys, "counts": counts}

    keep: list[str] = []
    records: list[dict] = []
    for key in keys:
        rec = databank.get(key) or {}
        # Already judged in an earlier round — do not pay for it twice.
        if rec.get("content_priority") not in (None, ""):
            keep.append(key)
            continue
        if counts["triaged"] >= budget:
            keep.append(key)  # over budget this round; judge it next time
            continue
        pdf = rec.get("pdf_path")
        if not pdf:
            keep.append(key)
            continue
        try:
            v = await pt.triage_one(effects, key, pdf)
        except Exception:  # noqa: BLE001 — an optimisation must not break the lane
            logger.exception("pre-OCR triage errored on %s — keeping it", key)
            keep.append(key)
            continue
        counts["triaged"] += 1
        verdict = v.get("verdict")
        row = {
            "paper_key": key,
            "content_bin": v.get("bin") or "",
            "content_priority": int(v.get("priority", 1)),
        }
        if verdict == "off_topic":
            counts["off_topic"] += 1
            row["extraction_status"] = "extract_off_topic"
            row["failure_reason"] = v.get("reason", "")[:200]
            logger.info("🚦 triage: %s -> off_topic (%s)", key, v.get("reason", ""))
        elif verdict == "corrupt":
            counts["corrupt"] += 1
            row["extraction_status"] = "extract_failed"
            row["failure_reason"] = v.get("reason", "")[:200]
            logger.warning("🚦 triage: %s -> corrupt (%s)", key, v.get("reason", ""))
        else:
            if verdict == "unknown":
                counts["unknown"] += 1
            keep.append(key)
        records.append(row)

    # ALWAYS report, even when every paper was kept. A round that logs only
    # its removals is indistinguishable from a round that never ran — which
    # is precisely how this looked on its first live bounce: four papers had
    # been judged and booked, and the log said nothing at all.
    if counts["triaged"]:
        logger.info(
            "🚦 pre-OCR triage: %d judged — %d kept, %d off-topic, %d unreadable, "
            "%d corrupt",
            counts["triaged"],
            counts["triaged"] - counts["off_topic"] - counts["corrupt"],
            counts["off_topic"],
            counts["unknown"],
            counts["corrupt"],
        )
    if records:
        try:
            await append_extraction_records(effects, records)
        except Exception:  # noqa: BLE001
            logger.exception("pre-OCR triage could not book verdicts")
            # The verdicts are lost but the papers are not: anything not kept
            # was removed from THIS round only and is pending again next time.
    return {"keep": keep, "counts": counts}


async def action_ocr_drain_batch(step_input: StepInput) -> StepOutput:
    """Drain a bounded slice of the OCR backlog — the ocr_drain flow's one
    work step, built to run as a PARALLEL BRANCH beside discovery.

    Mission-clean by construction: selection reads the databank, extraction
    (action_extract_pdf_batch) appends extraction.jsonl — no mission writes,
    so it satisfies the branch ownership contract without exceptions.

    Inputs: working_directory; env OUROBOROS_OCR_DRAIN_PDFS bounds the
    slice (default 4 ≈ one discovery dispatch of paddle work at ~65 s/paper;
    0 disables). Result: attempted, extracted, reason.
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    raw = os.environ.get("OUROBOROS_OCR_DRAIN_PDFS", "").strip()
    try:
        max_pdfs = int(raw) if raw else 4
    except ValueError:
        max_pdfs = 4
    if max_pdfs <= 0:
        return StepOutput(
            result={"attempted": 0, "reason": "disabled"},
            observations="OCR drain disabled",
        )
    if effects is None:
        return StepOutput(
            result={"attempted": 0, "reason": "no effects"},
            observations="OCR drain: no effects",
        )

    databank = await read_databank(effects)
    keys = select_ocr_batch(databank, max_pdfs)
    if not keys:
        # Regular queue empty — spend the round on one BOOK SEGMENT instead
        # (the oversize interleave; see _book_segment_round).
        working_dir = str(
            step_input.inputs.get("working_directory")
            or getattr(
                getattr(step_input.context.get("mission"), "config", None),
                "working_directory",
                "",
            )
            or ""
        )
        if working_dir:
            book = await _book_segment_round(step_input, working_dir)
            logger.info("📚 book lane: %s", book)
            summary = {"attempted": 0, "book": book}
            note = (
                f"book segment: {book.get('book','')} "
                f"{book.get('segment') or ''} "
                f"{'ASSEMBLED ' + str(book.get('status')) if book.get('assembled') else ''}"
                if book.get("book")
                else f"idle ({book.get('reason')})"
            )
            return StepOutput(
                result=summary,
                observations=f"OCR drain: {note}",
                context_updates={"ocr_summary": summary},
            )
        summary = {"attempted": 0, "reason": "nothing unclaimed pending"}
        return StepOutput(
            result=summary,
            observations="OCR drain: nothing unclaimed pending",
            context_updates={"ocr_summary": summary},
        )
    claimed = list(keys)
    try:
        # PRE-OCR TRIAGE. Read each claimed paper's FIRST PAGE before spending
        # ~290s of OCR on it: paddle transcribes the page (~3s), muse reads
        # the TEXT (~13s) and returns a technique bin plus a priority.
        #
        # Inside the existing try/finally on purpose — the keys are already
        # claimed, so a triage that books-and-drops costs the lane nothing
        # extra and cannot leak a claim.
        triaged = await _triage_claimed(step_input, effects, databank, keys)
        keys = triaged["keep"]
        if not keys:
            summary = {"attempted": 0, "triage": triaged["counts"]}
            return StepOutput(
                result=summary,
                observations=(
                    "OCR drain: every claimed paper was triaged out "
                    f"({triaged['counts']})"
                ),
                context_updates={"ocr_summary": summary},
            )
        sub = step_input.model_copy(
            update={"inputs": {**dict(step_input.inputs or {}), "paper_keys": keys}}
        )
        out = await action_extract_pdf_batch(sub)
        result = dict(out.result or {})
    finally:
        release_ocr_keys(claimed)
    summary = {"attempted": len(keys), "triage": triaged["counts"], **result}
    obs = f"OCR drain: {len(keys)} pdf(s) — {out.observations}"
    # UNDER-FILLED ROUND RIDES A BOOK SEGMENT TOO. The strict
    # empty-queue-only gate starved the book lane: acquisition keeps the
    # regular queue at 1-2, so drain rounds always found SOMETHING and the
    # oversize pile never advanced (measured: 0 books started across a day
    # of queue≈0 samples). A partial batch has budget to spare; spend it.
    if len(keys) < max_pdfs:
        working_dir = str(
            step_input.inputs.get("working_directory")
            or getattr(
                getattr(step_input.context.get("mission"), "config", None),
                "working_directory",
                "",
            )
            or ""
        )
        if working_dir:
            # Shared round: paddle is mid-batch, so take a quarter slice.
            book = await _book_segment_round(
                step_input, working_dir, seg_override=max(10, _book_pages() // 4)
            )
            summary["book"] = book
            logger.info("📚 book lane: %s", book)
            if book.get("book"):
                obs += (
                    f"; book segment {book.get('book')} "
                    f"{book.get('segment') or ''}"
                    + (
                        f" ASSEMBLED {book.get('status')}"
                        if book.get("assembled")
                        else ""
                    )
                )
    return StepOutput(
        result=summary,
        observations=obs,
        context_updates={"ocr_summary": summary},
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

    async def _run_segment(pdf_path: str, key: str, a: int, b: int):
        """One page range through the toolchain. (report, failure detail)."""
        cmd = [
            os.path.join(root, _TOOL_PY),
            os.path.join(root, _TOOL_SCRIPT),
            "--pdfs",
            pdf_path,
            "--keys",
            key,
            "--databank-dir",
            os.path.join(working_dir, "databank"),
            "--vl-backend",
            _VL_BACKEND,
            "--page-range",
            f"{a}:{b}",
        ]
        res = await effects.run_command(cmd, timeout=_EXTRACT_ITEM_TIMEOUT_S)
        for line in (res.stdout or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("paper_key") == key:
                return r, ""
        detail = (
            "timed out"
            if res.timed_out
            else (
                f"exit {res.return_code}"
                if res.return_code
                else "exited cleanly with no output"
            )
        )
        tail = (res.stderr or "").strip().splitlines()[-2:]
        return None, detail + (f"; stderr: {' | '.join(tail)}" if tail else "")

    async def _extract_one(pdf_path: str, key: str, rec: dict):
        """Extract one paper IN PAGE SEGMENTS, banking after each.

        Returns (report | None, detail, paused) where `report` is an
        aggregate shaped like a whole-document report, so the verdict
        policy below judges a segmented paper exactly as it judges any
        other. `paused` means work stopped cleanly with progress banked —
        no verdict, no burned retry, resumes where it left off.

        WHY SEGMENTS AND NOT WHOLE DOCUMENTS. A paper was previously one
        subprocess: a kill at page 200 of a 237-page dissertation threw
        away all 200 pages, and the only way to stop the pipeline without
        losing work was to wait for the document to finish (observed:
        ~26 minutes of nothing but waiting for a safe restart window).
        Segments make the unit of loss a page range instead of a document,
        which is also what lets a pause land promptly: the check happens
        between segments, at a boundary where everything before it is
        already on disk.

        The machinery is the oversize book lane's, generalized — page
        ranges, part files, and a `next_page` cursor were already proven
        there on a 422-page volume.
        """
        progress = dict(rec.get("extract_progress") or {})
        parts = list(progress.get("parts") or [])
        start = int(progress.get("next_page") or 0)
        total = int(progress.get("total_pages") or 0)
        seg = _extract_pages()

        # Hard backstop on the loop. Every termination condition below
        # depends on the tool reporting a page count, and a loop whose exit
        # depends on a subprocess's output must not be able to run forever
        # if that output is ever shaped differently than expected.
        max_segments = 400
        while len(parts) < max_segments:
            if total and start >= total:
                break
            rep, detail = await _run_segment(pdf_path, key, start, start + seg)
            if rep is None:
                # No report at all — the round decides whether that is the
                # tool's fault or this paper's (see the caller).
                return None, detail, False
            if rep.get("error"):
                # AN IN-TOOL ERROR IS STILL A REPORT, and the verdict
                # policy below owns what it means — a 500 from the VLM is
                # a transient that must not condemn the paper, while an
                # unreadable PDF is terminal. Returning None here would
                # route both through the no-report path and lose that
                # distinction (two papers reached a terminal state on a
                # server 500 before the toolchain-fault branch existed).
                # Pages already banked stay banked; the retry resumes.
                return rep, detail, False

            seg_total = int(rep.get("total_pages") or 0)
            if not seg_total:
                # NO PAGE COUNT, NO SEGMENTATION. Without a total there is
                # no way to know whether more pages remain, and guessing
                # "yes" re-runs the same range forever. Treat what came
                # back as the whole document — which is exactly the
                # pre-segmentation behaviour for a report of this shape.
                parts.append(_part_from(rep, start, seg))
                break
            total = seg_total
            parts.append(_part_from(rep, start, seg))
            start = min(start + seg, total)

            if start >= total:
                break

            # BANK BEFORE CONTINUING. Everything up to here survives
            # whatever happens next, and the record stays PENDING so the
            # paper is still selectable — it simply resumes at next_page.
            rec["extract_progress"] = {
                "next_page": start,
                "total_pages": total,
                "parts": parts,
            }
            await append_extraction_records(effects, [rec])

            # A pause lands HERE: at a page boundary, with the work banked.
            if await _mission_paused(effects):
                return None, f"paused at page {start}/{total}", True
            if time.monotonic() > deadline:
                return None, f"deadline at page {start}/{total}", True

        # COMPLETE — assemble and synthesize a whole-document report so the
        # verdict policy below needs no knowledge of segmentation.
        assembled = _assemble_segments(working_dir, key, parts)
        agg = aggregate_book_parts(parts)
        # Figures come from what the segments REPORTED keeping. Figure
        # numbering is append-aware across ranges, so the sum is the
        # document's count; the directory is only a fallback for a report
        # that predates the field.
        figures = sum(int(p.get("figures_kept") or 0) for p in parts)
        if not figures:
            figdir = os.path.join(working_dir, "databank", "figures", key)
            if os.path.isdir(figdir):
                figures = sum(1 for f in os.listdir(figdir) if f.startswith("fig_"))
        rec["extract_progress"] = {}
        # Script profile from the ASSEMBLED document when there is one —
        # it sees the whole paper rather than one range. When assembly
        # produced nothing (a part file missing under us), fall back to
        # what the last segment reported rather than to a census of the
        # empty string, which would read as 100% Latin and mis-route a
        # non-Latin paper away from the translation lane.
        profile = (
            markdown_script_profile(assembled)
            if assembled.strip()
            else (rep.get("script_profile") or {})
        )
        return (
            {
                "paper_key": key,
                "md_path": os.path.join("markdown", f"{key}.md"),
                "figures_kept": figures,
                "script_profile": profile,
                "oversize": False,
                "error": "",
                **agg,
            },
            "",
            False,
        )

    extracted = retried = failed = lingual_count = 0
    unjudged: list[str] = []
    paused_keys: list[str] = []
    unjudged_detail = ""
    deadline = time.monotonic() + EXTRACT_TIMEOUT_S
    for k, pdf_path in zip(resolved_keys, pdfs):
        # BATCH DEADLINE, separate from the per-item timeout. EXTRACT_TIMEOUT_S
        # was a whole-batch budget; reused per item it would silently become
        # N times itself. Papers past the deadline stay pending and unclaimed,
        # which is the same state they were in before the round.
        if time.monotonic() > deadline:
            unjudged.append(k)
            unjudged_detail = unjudged_detail or "batch deadline reached"
            continue

        rec = dict(databank.get(k) or {"paper_key": k})
        rep, detail, paused = await _extract_one(pdf_path, k, rec)

        # A CLEAN STOP IS NOT A FAILURE. Progress is banked, the record is
        # still pending, and the paper resumes at its next_page. Booking
        # anything here — even "unjudged" — would misreport an orderly
        # pause as something that went wrong.
        if paused:
            paused_keys.append(k)
            break

        # NO REPORT INDICTS THE TOOLCHAIN, NOT THE PAPER.
        # extract_batch prints one JSON line per paper even when that paper
        # fails — an unreadable PDF still gets a report carrying `error`. So
        # NO line means the process died before it could judge anything: a
        # bad interpreter, a missing model, an import error, a path with no
        # file behind it. None of that is evidence about the paper, and
        # booking it destroys the paper's eligibility for the run that
        # finally works.
        #
        # This is the containment for the /Users-vs-/home port bug: 99 papers
        # were marked extract_failed by a toolchain that never opened one of
        # them. Two dispatches burn the retry rung and reach a TERMINAL
        # state, so the loss was silent and permanent. Leaving the record
        # untouched means the sweep re-dispatches — noisy, and noise is the
        # correct failure mode when the tool itself is broken.
        #
        # Per paper this is strictly better than the batch form it replaces:
        # one bad paper no longer leaves its siblings unjudged.
        if rep is None:
            unjudged.append(k)
            unjudged_detail = unjudged_detail or detail
            continue

        # NOTE: `rec` is the same dict _extract_one just worked on — it
        # carries the cleared extract_progress. Re-reading it from the
        # databank here would resurrect the finished paper's segment
        # cursor and make it look perpetually half-done.
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
            and (
                (
                    rep.get("verified_pages", 0) > 0
                    and rep.get("numeric_match_rate", 0) >= MIN_NUMERIC_RATE
                    and rep.get("span_pass_rate", 0) >= MIN_SPAN_RATE
                )
                # UNVERIFIED-BUT-CLEAN SCAN (operator policy, 2026-08-22):
                # no text layer means the rates are vacuous, not failed —
                # paddle's verdict stands when the degeneration guard
                # below is the only check that still measures anything.
                # Flag stamped at booking; the curator judges content.
                # TABLE-DOMINANT: span is a prose metric and this page
                # is not prose. The numeric bar still binds.
                or (
                    rep.get("verified_pages", 0) > 0
                    and rep.get("numeric_match_rate", 0) >= MIN_NUMERIC_RATE
                    and rep.get("span_pass_rate", 0) < MIN_SPAN_RATE
                    and (_batch_md_text(working_dir, rep) or "") != ""
                    and _table_fraction(_batch_md_text(working_dir, rep) or "")
                    >= TABLE_DOMINANT_MIN_FRAC
                )
                or (
                    rep.get("verified_pages", 0) <= 0
                    # the segment aggregator zeroes `pages` on merged
                    # reports; unverified_pages survives aggregation
                    and (rep.get("pages", 0) > 0 or rep.get("unverified_pages", 0) > 0)
                    and _md_size(working_dir, rep.get("md_path"))
                    >= UNVERIFIED_MIN_MD_BYTES
                )
            )
            # A looped decode keeps every number and so passes both rates.
            # An older report has no such field; absent reads as 0 = clean,
            # which is the right default for a metric that did not exist.
            and rep.get("max_repeat_words", 0) <= MAX_REPEAT_WORDS
        )
        # Latin-script language vote (see _LATIN_STOPWORDS): the metrics
        # above cannot fail a clean Spanish paper, and the script gate
        # below cannot see it. Read the head of the markdown the tool
        # just wrote — a 60KB slice is plenty for a stopword vote.
        latin_lang, latin_conf = ("", 0.0)
        if (
            ok
            and script_nonlatin_frac(rep.get("script_profile")) < LINGUAL_NONLATIN_MIN
        ):
            try:
                with open(
                    os.path.join(working_dir, "databank", rep["md_path"]),
                    encoding="utf-8",
                    errors="replace",
                ) as fh:
                    latin_lang, latin_conf = latin_language_vote(fh.read(60_000))
            except OSError:
                pass
        latin_lingual = (
            latin_lang not in ("", "en") and latin_conf >= LINGUAL_LATIN_MIN_CONF
        )
        ok = ok and not latin_lingual
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
            if rep.get("verified_pages", 0) <= 0:
                rec["extraction_quality"]["unverified_text_layer"] = True
            elif rep.get("span_pass_rate", 0) < MIN_SPAN_RATE:
                rec["extraction_quality"]["table_dominant"] = True
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
            # Lingual routing precondition: everything else about the
            # extraction is CLEAN (verified pages, no loop, numerics anchor
            # holds) and only the English-prose span metric failed on a
            # substantially non-Latin document.
            lingual = latin_lingual or (
                bool(rep)
                and not rep.get("error")
                and not oversize
                and not truncated
                and rep.get("verified_pages", 0) > 0
                and rep.get("max_repeat_words", 0) <= MAX_REPEAT_WORDS
                and rep.get("numeric_match_rate", 0) >= MIN_NUMERIC_RATE
                and rep.get("span_pass_rate", 0) < MIN_SPAN_RATE
                and script_nonlatin_frac(rep.get("script_profile"))
                >= LINGUAL_NONLATIN_MIN
            )
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
            elif lingual:
                if latin_lingual and not rec.get("language"):
                    rec["language"] = latin_lang
                reason = (
                    "lingual — numerics verified "
                    f"({rep.get('numeric_match_rate', 0):.2f}) on a "
                    + (
                        f"Latin-script {latin_lang} document "
                        f"({latin_conf:.2f} vote); "
                        if latin_lingual
                        else (
                            "document whose span metric is English-prose on a "
                            f"{int(100 * script_nonlatin_frac(rep.get('script_profile')))}% "
                            "non-Latin volume; "
                        )
                    )
                    + "queued for translation"
                )
            elif rep:
                reason = (
                    "below quality threshold "
                    f"(numeric={rep.get('numeric_match_rate', 0):.2f}, "
                    f"span={rep.get('span_pass_rate', 0):.2f})"
                )
            else:
                # `detail` is _extract_one's explanation for the missing
                # report ("timed out", "exit N", "exited cleanly with no
                # output"). The previous line referenced `result.timed_out`
                # — a name that does not exist in this scope, so every trip
                # through this branch raised NameError instead of booking
                # the failure reason (found by ruff F821, 2026-08-26).
                reason = "no report from toolchain" + (f" ({detail})" if detail else "")
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
            toolchain_fault = bool(rep) and is_toolchain_fault(rep.get("error", ""))
            if toolchain_fault:
                # FIRST IN THE CHAIN, and that placement is the point. Every
                # branch below classifies the DOCUMENT — too big, a fragment,
                # no text layer, poor rates — and not one of those readings
                # means anything when the machinery failed partway through.
                #
                # Concretely: a 500 on page 1 leaves verified_pages at 0, which
                # the `unverifiable` branch would read as "a scan with no text
                # layer" and file as terminal. Same lost paper as before,
                # reached by a different route. Only ordering this first closes
                # both.
                #
                # A server fault never consumes a retry rung and never reaches
                # a terminal state: the record is cleared back to pending and
                # the sweep picks it up again.
                #
                # This is the per-paper twin of the zero-report guard above.
                # That one catches a batch whose process died; this catches a
                # batch that survived and reported a transport failure for one
                # paper — the tool did its job and told us the VLM refused,
                # which says nothing about the PDF.
                #
                # Live: two papers reached terminal extract_failed on
                # `Error code: 500 — the model produced output that does not
                # match the expected peg-native format`. One was re-run
                # standalone, unchanged flags, and scored 0.983 numeric /
                # 0.888 span with 10/10 pages verified. The fault is a
                # sampling artifact, so a retry genuinely recovers and
                # condemning the paper simply loses it.
                #
                # An unbounded retry is the hazard this trades for. It is the
                # right trade while these faults stay rare (2 in 219
                # extractions): a paper that fails this way forever costs one
                # dispatch per sweep and stays visible in the pending count,
                # whereas a terminal state loses a good paper silently.
                rec["failure_reason"] = (
                    f"extraction (toolchain fault, will retry): {reason}"
                )
                rec["extraction_status"] = ""
                retried += 1
            elif oversize:
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
            elif lingual:
                # No retry rung burned: another OCR pass reads the same
                # script and scores the same. The markdown (already recorded
                # above, with figures) is the translation drain's input.
                rec["extraction_status"] = "extract_lingual"
                if latin_lingual and not rec.get("language"):
                    rec["language"] = latin_lang
                rec["failure_reason"] = f"extraction: {reason}"
                rec["script_profile"] = rep.get("script_profile") or {}
                lingual_count += 1
            elif prior_retry:
                rec["extraction_status"] = "extract_failed"
                rec["failure_reason"] = f"extraction: {reason}"
                failed += 1
            else:
                rec["extraction_status"] = "needs_reextract"
                rec["failure_reason"] = f"extraction (will retry): {reason}"
                retried += 1

        # BOOK NOW, not at the end of the batch. This one append is the
        # whole point of the per-item restructure: after it returns, this
        # paper's work survives anything that happens to the rest of the
        # round. SIDECAR, not papers.jsonl — the scraper owns that file and
        # both writers do a whole-file read-modify-write, so sharing it
        # loses appends. Disjoint files let acquisition and OCR run at once.
        await append_extraction_records(effects, [rec])

    judged = len(resolved_keys) - len(unjudged)

    # WHO IS AT FAULT FOR A MISSING REPORT — the round decides, not the paper.
    #
    # Under the old batch form, siblings were the discriminator: the tool
    # prints one line per paper, so a paper with no line among siblings that
    # DID report had itself broken the tool, and booking it needs_reextract
    # was evidence-based. Per-item runs have no siblings, so the same
    # question is answered one level up:
    #
    #   some papers judged  -> the toolchain works; this paper broke it.
    #                          Run it through the normal retry ladder, which
    #                          terminates instead of re-offering it forever.
    #   none judged         -> the toolchain is broken. Leave every record
    #                          untouched (the /Users-vs-/home containment).
    #
    # Both incident lessons survive, and neither can loop: a paper that
    # reliably crashes the tool still reaches a terminal state.
    if judged and unjudged:
        for k in unjudged:
            rec = dict(databank.get(k) or {"paper_key": k})
            reason = f"no report from tool ({unjudged_detail})"
            if rec.get("extraction_status") == "needs_reextract":
                rec["extraction_status"] = "extract_failed"
                rec["failure_reason"] = f"extraction: {reason}"
                failed += 1
            else:
                rec["extraction_status"] = "needs_reextract"
                rec["failure_reason"] = f"extraction (will retry): {reason}"
                retried += 1
            await append_extraction_records(effects, [rec])
        unjudged = []

    # NOTHING JUDGED AT ALL INDICTS THE TOOLCHAIN. If EVERY paper came back
    # without a report, the tool itself is broken, and the summary has to say
    # so by name or the next reader repeats the investigation that cost 99
    # papers to the /Users-vs-/home port bug.
    if resolved_keys and not judged:
        summary = (
            f"Extraction toolchain produced no report for any of "
            f"{len(resolved_keys)} paper(s) ({unjudged_detail}) — the batch "
            f"is unjudged and stays pending"
        )
        return StepOutput(
            result={"status": "failed"},
            observations=summary,
            context_updates={
                "directive_report": {
                    "flow": "extract_pdfs",
                    "status": "failed",
                    "summary": summary,
                    "headline": "extraction toolchain failed — no reports",
                }
            },
        )

    status = "success" if extracted == judged else "partial"
    if extracted == 0:
        status = "failed"
    summary = (
        f"Extracted {extracted}/{len(resolved_keys)} paper(s)"
        + (f", {retried} queued for retry" if retried else "")
        + (f", {lingual_count} queued for translation" if lingual_count else "")
        + (f", {failed} failed terminally" if failed else "")
        + (
            f", {len(unjudged)} unjudged and still pending ({unjudged_detail})"
            if unjudged
            else ""
        )
        + (
            f", {len(paused_keys)} paused mid-document with progress banked"
            if paused_keys
            else ""
        )
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
