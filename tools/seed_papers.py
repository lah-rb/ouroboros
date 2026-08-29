#!/usr/bin/env python3
"""Manually seed papers into the scraper pipeline.

WHY. The foundations goals (2026-08-29) want hand-picked papers — the
Raman scattering theory paper, the phase-diagram monograph chapter — that
discovery may never surface. This tool books them exactly the way the
pipeline expects, so a seeded paper flows OCR → figtext → curation like
any discovered one. It merges the two proven manual paths:
tools/monograph_discovery.py --admit (record construction from nothing)
and tools/oa_triage.py --ingest (mission refusal + %PDF- magic check).

TWO SHAPES:

* PDF in hand — `--pdf file.pdf --title "..." [--doi ...]`:
  the PDF is copied to <workspace>/pdfs/<key>.pdf and the record books
  status "cataloged" + access_status "oa_pdf", which is all the OCR
  selector needs (_extraction_pending). "cataloged", not "candidate":
  a candidate would re-route through the catalog phase, whose OA
  resolution could overwrite access_status.

* DOI only — `--doi 10.xxxx/yyyy [--title ...]`:
  books status "candidate" + access_status "", the exact shape
  catalog_sweep_next picks up; resolve_oa_pdf then does the Unpaywall/
  CORE work. openalex_id is deliberately left EMPTY — the hopeless-
  deprioritisation and OUROBOROS_SKIP_HOPELESS_UNPAYWALL branches both
  key on a present openalex_id with no URLs, and an empty one sorts the
  seed to the FRONT of the batch.

RECORD HYGIENE (the rules that have bitten before):
* papers.jsonl is last-row-REPLACES per key — every append here is a
  complete record, never a partial.
* extraction-owned fields (extraction_status, md_path, …) are NEVER
  written to papers.jsonl; the append filter in the agent drops them
  silently and a direct append would shadow the sidecar. This tool
  writes none of them.
* Refuses while a mission runs (write racing = silent record loss).

Usage:
  .venv/bin/python tools/seed_papers.py --workspace ~/corpora/ouroboros-spectra \
      --aspect "Spectroscopy technique physics" \
      --pdf ~/downloads/placzek_raman_theory.pdf --title "..." --doi 10.1002/...
  .venv/bin/python tools/seed_papers.py --doi 10.2138/am-2004-0403 \
      --aspect "Mineral formation and chemistry"
  # batch mode: a JSONL manifest with {pdf?, doi?, title?, abstract?, year?}
  .venv/bin/python tools/seed_papers.py --manifest seeds.jsonl --aspect "..."
Add --apply to write; default is a dry run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def mission_is_running() -> bool:
    """Fail CLOSED (oa_triage precedent)."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001
        return True
    return any(
        "ouroboros.py start" in ln for ln in out.splitlines() if "grep" not in ln
    )


def build_record(
    *,
    title: str,
    doi: str,
    abstract: str,
    year: int | None,
    aspect: str,
    pdf_rel: str,
) -> dict:
    from agent.actions.scholarly_actions import paper_key

    rec = {
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "year": year or 0,
        "venue": "",
        "authors": [],
        "arxiv_id": "",
        # EMPTY on purpose — see the module docstring's DOI-only notes.
        "s2_id": "",
        "openalex_id": "",
        "source_aspects": [aspect] if aspect else [],
        "tags": [],
        "reference_dois": [],
        "referenced_works": [],
        "language": "",
        "license": "",
        "failure_reason": "",
        "oa_pdf_url": "",
        "oa_pdf_urls": [],
        "oa_attempted": [],
        "discovery_method": "seeded",
        "retrieval_method": "manual" if pdf_rel else "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if pdf_rel:
        rec["status"] = "cataloged"
        rec["access_status"] = "oa_pdf"
        rec["pdf_path"] = pdf_rel
    else:
        rec["status"] = "candidate"
        rec["access_status"] = ""
        rec["pdf_path"] = ""
    rec["paper_key"] = paper_key(rec)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--workspace", default=os.path.expanduser("~/corpora/ouroboros-spectra")
    )
    ap.add_argument("--pdf", help="local PDF to seed (copied into pdfs/)")
    ap.add_argument("--doi", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--abstract", default="")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument(
        "--aspect",
        default="",
        help="aspect name for source_aspects (must match a plan aspect for "
        "coverage counting; papers flow to curation regardless)",
    )
    ap.add_argument(
        "--manifest",
        help="JSONL of {pdf?, doi?, title?, abstract?, year?} for batch seeding",
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if mission_is_running():
        print("REFUSING: a mission is running on this workspace.")
        return 2

    wd = args.workspace
    papers_path = os.path.join(wd, "databank", "papers.jsonl")
    if not os.path.isfile(papers_path):
        print(f"no databank at {papers_path}")
        return 2

    # existing keys, for duplicate suppression
    existing: set[str] = set()
    with open(papers_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                k = json.loads(line).get("paper_key")
            except Exception:  # noqa: BLE001
                continue
            if k:
                existing.add(k)

    entries: list[dict] = []
    if args.manifest:
        with open(args.manifest, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
    else:
        entries.append(
            {
                "pdf": args.pdf,
                "doi": args.doi,
                "title": args.title,
                "abstract": args.abstract,
                "year": args.year,
            }
        )

    rows: list[dict] = []
    copies: list[tuple[str, str]] = []
    for e in entries:
        pdf = e.get("pdf") or ""
        doi = str(e.get("doi") or "").strip()
        title = str(e.get("title") or "").strip()
        if not pdf and not doi:
            print(f"  SKIP (neither pdf nor doi): {e}")
            continue
        pdf_rel = ""
        if pdf:
            pdf = os.path.expanduser(pdf)
            try:
                with open(pdf, "rb") as fh:
                    if fh.read(5) != b"%PDF-":
                        print(f"  SKIP (not a PDF by magic): {pdf}")
                        continue
            except OSError as exc:
                print(f"  SKIP (unreadable): {pdf} — {exc}")
                continue
            if not title and not doi:
                # a key needs SOME identity; fall back to the filename
                title = os.path.splitext(os.path.basename(pdf))[0]
        rec = build_record(
            title=title,
            doi=doi,
            abstract=str(e.get("abstract") or ""),
            year=e.get("year"),
            aspect=args.aspect,
            pdf_rel="",  # set after key derivation
        )
        if pdf:
            rec["status"] = "cataloged"
            rec["access_status"] = "oa_pdf"
            rec["pdf_path"] = f"pdfs/{rec['paper_key']}.pdf"
            rec["retrieval_method"] = "manual"
            copies.append((pdf, os.path.join(wd, rec["pdf_path"])))
        if rec["paper_key"] in existing:
            print(f"  SKIP (already in corpus): {rec['paper_key']}")
            continue
        existing.add(rec["paper_key"])
        rows.append(rec)
        print(
            f"  seed {rec['paper_key']}  "
            f"({'pdf-in-hand' if rec['pdf_path'] else 'doi-only'})"
        )

    print(f"\n{len(rows)} record(s) ready")
    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0
    if not rows:
        return 0
    for src, dst in copies:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    with open(papers_path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"appended {len(rows)} record(s); copied {len(copies)} PDF(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
