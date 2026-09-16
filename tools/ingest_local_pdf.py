#!/usr/bin/env python3
"""Ingest ONE local PDF the operator hands over, booked as acquisition would book it.

WHY. Some of the most valuable documents never reach the discovery lanes: agency
reports (ERDC/CRREL, USGS), theses, society proceedings — no abstract in the
catalogues, no OA resolver route, sometimes a DOI minted by the agency that no
index carries. The operator finds them by hand. This books such a file exactly
in the state action_download_papers leaves behind (status acquired,
access_status oa_pdf, pdf_path pdfs/<key>.pdf), so the OCR drain selects it on
its next round and it walks the ordinary path: extraction, figtext, curate
review, pack. Nothing is preapproved — the curator still judges it — unless
--binder puts it on the foundations shelf (see dev/ingest_reading_list.py,
whose record shape this reuses).

The row is written through the agent's own appender (O_APPEND + lock + field
filter), merged over the LAST existing row for the key when one exists — never
a partial row.

    .venv/bin/python tools/ingest_local_pdf.py --pdf ~/Downloads/report.pdf \\
        --title "…" --doi 10.21079/11681/38061 --authors "R. S. Harmon; R. R. Hark" \\
        --year 2020 --venue "ERDC/CRREL CR-20-1" --aspect "multi technique paired spectra" \\
        --license public-domain --url https://doi.org/10.21079/11681/38061 \\
        --source "operator hand-off 2026-09-16"            # dry run; add --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import paper_key  # noqa: E402

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")


def build_record(
    base: dict | None,
    *,
    key: str,
    title: str,
    doi: str,
    arxiv: str,
    authors: list[str],
    year: int | None,
    venue: str,
    aspects: list[str],
    license_: str,
    url: str,
    source: str,
    binder: bool,
    now: str,
) -> dict:
    """The papers.jsonl row: the reading-list ingest's shape, merged over base."""
    rec = (
        dict(base)
        if base
        else {
            "status": "candidate",
            "access_status": "",
            "source_aspects": list(aspects),
            "oa_pdf_url": "",
            "oa_pdf_urls": [],
            "oa_attempted": [],
            "pdf_path": "",
            "tags": [],
            "reference_dois": [],
            "referenced_works": [],
            "language": "",
            "license": "",
            "failure_reason": "",
            "title": title,
            "abstract": "",
            "year": year,
            "venue": venue,
            "authors": list(authors),
            "doi": doi,
            "arxiv_id": arxiv,
            "s2_id": "",
            "openalex_id": "",
        }
    )
    rec["paper_key"] = key
    rec["status"] = "acquired"
    rec["access_status"] = "oa_pdf"
    rec["oa_pdf_url"] = url
    rec["oa_pdf_urls"] = list(dict.fromkeys([url] + list(rec.get("oa_pdf_urls") or [])))
    rec["oa_attempted"] = list(
        dict.fromkeys(list(rec.get("oa_attempted") or []) + [url])
    )
    rec["pdf_path"] = f"pdfs/{key}.pdf"
    rec["failure_reason"] = ""
    rec["retrieval_method"] = "manual"
    rec["ingest_source"] = source
    rec["updated_at"] = now
    if license_:
        rec["license"] = license_
    for k, v in (("title", title), ("venue", venue), ("year", year), ("doi", doi)):
        if v and not rec.get(k):
            rec[k] = v
    if authors and not rec.get("authors"):
        rec["authors"] = list(authors)
    merged_aspects = list(
        dict.fromkeys(list(rec.get("source_aspects") or []) + aspects)
    )
    rec["source_aspects"] = merged_aspects
    if binder:
        rec["binder"] = True
    return rec


def _last_row(path: str, key: str) -> dict | None:
    import json

    found = None
    if not os.path.exists(path):
        return None
    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip(b"\x00\r\n ")
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if r.get("paper_key") == key:
                found = r
    return found


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--doi", default="")
    ap.add_argument("--arxiv", default="")
    ap.add_argument("--authors", default="", help="semicolon-separated")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--venue", default="")
    ap.add_argument("--aspect", action="append", default=[], help="repeatable")
    ap.add_argument("--license", dest="license_", default="")
    ap.add_argument("--url", default="", help="where the operator found it")
    ap.add_argument("--source", default="operator hand-off")
    ap.add_argument("--binder", action="store_true")
    ap.add_argument("--root", default=CORPUS)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    pdf = os.path.expanduser(a.pdf)
    with open(pdf, "rb") as fh:
        if fh.read(5) != b"%PDF-":
            print("not a PDF:", pdf)
            return 2
    doi = a.doi.strip().lower()
    key = paper_key({"doi": doi, "arxiv_id": a.arxiv, "s2_id": "", "title": a.title})
    databank = os.path.join(a.root, "databank", "papers.jsonl")
    base = _last_row(databank, key)
    url = a.url or (f"https://doi.org/{doi}" if doi else "")
    rec = build_record(
        base,
        key=key,
        title=a.title,
        doi=doi,
        arxiv=a.arxiv,
        authors=[s.strip() for s in a.authors.split(";") if s.strip()],
        year=a.year,
        venue=a.venue,
        aspects=a.aspect,
        license_=a.license_,
        url=url,
        source=a.source,
        binder=a.binder,
        now=datetime.now(timezone.utc).isoformat(),
    )
    dest = os.path.join(a.root, rec["pdf_path"])
    verb = "UPDATE" if base else "NEW"
    print(f"{verb} {key}\n  title: {rec['title'][:90]}\n  pdf → {dest}")
    if base:
        print(
            f"  existing: status={base.get('status')} review={base.get('review_status')} pdf_path={base.get('pdf_path')}"
        )
    if os.path.exists(dest):
        same = (
            hashlib.sha256(open(dest, "rb").read()).digest()
            == hashlib.sha256(open(pdf, "rb").read()).digest()
        )
        print(
            "  destination exists —",
            "identical bytes" if same else "DIFFERENT bytes; refusing to overwrite",
        )
        if not same:
            return 3
    if not a.apply:
        print("dry run — pass --apply to copy the PDF and write the row.")
        return 0
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if not os.path.exists(dest):
        shutil.copy2(pdf, dest)
    from agent.actions.scholarly_actions import append_records
    from agent.effects.local import LocalEffects

    asyncio.run(append_records(LocalEffects(a.root), [rec]))
    print("written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
