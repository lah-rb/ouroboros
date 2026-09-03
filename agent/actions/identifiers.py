"""Alternative identifiers for papers that carry no DOI or arXiv id.

WHY. 332 accepted-or-pending papers have no DOI (measured 2026-09-03): theses
and institutional reports harvested from CORE, national thesis registries and
university repositories, two thirds of everything waiting on curation. The
pack envelope required `doi|arxiv_id`, so every one of them packed cleanly and
was then refused -- the identifier rule, not packing, became the pipeline's
dominant loss.

THE POLICY (operator, 2026-09-03): "attempt more manual action to resolve what
we can, look for alternative ids on older papers, and emit the rest as
identifier: none."

So identity is resolved in tiers, cheapest first, and the envelope accepts any
tier -- including the honest absence:

  doi / arxiv_id      the existing fields, unchanged
  openalex            a work id already on the record (112 of the 332), or one
                      found by a title+year lookup
  handle / urn / hdl  a persistent identifier embedded in the source URL
                      (repository handles, national thesis numbers, URNs)
  core                the CORE aggregator's own stable download id -- weakest,
                      because it identifies a COPY rather than a work, so it
                      is only ever the last resort before "none"
  none                nothing resolved; recorded explicitly so a consumer can
                      tell "unidentified" from "not looked at"

Every tier is written to `identifier` / `identifier_kind` on the record, so a
paper is resolved once and never re-looked-up.
"""

from __future__ import annotations

import re
from typing import Any

# Ordered best-first; the envelope reports the first that is present.
IDENTIFIER_FIELDS: tuple[tuple[str, str], ...] = (
    ("doi", "doi"),
    ("arxiv_id", "arxiv"),
    ("openalex_id", "openalex"),
    ("identifier", ""),  # kind comes from identifier_kind
)

# Persistent identifiers embedded in repository URLs, best first. Each pattern
# yields the identifier text; the key is the kind recorded on the record.
_URL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # hdl.handle.net/X, /handle/X, and DSpace's /bitstream/X/<seq>/<file>
        # form, which is how most repository PDFs are actually linked.
        "hdl",
        re.compile(
            r"(?:hdl\.handle\.net/|/handle/|/bitstream/)((?:\d+|[\w.]+)/[\w.\-]+)", re.I
        ),
    ),
    ("urn", re.compile(r"\b(urn:[a-z0-9][\w:.\-]+)", re.I)),
    ("theses.fr", re.compile(r"theses\.fr/(\d{4}[A-Z0-9]+)", re.I)),
    ("nbn", re.compile(r"\b(nbn:[\w:.\-]+)", re.I)),
    ("core", re.compile(r"core\.ac\.uk/download/(?:pdf/)?(\d+)", re.I)),
)


# A bare handle: "10174/24376", "20.500.11754/70607". Distinguished from a DOI
# by the absence of the "10.NNNN/" registrant form a DOI always carries.
_BARE_HANDLE = re.compile(r"^(?:\d{2,6}|20\.\d{3,5}(?:\.\d+)?)/[\w.\-]+$")


def classify_identifier(value: str) -> tuple[str, str]:
    """(identifier, kind) for a value of unknown provenance.

    The scraper's own `identifier` field carries a mix: real repository
    handles, and bare URLs that identify nothing ("https://espace.inrs.ca/").
    A URL is re-parsed for an embedded persistent id and otherwise DISCARDED
    -- a landing page is a location, not an identity, and recording one as an
    identifier would make an unidentified paper look identified.
    """
    v = str(value or "").strip()
    if not v:
        return "", ""
    if v.lower().startswith(("http://", "https://")):
        return identifier_from_url(v)
    if v.lower().startswith("10.") and "/" in v:
        return v, "doi"
    if _BARE_HANDLE.match(v):
        return v, "hdl"
    if v.lower().startswith("urn:"):
        return v, "urn"
    if re.fullmatch(r"W\d+", v):
        return v, "openalex"
    return v, "other"


def identifier_from_url(url: str) -> tuple[str, str]:
    """(identifier, kind) parsed from a source URL, or ("", "").

    A Wayback wrapper is unwrapped first: the archive URL identifies a
    snapshot, the URL inside it identifies the document.
    """
    if not url:
        return "", ""
    inner = re.sub(r"^https?://web\.archive\.org/web/\d+\w*/", "", url.strip())
    for kind, pat in _URL_PATTERNS:
        m = pat.search(inner)
        if m:
            return m.group(1), kind
    return "", ""


def record_identifier(record: dict) -> tuple[str, str]:
    """(identifier, kind) for a record, best tier first; ("", "none") if none.

    Reads only what is already on the record -- no network. `identifier` /
    `identifier_kind`, once written by the resolver, are preferred over a
    fresh URL parse so a resolution is stable.
    """
    for field, kind in IDENTIFIER_FIELDS:
        val = str(record.get(field) or "").strip()
        if not val:
            continue
        if field == "identifier":
            known = str(record.get("identifier_kind") or "").strip()
            if known:
                return val, known  # a resolution this pipeline wrote: trust it
            ident, guessed = classify_identifier(val)  # the scraper's field
            if ident:
                return ident, guessed
            continue  # a bare URL identifies nothing; keep looking
        return val, kind
    ident, kind = identifier_from_url(str(record.get("oa_pdf_url") or ""))
    if ident:
        return ident, kind
    for url in record.get("oa_pdf_urls") or []:
        ident, kind = identifier_from_url(str(url or ""))
        if ident:
            return ident, kind
    return "", "none"


def openalex_id_short(value: str) -> str:
    """W-number from an OpenAlex id or URL ("https://openalex.org/W123" -> W123)."""
    v = str(value or "").strip()
    m = re.search(r"\b(W\d+)\b", v)
    return m.group(1) if m else v


_TITLE_STOPWORDS = frozenset("the a an of and for at from with by in on to".split())


def _title_words(text) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower())) - _TITLE_STOPWORDS


def title_match_score(a: str, b: str) -> float:
    """Word-overlap of two titles, 0..1 -- deliberately crude and
    order-insensitive, because extracted titles carry OCR damage and
    subtitle punctuation that a strict ratio penalises."""
    wa, wb = _title_words(a), _title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def is_confident_match(work: dict, record: dict, min_score: float = 0.72) -> bool:
    """Does an OpenAlex work plausibly BE this paper?

    Title overlap must clear ``min_score``, and if both sides state a year
    they must agree within one (repositories date the deposit, publishers the
    issue). A wrong identity is worse than none, so this errs strict.
    """
    if (
        title_match_score(
            work.get("title") or work.get("display_name"), record.get("title")
        )
        < min_score
    ):
        return False
    wy, ry = work.get("publication_year"), record.get("year")
    try:
        if wy and ry and abs(int(wy) - int(ry)) > 1:
            return False
    except (TypeError, ValueError):
        pass
    return True


def envelope_identity(record: dict) -> dict[str, Any]:
    """The identity block for a pack envelope: always present, always honest."""
    ident, kind = record_identifier(record)
    return {
        "identifier": ident,
        "identifier_kind": kind if ident else "none",
    }
