#!/usr/bin/env python3
"""Operator-browser intake: ranked Scholar fetch lists out, verified documents in.

WHY. The acquisition lanes cannot reach the `oa_unresolved` papers: about half
sit behind publisher bot walls (HTTP 403, or an HTML challenge page) and half
behind dead links. A person in a browser gets through where a crawler must not
(ingest_reading_list.py: this project does not defeat bot management). The
2026-09-24 pilot (scholar_title_pilot.md) recovered 7 of 10 randomly drawn
unresolved papers at ~30 s each, by searching Google Scholar for the exact
title and taking the PDF from its sidebar. This tool makes that a lane. It
never fetches anything: the operator browses, the tool ranks the work and
verifies what comes back.

  export  rank the pool and write a fetch list of --n papers to lists/.
          Ask for more than you mean to do; it's a soft stop (ask 80 for a
          goal of 40).
  ingest  match every PDF / HTML file in the bundle to its record by its FIRST
          PAGE, then book the matches. --through N records items 1..N of the
          list that got no file as misses; misses are not listed again.

RANKING (operator ruling 2026-09-24): on-topic first; English first within a
tier; bot-walled before dead links within that. "On-topic" is the curator's
accept rate predicted from what the catalog knows before the text: a logistic
fit, redone on every export over every reviewed paper, of accept on
- the number of "exact" catalog tags,
- the best tag label,
- domain terms in the title and abstract,
- citations from accepted papers.

Measured 2026-09-24: 0/1/2/3+ exact tags → 46/71/90/93 % accepted; 0 → 4+
domain terms → 35 → 80 %. Tiers: high ≥ 0.75, good ≥ 0.60, fair ≥ 0.45, low.
Each item leads with its Scholar exact-title link, because the operator found
Scholar's PDF sidebar faster than publisher landing pages. The DOI link comes
second.

VERIFICATION. A file is matched to a record when its first two pages carry
either the record's whole title, or its DOI plus half its title. The title
check ignores whitespace and punctuation, so line-broken and CJK titles
still match. The longest matching title wins, so "Part VI" never takes Part
VII's PDF. A record outside the list needs its DOI, or a long title (40+
compacted characters), to be matched.

A record that already holds a PDF is re-checked the same way:
- If its PDF matches the record, the new file is a duplicate and is skipped.
- If it doesn't, the record holds a WRONG DOCUMENT and is replaced, but only
  with --replace-mismatched. The old PDF and every derived artifact move to
  <corpus>/manual_intake/quarantine/<key>/<stamp>/, extraction and review are
  reset, and OCR and curation run again.

The motivating case, 2026-09-24: doi_10.1002_jrs.5214 held an 11-page
battery paper. The article runs 11 pages, so the page-extent guard passed it,
and the curator correctly denied the battery text.

WRITES (only with --apply). Every row goes through the agent's own appenders
and is the key's last row plus changes (last-row-replaces). The safe side is
written first, the activating side last.

Papers side:
- access_status oa_pdf + pdf_path. A full-text HTML page instead gets
  oa_html; it is converted here with pandoc (the Word-supplement route) and
  booked to the extraction sidecar as extracted.
- retrieval_method operator_browser, and a manual_intake provenance dict.
- license restricted-asn for academia.edu / ResearchGate copies of works that
  carry no open licence (the licence-gate convention).
- status stays "cataloged" when the tag lane already stamped the record.

Files are COPIED; the bundle is left as it was. The ledger
(<corpus>/manual_intake/ledger.jsonl) keeps every export, hit and miss.

  .venv/bin/python dev/manual_intake/intake.py export --n 80
  .venv/bin/python dev/manual_intake/intake.py ingest --list dev/manual_intake/lists/<list>.md --through 60
  .venv/bin/python dev/manual_intake/intake.py ingest --list ... --through 60 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import datetime as dt
import glob
import hashlib
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.acquisition_routes import (  # noqa: E402
    acquirer,
    browser_exhausted,
    read_browser_log,
)

CORPUS = Path(os.path.expanduser("~/corpora/ouroboros-spectra"))
DATABANK = CORPUS / "databank"
INTAKE = CORPUS / "manual_intake"
LEDGER = INTAKE / "ledger.jsonl"
LISTS = Path(__file__).resolve().parent / "lists"
BUNDLE = Path(os.path.expanduser("~/Downloads/paper_bundle"))

TIERS = ((0.75, "high"), (0.60, "good"), (0.45, "fair"), (0.0, "low"))
DOMAIN_TERMS = re.compile(
    r"raman|laser[- ]induced breakdown|\blibs\b|spectroscop|spectrometr|spectra\b|spectrum|"
    r"x-ray diffraction|\bxrd\b|x-ray fluorescence|\bxrf\b|\bftir\b|infrared|reflectance|mineral|"
    r"crystal|pigment|\bores?\b|\brocks?\b|geolog|meteorit|\bmars\b|lunar|planetary|ceramic|glass|"
    r"luminescen|emission line",
    re.IGNORECASE,
)
EN_WORDS = frozenset(
    "the of and in for on with by from to an using study analysis its at as into via between based "
    "new case during under".split()
)
OTHER_WORDS = frozenset(
    "de la le les des du et en el los las del y una der die das und von im mit zur zum für auf dem den "
    "ein eine di il della per do da dos em na no com uma sur par au aux".split()
)
STOP = EN_WORDS | OTHER_WORDS
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
#: an item line; tolerant of the operator marking items up (**25. [..**, ~~25. [..~~)
ITEM_RE = re.compile(r"^\s*[*_~]*\s*(\d+)\.\s+[*_~]*\[")
KEY_RE = re.compile(r"`([^`]+)`\s*$")
ASN_HOSTS = ("academia.edu", "researchgate")
OPEN_LICENCE_PREFIXES = ("cc-", "cc0", "public-domain")
#: papers-side fields derived from the held document; cleared when it is replaced
DOC_DERIVED_FIELDS = (
    "review_status", "review_doc_form", "review_summary", "review_issues", "deny_category",
    "tag_review_agreement", "curation_method", "review_document_form", "pack_doc_form", "pack_status",
    "dataset_path", "pack_quality", "figtext_status", "figtext_path", "figtext_progress",
    "curate_min_seat_tokens",
)  # fmt: skip


# ── databank reads ─────────────────────────────────────────────────────


def last_rows(path: Path) -> dict[str, dict]:
    """paper_key -> the key's LAST row (last-row-replaces)."""
    out: dict[str, dict] = {}
    with open(path, "rb") as fh:
        for line in fh:
            line = line.strip(b"\x00\r\n ")
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("paper_key"):
                out[rec["paper_key"]] = rec
    return out


def tail_ok(path: Path) -> bool:
    """The file ends in a newline and its last line parses (power-cut check)."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 262144))
        chunk = fh.read()
    if size and not chunk.endswith(b"\n"):
        return False
    lines = [ln for ln in chunk.split(b"\n") if ln.strip(b"\x00\r ")]
    if not lines:
        return True
    try:
        json.loads(lines[-1])
        return True
    except json.JSONDecodeError:
        return False


def read_ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in open(LEDGER, encoding="utf-8"):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def append_ledger(events: list[dict]) -> None:
    INTAKE.mkdir(parents=True, exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


# ── text helpers ───────────────────────────────────────────────────────


def clean_title(t: str) -> str:
    t = html.unescape(re.sub(r"<[^>]+>", "", t or ""))
    return re.sub(r"\s+", " ", t).strip().rstrip(".")


def compact(s: str) -> str:
    """Case-, whitespace-, punctuation- and accent-blind form (NFKC folds
    ligatures; accents fold because title pages print capitals without them:
    "COPOLYMERES ANTIBACTERIENS" for a record's "copolymères antibactériens",
    2026-10-03)."""
    s = unicodedata.normalize("NFKC", s or "").lower().replace("-\n", "")
    s = "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )
    return re.sub(r"[\W_]+", "", s)


def tokens(s: str) -> set[str]:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return {w for w in re.findall(r"\w+", s) if len(w) >= 3 and w not in STOP}


def dois_in(text: str) -> set[str]:
    return {d.lower().rstrip(".,;:)]}") for d in DOI_RE.findall(text or "")}


def latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if ord(c) < 0x250) / len(letters)


def is_english(rec: dict) -> bool:
    lang = str(rec.get("language") or "").lower()
    if lang:
        return lang.startswith("en")
    text = f"{rec.get('title') or ''} {(rec.get('abstract') or '')[:400]}"
    if latin_ratio(text) < 0.5:
        return False
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    en = sum(w in EN_WORDS for w in words)
    other = sum(w in OTHER_WORDS for w in words)
    return en > other or (en == other == 0 and text.isascii())


def failure_class(rec: dict) -> str:
    f = str(rec.get("failure_reason") or "")
    if "403" in f or "text/html" in f:
        return "bot wall"
    if re.search(r"HTTP (404|400|410)", f):
        return "dead link"
    return "other"


def scholar_url(title: str) -> str:
    return "https://scholar.google.com/scholar?hl=en&q=" + urllib.parse.quote_plus(
        f'"{title}"'
    )


# ── ranking ────────────────────────────────────────────────────────────


def cited_counts(papers: dict[str, dict]) -> collections.Counter:
    """DOI -> how many accepted papers cite it."""
    c: collections.Counter = collections.Counter()
    for r in papers.values():
        if r.get("review_status") == "accepted":
            for d in {str(x).lower() for x in (r.get("reference_dois") or [])}:
                c[d] += 1
    return c


def features(rec: dict, cites: collections.Counter) -> list[float]:
    tags = [t for t in (rec.get("tags") or []) if isinstance(t, dict)]
    labels = {t.get("relevance") for t in tags}
    exact = min(3, sum(1 for t in tags if t.get("relevance") == "exact"))
    text = f"{rec.get('title') or ''} {rec.get('abstract') or ''}"
    terms = len({m.group(0).lower() for m in DOMAIN_TERMS.finditer(text)})
    try:
        stored = int(rec.get("cited_by_accepted") or 0)
    except (TypeError, ValueError):
        stored = 0
    cited = max(
        stored, cites.get(str(rec.get("doi") or "").lower(), 0) if rec.get("doi") else 0
    )
    return [
        1.0,
        float(exact),
        float(exact == 0 and "close" in labels),
        float(exact == 0 and "close" not in labels and "adjacent" in labels),
        min(4, terms) / 4.0,
        math.log1p(cited),
        float(bool(str(rec.get("abstract") or "").strip())),
    ]


def fit_logistic(X, y, ridge: float = 1e-3, iters: int = 50):
    """Ridge logistic regression by Newton steps (numpy only)."""
    import numpy as np

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-X @ w))
        H = X.T @ (X * (p * (1 - p))[:, None]) + ridge * np.eye(X.shape[1])
        step = np.linalg.solve(H, X.T @ (y - p) - ridge * w)
        w += step
        if np.abs(step).max() < 1e-9:
            break
    return w


def auc(scores, y) -> float:
    pos = [s for s, t in zip(scores, y) if t]
    neg = [s for s, t in zip(scores, y) if not t]
    if not pos or not neg:
        return float("nan")
    order = sorted((s, i) for i, s in enumerate(list(pos) + list(neg)))
    ranks = [0.0] * len(order)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and order[j + 1][0] == order[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k][1]] = (i + j) / 2 + 1
        i = j + 1
    rp = sum(ranks[: len(pos)])
    return (rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg))


def tier(p: float) -> str:
    return next(name for cut, name in TIERS if p >= cut)


def rank_pool(papers: dict[str, dict], pool: list[dict]) -> tuple[list[dict], dict]:
    """Score and order the pool; returns (ordered items, fit report)."""
    import numpy as np

    cites = cited_counts(papers)
    reviewed = [
        r for r in papers.values() if r.get("review_status") in ("accepted", "denied")
    ]
    X = [features(r, cites) for r in reviewed]
    y = [r["review_status"] == "accepted" for r in reviewed]
    w = fit_logistic(X, y)
    fit_scores = 1 / (1 + np.exp(-np.asarray(X) @ w))
    report = {
        "reviewed": len(reviewed),
        "accept_rate": round(sum(y) / max(1, len(y)), 3),
        "auc": round(auc(fit_scores.tolist(), y), 3),
        "weights": [round(float(v), 3) for v in w],
    }
    items = []
    for r in pool:
        p = float(1 / (1 + np.exp(-np.asarray(features(r, cites)) @ w)))
        items.append(
            {
                "rec": r,
                "p": p,
                "tier": tier(p),
                "english": is_english(r),
                "klass": failure_class(r),
            }
        )
    order = {name: i for i, (_, name) in enumerate(TIERS)}
    items.sort(
        key=lambda it: (
            order[it["tier"]],
            not it["english"],
            it["klass"] != "bot wall",
            -it["p"],
            it["rec"]["paper_key"],
        )
    )
    return items, report


# ── export ─────────────────────────────────────────────────────────────


def cmd_export(args) -> int:
    papers = last_rows(DATABANK / "papers.jsonl")
    statuses = {
        "unresolved": ("oa_unresolved",),
        "closed": ("closed",),
        "both": ("oa_unresolved", "closed"),
    }[args.pool]
    missed = {e["key"] for e in read_ledger() if e.get("event") == "miss"}
    # WHO FETCHES IT comes from the one routes table (agent/acquisition_routes):
    # an operator list never carries a paper the browser fetcher still owns,
    # and a browser list carries only those (operator ruling 2026-10-02).
    exhausted = browser_exhausted(read_browser_log(CORPUS))
    pool = [
        r for r in papers.values()
        if r.get("access_status") in statuses and clean_title(r.get("title") or "")
        and r.get("record_kind") != "supplement" and (args.retry_missed or r["paper_key"] not in missed)
        and acquirer(r, exhausted) == args.route
    ]  # fmt: skip
    routed_away = sum(
        1 for r in papers.values()
        if r.get("access_status") in statuses and r.get("record_kind") != "supplement"
        and acquirer(r, exhausted) != args.route
    )  # fmt: skip
    items, fit = rank_pool(papers, pool)
    chosen = items[: args.n]
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    LISTS.mkdir(parents=True, exist_ok=True)
    out = LISTS / f"fetch_{stamp}_n{len(chosen)}.md"
    dist = collections.Counter(it["tier"] for it in items)
    L = [
        f"# Fetch list {stamp}: {len(chosen)} papers",
        "",
        f"Route: {args.route} ({routed_away:,} `{'/'.join(statuses)}` papers belong to another route). "
        f"Pool: {len(pool):,} `{'/'.join(statuses)}` papers (tiers: "
        + ", ".join(f"{name} {dist.get(name, 0):,}" for _, name in TIERS)
        + f"; {len(missed):,} earlier misses left out). Order: on-topic tier, then English, then bot-walled "
        f"before dead links. p = predicted curator accept rate (fit on {fit['reviewed']:,} reviewed papers, "
        f"AUC {fit['auc']}).",
        "",
        f"Save each hit to `{BUNDLE}` under any name. When you stop, note the number of the last item you tried "
        "and ingest with `--through` that number:",
        "",
        f"    .venv/bin/python dev/manual_intake/intake.py ingest --list {out.relative_to(REPO)} --through N",
        "",
    ]
    for i, it in enumerate(chosen, 1):
        r = it["rec"]
        t = clean_title(r["title"])
        meta = (
            " · ".join(
                x
                for x in (
                    str(r.get("venue") or "").strip(),
                    str(r.get("year") or "").strip(),
                )
                if x
            )
            or "venue unknown"
        )
        doi = f"[doi](https://doi.org/{r['doi']})" if r.get("doi") else "no DOI"
        lang = "" if it["english"] else " · non-English"
        L += [
            f"{i}. [{t}]({scholar_url(t)})",
            f"   {meta} · {doi} · p {it['p']:.2f} ({it['tier']}){lang} · {it['klass']} · `{r['paper_key']}`",
            "",
        ]
    out.write_text("\n".join(L), encoding="utf-8")
    append_ledger(
        [
            {
                "event": "export",
                "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "list": str(out.relative_to(REPO)),
                "keys": [it["rec"]["paper_key"] for it in chosen],
                "pool": len(pool),
                "fit": fit,
            }
        ]
    )
    shown = collections.Counter(it["tier"] for it in chosen)
    print(f"-> {out}")
    print(
        f"pool {len(pool):,}; fit on {fit['reviewed']:,} reviewed (accept {fit['accept_rate']}), AUC {fit['auc']}"
    )
    print(
        "list tiers:",
        dict(shown),
        "| English",
        sum(it["english"] for it in chosen),
        "| bot wall",
        sum(it["klass"] == "bot wall" for it in chosen),
    )
    return 0


# ── ingest: reading and matching ───────────────────────────────────────


def parse_list(path: Path) -> list[tuple[int, str]]:
    """[(position, paper_key)] from a fetch list (or the pilot list)."""
    items, pos = [], None
    for line in open(path, encoding="utf-8"):
        m = ITEM_RE.match(line)
        if m:
            pos = int(m.group(1))
            continue
        if pos is not None:
            k = KEY_RE.search(line.rstrip())
            if k:
                items.append((pos, k.group(1)))
                pos = None
    return items


def read_pdf(path: Path, pages: int = 2) -> tuple[str, int, str]:
    """(first `pages` pages + metadata, page count, page 1 + metadata)."""
    import pymupdf

    with pymupdf.open(path) as doc:
        texts = [doc[i].get_text() for i in range(min(pages, doc.page_count))]
        meta = " ".join(str(v) for v in (doc.metadata or {}).values() if v)
        return (
            "\n".join(texts) + "\n" + meta,
            doc.page_count,
            (texts[0] if texts else "") + "\n" + meta,
        )


def body_match(
    path: Path, papers: dict, listed: dict[str, int], max_pages: int = 60
) -> tuple[str | None, str]:
    """Fallback when the first two pages name no record: a LISTED title, verbatim and at least
    30 compacted characters, anywhere in the document (proceedings excerpts and theses put
    cover matter first). Exactly one such title, or no match."""
    import pymupdf

    with pymupdf.open(path) as doc:
        pages = [
            compact(doc[i].get_text()) for i in range(min(max_pages, doc.page_count))
        ]
    found = []
    for key in listed:
        ct = compact(clean_title(papers.get(key, {}).get("title") or ""))
        if len(ct) >= 30:
            hit = next((i + 1 for i, pg in enumerate(pages) if ct in pg), None)
            if hit:
                found.append((key, hit))
    if not found:
        # LINE-NUMBERED MANUSCRIPTS (2026-10-02): an author's accepted
        # manuscript numbers every line, and the numbers land inside the
        # title ("Post-Landing Major Element Quantification Using 1 SuperCam
        # Laser Induced Breakdown Spectro..."), so the verbatim test never
        # sees it. Second pass with digits dropped from both sides, under the
        # same exactly-one-listed-title rule.
        bare = [re.sub(r"\d+", "", pg) for pg in pages]
        for key in listed:
            ct = re.sub(
                r"\d+", "", compact(clean_title(papers.get(key, {}).get("title") or ""))
            )
            if len(ct) >= 30:
                hit = next((i + 1 for i, pg in enumerate(bare) if ct in pg), None)
                if hit:
                    found.append((key, hit))
        if len(found) == 1:
            return found[0][0], f"title on page {found[0][1]} (line numbers ignored)"
    if len(found) == 1:
        return found[0][0], f"title on page {found[0][1]}"
    return None, (
        ("several listed titles in the body: " + ", ".join(k for k, _ in found))
        if found
        else "no listed title anywhere in the document"
    )


def read_html(path: Path) -> tuple[str, dict]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    meta: dict[str, str] = {}
    for a, b in (("name", "content"), ("content", "name")):
        for m in re.finditer(
            rf'<meta\s+{a}="([^"]*)"\s+{b}="([^"]*)"', raw, re.IGNORECASE
        ):
            name, value = (
                (m.group(1), m.group(2)) if a == "name" else (m.group(2), m.group(1))
            )
            if name.lower().startswith(("citation_", "dc.")):
                meta.setdefault(name.lower(), html.unescape(value))
    body = re.sub(
        r"<script.*?</script>|<style.*?</style>", " ", raw, flags=re.S | re.IGNORECASE
    )
    body = html.unescape(re.sub(r"<[^>]+>", " ", body))
    return body + "\n" + " ".join(meta.values()), meta


class TitleIndex:
    """Every databank record by DOI and by compacted title."""

    def __init__(self, papers: dict[str, dict]):
        self.by_doi: dict[str, list[str]] = collections.defaultdict(list)
        self.titles: list[tuple[str, str, set[str]]] = []
        for k, r in papers.items():
            d = str(r.get("doi") or "").lower().strip()
            if d:
                self.by_doi[d].append(k)
            t = clean_title(r.get("title") or "")
            if len(compact(t)) >= 12:
                self.titles.append((k, compact(t), tokens(t)))

    def candidates(self, text: str) -> list[dict]:
        ctext, ttoks = compact(text), tokens(text)
        doi_keys = {k for d in dois_in(text) for k in self.by_doi.get(d, ())}
        out = []
        for k, ct, toks in self.titles:
            doi_hit = k in doi_keys
            contained = ct in ctext
            if contained:
                score = 1.0
            elif len(toks) >= 4 or doi_hit:
                # Word coverage is weaker evidence than the title verbatim: a paper on the same
                # topic can carry every word of a short generic title. Capped below 1.0 so it
                # never passes for a verbatim match (2026-09-25: "Pigments and Mixtures
                # Identification by Visible Reflectance Spectroscopy" matched a Rembrandt paper).
                score = min(0.99, len(toks & ttoks) / max(1, len(toks)))
            else:
                continue
            if (
                (contained and len(ct) >= 20)
                or (score >= 0.85 and len(toks) >= 4)
                or (doi_hit and score >= 0.5)
            ):
                out.append(
                    {
                        "key": k,
                        "score": round(score, 3),
                        "doi_hit": doi_hit,
                        "tlen": len(ct),
                        "contained": contained,
                    }
                )
        out.sort(key=lambda c: (c["doi_hit"], c["score"], c["tlen"]), reverse=True)
        return out


_HAL_ID = re.compile(r"HAL Id:\s*([a-z]+-\d{6,})", re.I)


def hal_id_match(text: str, papers: dict, listed: dict[str, int]) -> str | None:
    """The listed record whose stored HAL link carries the document's HAL Id.

    HAL prepends a cover sheet ("HAL Id: tel-05326197 ... Submitted on ...")
    whose title can differ from the record's (a thesis's French title, an
    English translation in the record); the id is exact (2026-10-03: 6 of 87
    browser-fetched HAL theses)."""
    m = _HAL_ID.search(text or "")
    if not m:
        return None
    hal = m.group(1).lower()
    pat = re.compile(r"/" + re.escape(hal) + r"(?:v\d+)?(?:[/?#]|$)", re.I)
    hits = [
        k
        for k in listed
        if any(
            pat.search(str(u))
            for u in [
                papers.get(k, {}).get("oa_pdf_url") or "",
                *(papers.get(k, {}).get("oa_pdf_urls") or []),
                *(papers.get(k, {}).get("oa_attempted") or []),
            ]
        )
    ]
    return hits[0] if len(hits) == 1 else None


def filename_match(path: Path, papers: dict, listed: dict[str, int]) -> str | None:
    """A file SAVED UNDER a listed title (exact compacted match, >= 30 chars,
    exactly one). Last resort for a manuscript whose own title differs from the
    record's -- 2026-10-02: the accepted manuscript of "Optimisation of fast
    quantification of fluorine content using handheld laser induced breakdown
    spectroscopy" titles itself "... using handheld LIBS"."""
    stem = compact(path.stem)
    if len(stem) < 30:
        return None
    hits = [
        k
        for k in listed
        if compact(clean_title(papers.get(k, {}).get("title") or "")) == stem
    ]
    return hits[0] if len(hits) == 1 else None


def decide(cands: list[dict], listed: set[str]) -> tuple[str | None, str]:
    """(key, reason). The best candidate wins unless the runner-up ties it."""
    if not cands:
        return None, "no record's title or DOI is on the first two pages"
    best = cands[0]
    if len(cands) > 1 and (
        cands[1]["doi_hit"],
        cands[1]["score"],
        cands[1]["tlen"],
    ) == (best["doi_hit"], best["score"], best["tlen"]):
        return None, f"ambiguous: {best['key']} vs {cands[1]['key']}"
    if (
        best["key"] not in listed
        and not best["doi_hit"]
        and not (best.get("contained") and best["tlen"] >= 40)
    ):
        return (
            None,
            f"weak match to an unlisted record ({best['key']}, title score {best['score']})",
        )
    return best["key"], (
        "doi+title"
        if best["doi_hit"]
        else ("title" if best.get("contained") else "title-words")
    )


def holds_itself(rec: dict, text: str) -> bool:
    """Does this text carry this record's title (or DOI + half the title)?"""
    t = clean_title(rec.get("title") or "")
    ct, toks = compact(t), tokens(t)
    if ct and ct in compact(text):
        return True
    cov = len(toks & tokens(text)) / max(1, len(toks))
    doi = str(rec.get("doi") or "").lower()
    return (cov >= 0.85 and len(toks) >= 4) or (
        bool(doi) and doi in dois_in(text) and cov >= 0.5
    )


def infer_host(path: Path, text: str, rec: dict) -> str:
    """Where the operator's copy came from, as far as the file tells."""
    low, name = text.lower(), path.name
    if "researchgate.net/publication" in low:
        return "researchgate"
    if "academia.edu" in low:
        return "academia.edu"
    stem = path.stem
    if re.fullmatch(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)+", stem) and len(stem) <= 44:
        folded = (
            unicodedata.normalize("NFKD", rec.get("title") or "")
            .encode("ascii", "ignore")
            .decode()
        )
        if compact(folded).startswith(compact(stem)):
            return "academia.edu"  # its downloads are named Title_words_cut_at_40.pdf
    if "hal id" in low or "hal open science" in low or "to cite this version" in low:
        return "repository:hal"
    if re.search(r"arxiv:\d{4}\.\d{4,5}", low):
        return "repository:arxiv"
    if re.match(r"1-s2\.0-.*-main\.pdf$", name):
        return "publisher:elsevier"
    if re.search(r" - (19|20)\d\d - ", name):
        return "publisher:wiley"
    if re.match(r"[a-z]+-\d+-\d{3,6}(-v\d+)?\.pdf$", name):
        return "publisher:mdpi"
    if re.match(r"s\d{5}-\d{3}-\d{5}-\w\.pdf$", name):
        return "publisher:springer-nature"
    if name.startswith("bitstream"):
        return "repository"
    if path.suffix.lower() in (".html", ".htm"):
        return "html page"
    return "unknown"


def derived_paths(key: str, rec: dict) -> list[Path]:
    """Every stored artifact derived from the document a record holds."""
    md = DATABANK / "markdown"
    paths = [
        CORPUS / "pdfs" / f"{key}.pdf", md / f"{key}.md", md / f"{key}.en.md", DATABANK / "figures" / key,
        DATABANK / "figtext" / f"{key}.json", DATABANK / "figdata" / f"{key}.json", DATABANK / "_preocr" / f"{key}.png",
        DATABANK / "dataset" / f"{key}.json", DATABANK / "translations" / f"{key}.parts.jsonl",
        DATABANK / "translations" / f"{key}..parts.jsonl",
    ]  # fmt: skip
    paths += [
        Path(p) for p in sorted(glob.glob(str(md / f"{glob.escape(key)}.part_*.md")))
    ]
    for field in ("pdf_path", "md_path", "md_en_path", "figtext_path", "dataset_path"):
        v = rec.get(field)
        if v:
            paths.append(CORPUS / v)
    seen, out = set(), []
    for p in paths:
        if p.exists() and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── HTML → markdown (pandoc) ───────────────────────────────────────────


def _stringify(node) -> str:
    if isinstance(node, dict):
        t = node.get("t")
        if t == "Str":
            return node["c"]
        if t in ("Space", "SoftBreak", "LineBreak"):
            return " "
        if t in ("Code", "Math", "RawInline"):
            return str(node["c"][-1])
        return _stringify(node.get("c"))
    if isinstance(node, list):
        return "".join(_stringify(x) for x in node)
    return ""


def _strip_embedded(node):
    """Drop inline data: images (badges, icons) and the links left empty without them."""
    if isinstance(node, list):
        return [y for y in (_strip_embedded(x) for x in node) if y is not None]
    if not isinstance(node, dict):
        return node
    t = node.get("t")
    if t == "Image" and str(node["c"][2][0]).startswith("data:"):
        return None
    if "c" in node:
        node = {**node, "c": _strip_embedded(node["c"])}
    if (
        t == "Link"
        and not _stringify(node["c"][1]).strip()
        and not any(isinstance(i, dict) and i.get("t") == "Image" for i in node["c"][1])
    ):
        return None
    return node


def html_fragment(ast: dict, title: str, min_chars: int = 1500) -> list:
    """The block list of the content container: the innermost Div around the
    title heading that holds at least min_chars of text (navigation, menus and
    footers sit outside it)."""
    target = compact(title)

    def find(blocks, path, kinds):
        for i, b in enumerate(blocks):
            if not isinstance(b, dict):
                continue
            if b.get("t") == "Div":
                hit = find(b["c"][1], path + [(blocks, i)], kinds)
                if hit:
                    return hit
            elif b.get("t") in kinds and target and target in compact(_stringify(b)):
                return path + [(blocks, i)]
        return None

    path = find(ast["blocks"], [], ("Header",)) or find(
        ast["blocks"], [], ("Para", "Plain")
    )
    if not path:
        return []
    for blocks, i in reversed(path[:-1]):
        if len(_stringify(blocks[i])) >= min_chars:
            return blocks[i]["c"][1]
    blocks, i = path[-1]
    return blocks[i:]


def convert_html(src: Path, key: str, record_title: str) -> dict:
    """pandoc the page's content container into the databank as the extractor would."""
    from tools.supplement_records import rasterise, rewrite_images

    rep = {"ok": False, "chars": 0, "figures": 0, "skipped": 0}
    title = clean_title(read_html(src)[1].get("citation_title", "")) or clean_title(
        record_title
    )
    proc = subprocess.run(
        ["pandoc", "-f", "html", "-t", "json", str(src)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        rep["error"] = proc.stderr[:300]
        return rep
    ast = json.loads(proc.stdout)
    blocks = html_fragment(ast, title)
    if not blocks:
        rep["error"] = "title not found in the page"
        return rep
    doc = {
        "pandoc-api-version": ast["pandoc-api-version"],
        "meta": {},
        "blocks": _strip_embedded(blocks),
    }
    proc = subprocess.run(
        ["pandoc", "-f", "json", "-t", "gfm-raw_html", "--wrap=none"],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        rep["error"] = proc.stderr[:300]
        return rep
    md, sources = rewrite_images(proc.stdout, key)
    fig_dir = DATABANK / "figures" / key
    kept = 0
    for i, s in enumerate(sources, 1):
        local = src.parent / urllib.parse.unquote(s)
        if (
            not s.startswith(("http:", "https:", "data:"))
            and local.is_file()
            and rasterise(local, fig_dir / f"fig_{i:02d}.png")
        ):
            kept += 1
        else:
            rep["skipped"] += 1
            md = md.replace(
                f'<img src="../figures/{key}/fig_{i:02d}.png">',
                f"<!-- dropped figure: {Path(s).name} -->",
            )
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    (DATABANK / "markdown").mkdir(parents=True, exist_ok=True)
    (DATABANK / "markdown" / f"{key}.md").write_text(md, encoding="utf-8")
    rep.update(
        ok=True, chars=len(md), figures=kept, latin_ratio=round(latin_ratio(md), 3)
    )
    return rep


# ── ingest ─────────────────────────────────────────────────────────────


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bundle_files(paths: list[Path]) -> list[Path]:
    out = []
    for p in paths:
        if p.is_dir():
            out += sorted(
                x
                for x in p.iterdir()
                if x.is_file() and x.suffix.lower() in (".pdf", ".html", ".htm")
            )
        elif p.is_file():
            out.append(p)
    return out


def plan_ingest(
    files: list[Path],
    papers: dict,
    listed: dict[str, int],
    replace: bool,
    assign: dict[str, str] | None = None,
) -> list[dict]:
    """``assign`` maps a file NAME to the record the operator says it is:
    the match for documents no text check can verify (2026-10-02: a 1997
    article scanned to JPEG pages with no text layer)."""
    index = TitleIndex(papers)
    done_hashes = {
        h.get("sha256")
        for e in read_ledger()
        if e.get("event") == "ingest"
        for h in e.get("hits", [])
    }
    plans, taken = [], {}
    for f in files:
        kind = "html" if f.suffix.lower() in (".html", ".htm") else "pdf"
        p = {"file": f, "kind": kind, "mtime": f.stat().st_mtime}
        try:
            if kind == "pdf":
                with open(f, "rb") as fh:
                    if fh.read(5) != b"%PDF-":
                        plans.append({**p, "action": "SKIP", "why": "not a PDF"})
                        continue
                text, pages, text1 = read_pdf(f)
                p["pages"] = pages
            else:
                text, _ = read_html(f)
                text1 = text
        except (
            Exception
        ) as exc:  # noqa: BLE001 — one unreadable file must not sink the bundle
            plans.append({**p, "action": "SKIP", "why": f"unreadable: {exc}"[:120]})
            continue
        p["sha256"] = sha256(f)
        # A file's identity is its title page: page 1 first; pages 1-2 only for cover pages,
        # and then not when the document carries a DOI that is not the matched record's
        # (a title on page 2 of a paper with its own DOI is a citation).
        key, why = decide(index.candidates(text1), set(listed))
        if not key:
            cands = index.candidates(text)
            key, why = decide(cands, set(listed))
            own = dois_in(text)
            if (
                key
                and not cands[0]["doi_hit"]
                and own
                and str(papers[key].get("doi") or "").lower() not in own
            ):
                key, why = (
                    None,
                    f"its first pages carry a different DOI ({sorted(own)[0]}); {cands[0]['key']}'s title is on page 2 (a citation?)",
                )
        if not key and kind == "pdf" and listed:
            key2, why2 = body_match(f, papers, listed)
            own = dois_in(text)
            rec_doi = str(papers.get(key2 or "", {}).get("doi") or "").lower()
            if key2 and own and rec_doi not in own:
                # a document with its own DOI that names a listed title only in its body is
                # citing it (2026-09-25: a Rembrandt paper whose p. 9 cites a listed paper)
                why = f"its first pages carry a different DOI ({sorted(own)[0]}); {key2}'s title appears only past the title page (a citation?)"
            elif key2:
                key, why = key2, why2
        if not key and listed:
            key_hal = hal_id_match(text1, papers, listed)
            if key_hal:
                key, why = key_hal, "HAL Id on the cover sheet"
        if not key and listed:
            key3 = filename_match(f, papers, listed)
            own = dois_in(text)
            rec_doi = str(papers.get(key3 or "", {}).get("doi") or "").lower()
            if key3 and not (own and rec_doi not in own):
                key, why = key3, "saved under the listed title (file name)"
        if assign and f.name in assign:
            key, why = assign[f.name], "assigned by the operator"
        if not key:
            plans.append(
                {
                    **p,
                    "action": "UNMATCHED",
                    "why": why,
                    "snippet": re.sub(r"\s+", " ", text)[:90],
                }
            )
            continue
        if key in taken:
            plans.append(
                {
                    **p,
                    "action": "SKIP",
                    "key": key,
                    "why": f"second file for {key} (kept {taken[key].name})",
                }
            )
            continue
        rec = papers[key]
        p.update(key=key, match=why, pos=listed.get(key), host=infer_host(f, text, rec))
        if p["sha256"] in done_hashes:
            plans.append({**p, "action": "SKIP", "why": "already ingested (same file)"})
            continue
        held = CORPUS / rec["pdf_path"] if rec.get("pdf_path") else None
        if rec.get("access_status") == "oa_html" or (held and held.is_file()):
            held_ok = rec.get("access_status") == "oa_html"
            if held and held.is_file():
                try:
                    held_ok = holds_itself(rec, read_pdf(held)[0])
                except (
                    Exception
                ):  # noqa: BLE001 — an unreadable held PDF counts as wrong
                    held_ok = False
            if held_ok:
                plans.append(
                    {**p, "action": "SKIP", "why": "record already holds this paper"}
                )
                continue
            if not replace:
                plans.append(
                    {
                        **p,
                        "action": "MISMATCH",
                        "why": "record holds a DIFFERENT document; --replace-mismatched swaps it",
                    }
                )
                continue
            p["action"] = "REPLACE"
        else:
            p["action"] = "NEW"
        if kind == "html":
            p["action"] += "-HTML"
        if p.get("pages") and rec.get("first_page") and rec.get("last_page"):
            try:
                extent = int(rec["last_page"]) - int(rec["first_page"]) + 1
                if (
                    extent > 1 and abs(extent - p["pages"]) > 2
                ):  # extent 1 = an article number, not pages
                    p["note"] = f"PDF has {p['pages']} pages; the article runs {extent}"
            except (TypeError, ValueError):
                pass
        taken[key] = f
        plans.append(p)
    return plans


def paper_row(rec: dict, p: dict, batch: str, now: str) -> dict:
    row = dict(rec)
    for f in (DOC_DERIVED_FIELDS if p["action"].startswith("REPLACE") else ()):
        row.pop(f, None)
    key = rec["paper_key"]
    if p["kind"] == "pdf":
        row["access_status"] = "oa_pdf"
        row["pdf_path"] = f"pdfs/{key}.pdf"
    else:
        row["access_status"] = "oa_html"
        row["pdf_path"] = ""
    if row.get("status") not in ("cataloged", "needs_retag"):
        row["status"] = "acquired"
    row["failure_reason"] = ""
    row["retrieval_method"] = "operator_browser"
    if p["host"] in ASN_HOSTS and not str(row.get("license") or "").lower().startswith(
        OPEN_LICENCE_PREFIXES
    ):
        row["license"] = "restricted-asn"
    row["manual_intake"] = {
        "batch": batch, "position": p.get("pos"), "file": p["file"].name, "host": p["host"], "sha256": p["sha256"],
        "match": p["match"], "ingested_at": now, "replaced_wrong_document": p["action"].startswith("REPLACE"),
    }  # fmt: skip
    return row


def extraction_reset_row(key: str) -> dict:
    return {
        "paper_key": key, "extraction_status": "", "failure_reason": "", "md_path": "", "md_en_path": "",
        "figure_count": 0, "extraction_method": "", "extraction_quality": {}, "extract_progress": {},
        "translated": False, "translate_attempts": 0, "translate_epoch": 0,
    }  # fmt: skip


def extraction_html_row(key: str, rep: dict) -> dict:
    return {
        "paper_key": key,
        "extraction_status": (
            "extracted" if rep.get("latin_ratio", 1.0) >= 0.5 else "extract_lingual"
        ),
        "failure_reason": "",
        "md_path": f"databank/markdown/{key}.md",
        "figure_count": int(rep.get("figures") or 0),
        "extraction_method": "pandoc-html",
        "extraction_quality": {
            "oracle": "none",
            "native_text": True,
            "chars": int(rep.get("chars") or 0),
            "figures_dropped": int(rep.get("skipped") or 0),
            "latin_ratio": rep.get("latin_ratio", 1.0),
        },
    }


def cmd_ingest(args) -> int:
    papers = last_rows(DATABANK / "papers.jsonl")
    ext = last_rows(DATABANK / "extraction.jsonl")
    listed = {k: pos for pos, k in parse_list(Path(args.list))} if args.list else {}
    batch = str(Path(args.list).name) if args.list else "unlisted"
    files = bundle_files(
        [Path(os.path.expanduser(b)) for b in (args.bundle or [str(BUNDLE)])]
    )
    assign = {}
    for spec in args.assign or []:
        name, _, item = spec.partition("=")
        item = item.strip().lstrip("#")
        key = next((k for k, pos in listed.items() if str(pos) == item), item)
        if key not in papers:
            print(f"--assign {spec!r}: no record {key!r}")
            return 2
        assign[name.strip()] = key
    plans = plan_ingest(files, papers, listed, args.replace_mismatched, assign)
    book = [p for p in plans if p["action"].startswith(("NEW", "REPLACE"))]
    hits = {
        p["key"]: p
        for p in plans
        if p.get("key") and p.get("pos") and p["action"] != "UNMATCHED"
    }
    for p in sorted(plans, key=lambda p: (p.get("pos") or 999, p["file"].name)):
        pos = f"#{p['pos']:<3}" if p.get("pos") else "    "
        who = p.get("key", "")[:52]
        extra = p.get("why") or f"{p.get('match')} · {p.get('host')}" + (
            f" · NOTE {p['note']}" if p.get("note") else ""
        )
        print(f"  {p['action']:<12} {pos} {who:<52} {p['file'].name[:40]:<40} {extra}")
        if p["action"] == "UNMATCHED":
            print(f"               first page: {p['snippet']}")
    through = args.through or max(
        (pos for k, pos in listed.items() if k in hits), default=0
    )
    misses = [
        (pos, k)
        for k, pos in sorted(listed.items(), key=lambda x: x[1])
        if pos <= through and k not in hits
    ]
    n_hit = sum(1 for k, pos in listed.items() if pos <= through and k in hits)
    if listed:
        print(
            f"\nlist {batch}: through #{through}: {n_hit} hit(s), {len(misses)} miss(es)"
            + (f" → hit rate {n_hit / through:.0%}" if through else "")
        )
        times = sorted(p["mtime"] for p in hits.values())
        if len(times) >= 2:
            span = times[-1] - times[0]
            first = min(p["pos"] for p in hits.values())
            last = max(p["pos"] for p in hits.values())
            print(
                f"files saved over {span / 60:.1f} min between items #{first} and #{last} ≈ {span / max(1, last - first):.0f} s per item"
            )
    print(
        f"{len(book)} to book ({sum(p['action'].startswith('REPLACE') for p in book)} replacement(s)); "
        f"{sum(p['action'] == 'MISMATCH' for p in plans)} wrong-document holder(s) left alone; {sum(p['action'] == 'UNMATCHED' for p in plans)} unmatched file(s)"
    )
    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0
    return apply_ingest(book, papers, ext, batch, misses, n_hit, through, hits)


def apply_ingest(book, papers, ext, batch, misses, n_hit, through, hits) -> int:
    from agent.actions.scholarly_actions import (
        append_extraction_records,
        append_records,
    )
    from agent.effects.local import LocalEffects

    for f in ("papers.jsonl", "extraction.jsonl"):
        if not tail_ok(DATABANK / f):
            print(f"REFUSED: databank/{f} has a torn tail — repair it before any write")
            return 3
    fx = LocalEffects(str(CORPUS))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    received = INTAKE / "received"
    received.mkdir(parents=True, exist_ok=True)
    safe_ext, safe_papers, act_ext, act_papers = [], [], [], []
    for p in book:
        key, rec = p["key"], papers[p["key"]]
        if p["action"].startswith("REPLACE") or key in ext:
            moved = derived_paths(key, {**rec, **ext.get(key, {})})
            if moved:
                q = INTAKE / "quarantine" / key / stamp
                q.mkdir(parents=True, exist_ok=True)
                for (
                    src
                ) in (
                    moved
                ):  # keep the corpus-relative path: figtext/ and figdata/ share file names
                    dst = q / src.relative_to(CORPUS)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                (q / "why.json").write_text(
                    json.dumps(
                        {
                            "key": key,
                            "replaced_by": p["file"].name,
                            "at": now,
                            "old_review": {
                                f: rec.get(f)
                                for f in (
                                    "review_status",
                                    "deny_category",
                                    "review_summary",
                                )
                            },
                        },
                        ensure_ascii=False,
                        indent=1,
                    ),
                    encoding="utf-8",
                )
            safe_ext.append(
                extraction_reset_row(key)
            )  # SAFE: nothing downstream selects an empty extraction
        shutil.copy2(p["file"], received / f"{key}{p['file'].suffix.lower()}")
        if p["kind"] == "pdf":
            shutil.copy2(p["file"], CORPUS / "pdfs" / f"{key}.pdf")
            act_papers.append(paper_row(rec, p, batch, now))  # ACTIVATES the OCR drain
        else:
            companion = p["file"].parent / f"{p['file'].stem}_files"
            if companion.is_dir():
                shutil.copytree(
                    companion, received / f"{key}_files", dirs_exist_ok=True
                )
            rep = convert_html(p["file"], key, rec.get("title") or "")
            if not rep["ok"]:
                print(
                    f"  HTML conversion failed for {key}: {rep.get('error')} — not booked"
                )
                continue
            print(
                f"  HTML {key}: {rep['chars']:,} chars, {rep['figures']} figure(s), {rep['skipped']} dropped"
            )
            safe_papers.append(
                paper_row(rec, p, batch, now)
            )  # oa_html: no acquisition or OCR lane selects it
            act_ext.append(
                extraction_html_row(key, rep)
            )  # ACTIVATES figtext + curation
    if safe_ext:
        asyncio.run(append_extraction_records(fx, safe_ext))
    if safe_papers:
        asyncio.run(append_records(fx, safe_papers))
    if act_papers:
        asyncio.run(append_records(fx, act_papers))
    if act_ext:
        asyncio.run(append_extraction_records(fx, act_ext))
    append_ledger([{
        "event": "ingest", "at": now, "list": batch, "through": through,
        "hits": [{"key": p["key"], "pos": p.get("pos"), "host": p["host"], "file": p["file"].name, "sha256": p["sha256"], "action": p["action"]} for p in book],
        "listed_hits": n_hit, "misses": len(misses),
    }] + [{"event": "miss", "at": now, "list": batch, "pos": pos, "key": k} for pos, k in misses])  # fmt: skip
    print(f"booked {len(act_papers) + len(safe_papers)} record(s); ledger {LEDGER}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="write a ranked fetch list")
    e.add_argument(
        "--n",
        type=int,
        default=80,
        help="list length (ask for more than you plan to do)",
    )
    e.add_argument(
        "--pool", choices=("unresolved", "closed", "both"), default="unresolved"
    )
    e.add_argument(
        "--retry-missed", action="store_true", help="list earlier misses again"
    )
    e.add_argument(
        "--route",
        choices=("operator", "browser"),
        default="operator",
        help="whose list: the operator's manual fetches, or tools/browser_fetch.py's "
        "(agent/acquisition_routes.py decides which papers are whose)",
    )
    i = sub.add_parser("ingest", help="match downloaded files to records and book them")
    i.add_argument("--list", help="the fetch list the files answer (positions, misses)")
    i.add_argument(
        "--bundle",
        action="append",
        help=f"a directory or file (repeatable; default {BUNDLE})",
    )
    i.add_argument(
        "--through",
        type=int,
        default=0,
        help="last list item tried (default: the last item with a file)",
    )
    i.add_argument(
        "--replace-mismatched",
        action="store_true",
        help="swap a record's wrong-document PDF",
    )
    i.add_argument(
        "--assign",
        action="append",
        metavar="FILE=ITEM",
        help="book FILE (a bundle file name) as list item ITEM (a position or a paper key) "
        "when no text check can match it, e.g. a scan with no text layer",
    )
    i.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    return cmd_export(args) if args.cmd == "export" else cmd_ingest(args)


if __name__ == "__main__":
    raise SystemExit(main())
