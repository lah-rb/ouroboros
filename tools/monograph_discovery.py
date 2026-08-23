#!/usr/bin/env python3
"""Open-monograph discovery: classic textbooks and reports onto a SHELF.

Operator intent (2026-08-22): open-use classics (Dana's Manual, state
mineral surveys, USGS series reports) are genuinely useful for the
GENERAL-KNOWLEDGE full fine-tune pass, not the near-term LoRA — so this
is a SHELF, deliberately outside the scraper pipeline. Nothing here
auto-downloads at scale, auto-OCRs, or enters curation; each stage is an
explicit operator action:

    discover  tools/monograph_discovery.py                 -> shelf rows
    download  tools/monograph_discovery.py --download N    -> PDFs on disk
    admit     tools/monograph_discovery.py --admit ID ...  -> papers.jsonl
              (enters the normal OCR/book pipeline from there)

Sources:
  - Internet Archive advancedsearch, mediatype:texts, TRULY-OPEN filter
    (year<=1929 public-domain era OR an explicit licenseurl) — the
    borrowable-only lending library is excluded by construction.
  - USGS Publications Warehouse (US-gov = public domain). Records link
    PDFs as typed links; report/sheet PDFs under pubs.usgs.gov are taken,
    external publisher DOIs are skipped (not ours to fetch here).

Shelf: databank/monographs.jsonl (append, dedup by source id).
PDFs:  databank/monographs/<id>.pdf
BOOK-SCALE CURATION DOES NOT EXIST YET — admitted volumes will extract
via the segmented book path but then sit review-pending; admit for
extraction value, not for the current pack pipeline.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "ouroboros-scraper/1.0 (mailto:luke.a.hayes@outlook.com)"}
IA = "https://archive.org/advancedsearch.php"
USGS = "https://pubs.usgs.gov/pubs-services/publication/"

QUERIES = [
    "mineralogy",
    "crystallography",
    "spectroscopy minerals",
    "petrography",
    "x-ray diffraction",
    "emission spectra",
    "reflectance spectra minerals",
    "pigments analysis",
]


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def ia_rows(q):
    query = f"mediatype:texts AND ({q}) AND " f"(year:[1800 TO 1929] OR licenseurl:*)"
    params = urllib.parse.urlencode(
        {
            "q": query,
            "rows": 100,
            "output": "json",
            "sort[]": "downloads desc",
            "fl[]": ["identifier", "title", "year", "licenseurl", "subject", "creator"],
        },
        doseq=True,
    )
    d = get(f"{IA}?{params}")
    for doc in d.get("response", {}).get("docs", []):
        ident = doc.get("identifier")
        if not ident:
            continue
        yield {
            "source": "internet_archive",
            "id": f"ia_{ident}",
            "title": str(doc.get("title") or "")[:300],
            "year": doc.get("year"),
            "creator": str(doc.get("creator") or "")[:200],
            "license": doc.get("licenseurl") or "public-domain-era",
            "download_url": f"https://archive.org/download/{ident}/{ident}.pdf",
            "landing": f"https://archive.org/details/{ident}",
            "query": q,
        }


def usgs_rows(q):
    d = get(f"{USGS}?q={urllib.parse.quote(q)}&page_size=100")
    for r in d.get("records", []):
        pdfs = [
            l.get("url")
            for l in (r.get("links") or [])
            if (l.get("url") or "").lower().endswith(".pdf")
            and "pubs.usgs.gov" in (l.get("url") or "")
        ]
        if not pdfs:
            continue
        yield {
            "source": "usgs_pubs",
            "id": f"usgs_{r.get('indexId')}",
            "title": str(r.get("title") or "")[:300],
            "year": r.get("publicationYear"),
            "creator": "",
            "license": "us-gov-public-domain",
            "download_url": sorted(pdfs, key=len)[0],
            "all_pdfs": pdfs[:20],
            "landing": f"https://doi.org/{r.get('doi')}" if r.get("doi") else "",
            "query": q,
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/ouroboros-spectra"))
    ap.add_argument(
        "--download",
        type=int,
        default=0,
        metavar="N",
        help="download the first N shelved-but-unfetched PDFs",
    )
    ap.add_argument(
        "--admit",
        nargs="*",
        default=[],
        help="shelf ids to admit into papers.jsonl (enters OCR)",
    )
    args = ap.parse_args()
    root = args.root
    shelf_path = os.path.join(root, "databank", "monographs.jsonl")
    os.makedirs(os.path.join(root, "databank", "monographs"), exist_ok=True)
    shelf = {}
    if os.path.exists(shelf_path):
        for line in open(shelf_path, encoding="utf-8"):
            d = json.loads(line)
            shelf[d["id"]] = d

    if not args.download and not args.admit:
        titles_seen = {
            re.sub(r"\W+", " ", (d.get("title") or "").lower()).strip()[:80]
            for d in shelf.values()
        }
        new = []
        for q in QUERIES:
            for fn in (ia_rows, usgs_rows):
                try:
                    for row in fn(q):
                        tkey = re.sub(r"\W+", " ", row["title"].lower()).strip()[:80]
                        if row["id"] in shelf or tkey in titles_seen:
                            continue
                        shelf[row["id"]] = row
                        titles_seen.add(tkey)
                        new.append(row)
                except Exception as e:
                    print(f"  {fn.__name__}({q!r}) failed: {e}", flush=True)
                time.sleep(1.2)
        with open(shelf_path, "a", encoding="utf-8") as w:
            for row in new:
                w.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"shelved {len(new)} new (shelf total {len(shelf)})")
        return 0

    if args.download:
        n = 0
        for d in shelf.values():
            if n >= args.download:
                break
            dest = os.path.join(root, "databank", "monographs", d["id"] + ".pdf")
            if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
                continue
            try:
                req = urllib.request.Request(d["download_url"], headers=UA)
                with urllib.request.urlopen(req, timeout=300) as r:
                    data = r.read(300_000_000)
                if data[:4] != b"%PDF":
                    print(f"  not a pdf: {d['id']}", flush=True)
                    continue
                open(dest, "wb").write(data)
                n += 1
                print(
                    f"  ok {d['id']} ({len(data)/1e6:.1f} MB) {d['title'][:50]}",
                    flush=True,
                )
            except Exception as e:
                print(f"  miss {d['id']}: {e}", flush=True)
            time.sleep(2.0)
        print(f"downloaded {n}")
        return 0

    # --admit: move into the pipeline as oa_pdf with pdf on disk
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    lines = []
    for sid in args.admit:
        d = shelf.get(sid)
        if not d:
            print(f"  unknown shelf id: {sid}")
            continue
        pdf = os.path.join("databank", "monographs", sid + ".pdf")
        if not os.path.isfile(os.path.join(root, pdf)):
            print(f"  {sid}: PDF not downloaded yet (--download first)")
            continue
        lines.append(
            json.dumps(
                {
                    "paper_key": sid,
                    "title": d["title"],
                    "year": d.get("year"),
                    "status": "cataloged",
                    "access_status": "oa_pdf",
                    "pdf_path": pdf,
                    "license": d.get("license", ""),
                    "discovery_method": "open_monograph",
                    "source_aspects": [],
                    "doi": "",
                    "updated_at": now,
                },
                ensure_ascii=False,
            )
        )
        print(f"  admitted {sid}: {d['title'][:60]}")
    if lines:
        with open(
            os.path.join(root, "databank", "papers.jsonl"), "a", encoding="utf-8"
        ) as w:
            w.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
