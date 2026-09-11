#!/usr/bin/env python3
"""Does each accepted paper's markdown carry the DOI its record claims?

WHY. A served PDF is not always the requested paper: the beryl seven-
spectrometer record (doi_10.1002_jrs.5214) held a battery paper, and the
curator denied the wrong content without anyone noticing the swap. The
header of an extracted markdown usually prints the paper's own DOI, so a
mismatch between the record's DOI and the DOIs in the first few kilobytes
is the cheapest mis-serve detector we have. Read-only: it lists, the
operator decides (re-acquire, re-key or deny).

CLASSES
  match          the record DOI appears in the header
  variant        the header DOIs share the record's registrant prefix or are a
                 preprint/repository DOI (HAL, Zenodo, arXiv, OSF, SSRN,
                 ChemRxiv, ESSOAr) -- usually the same work, another copy
  mismatch       header DOIs from another registrant: the mis-serve list
  no_header_doi  nothing DOI-shaped in the header (scans, old papers)
  no_record_doi  the record has no DOI to check against

    .venv/bin/python tools/doi_header_check.py --working-dir ~/corpora/ouroboros-spectra \
        [--head-chars 6000] [--all] [--out ~/tmp/doi_header_check.json]

Measured while planning (2026-09-11): 2,077 accepted papers with markdown,
711 with a DOI in the first 6 kB, 655 match, 47 differ before the variant
filter.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>)\]}]+", re.IGNORECASE)
REPOSITORY_PREFIXES = (
    "10.48550",  # arXiv
    "10.5281",  # Zenodo
    "10.31219",  # OSF
    "10.2139",  # SSRN
    "10.26434",  # ChemRxiv
    "10.1002/essoar",
    "10.22541",  # Authorea/ESSOAr
    "10.13140",  # ResearchGate
    "10.5194",  # EGU discussion papers
)
HAL_RE = re.compile(r"hal-\d+|halshs-", re.IGNORECASE)


def normalise(doi: str) -> str:
    d = doi.strip().lower()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    return d.rstrip(".,;:)]}>").strip()


def last_rows(path: str) -> dict[str, dict]:
    """Last-row-wins read of a JSONL sidecar, keyed by paper_key."""
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001 -- a torn tail is not a finding
                continue
            key = row.get("paper_key")
            if key:
                out.setdefault(key, {}).update(row)
    return out


def classify(record_doi: str, header_dois: list[str], header_text: str) -> str:
    rec = normalise(record_doi) if record_doi else ""
    hdr = [normalise(h) for h in header_dois]
    if not rec:
        return "no_record_doi"
    if not hdr:
        return "no_header_doi"
    if rec in hdr or any(h.startswith(rec) or rec.startswith(h) for h in hdr):
        return "match"
    prefix = rec.split("/", 1)[0]
    if any(h.split("/", 1)[0] == prefix for h in hdr):
        return "variant"
    if any(h.startswith(p) for h in hdr for p in REPOSITORY_PREFIXES) or HAL_RE.search(
        header_text
    ):
        return "variant"
    return "mismatch"


def scan(
    working_dir: str, head_chars: int, include_all: bool
) -> tuple[collections.Counter, list[dict]]:
    papers = last_rows(os.path.join(working_dir, "databank", "papers.jsonl"))
    extraction = last_rows(os.path.join(working_dir, "databank", "extraction.jsonl"))
    counts: collections.Counter = collections.Counter()
    findings: list[dict] = []
    for key, rec in sorted(papers.items()):
        if not include_all and rec.get("review_status") != "accepted":
            continue
        ext = extraction.get(key) or {}
        md_path = ext.get("md_path") or rec.get("md_path")
        if not md_path:
            counts["no_markdown"] += 1
            continue
        path = md_path if os.path.isabs(md_path) else os.path.join(working_dir, md_path)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(head_chars)
        except OSError:
            counts["markdown_missing"] += 1
            continue
        header_dois = sorted({normalise(m) for m in DOI_RE.findall(head)})
        cls = classify(rec.get("doi") or "", header_dois, head)
        counts[cls] += 1
        if cls in ("mismatch", "variant"):
            findings.append(
                {
                    "paper_key": key,
                    "class": cls,
                    "record_doi": normalise(rec.get("doi") or ""),
                    "header_dois": header_dois[:6],
                    "title": (rec.get("title") or "")[:80],
                }
            )
    return counts, findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--working-dir", default=os.path.expanduser("~/corpora/ouroboros-spectra")
    )
    ap.add_argument("--head-chars", type=int, default=6000)
    ap.add_argument(
        "--all",
        action="store_true",
        help="every paper with markdown, not only accepted",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    counts, findings = scan(args.working_dir, args.head_chars, args.all)
    total = sum(counts.values())
    print(f"{total} papers scanned ({'all' if args.all else 'accepted'} with markdown)")
    for cls, n in counts.most_common():
        print(f"  {cls:16} {n:6}")
    mism = [f for f in findings if f["class"] == "mismatch"]
    print(f"\n{len(mism)} MISMATCH (header DOI from another registrant):")
    for f in mism:
        print(
            f"  {f['paper_key'][:44]:44} rec {f['record_doi'][:28]:28} hdr {', '.join(f['header_dois'][:2])[:44]}"
        )
        print(f"  {'':44} {f['title']}")
    if args.out:
        out = os.path.expanduser(args.out)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        json.dump(
            {"counts": dict(counts), "findings": findings}, open(out, "w"), indent=1
        )
        print("->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
