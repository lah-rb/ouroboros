#!/usr/bin/env python3
"""Triage unretrieved open-access papers, and ingest ones fetched by hand.

WHY THIS EXISTS. `access_status: oa_unresolved` lumps together two populations
that need opposite responses, and the pipeline currently treats them the same:

  HARD WALL (51%)   the publisher's WAF returned 403 to a polite crawler.
                    Nothing in the client fixes this — measured 2026-08-13:
                    Cloudflare on 69% of a sample, unmoved by a real browser
                    User-Agent (we already send one), unmoved by following
                    redirects. Chasing it means defeating bot detection, which
                    this pipeline does not do. The sanctioned routes are the
                    publishers' own text-mining APIs.

  NAVIGABLE (43%)   the fetch SUCCEEDED. We hold a real landing page, or a
                    figure thumbnail, and simply failed to find the PDF link
                    on it. No wall is involved. A human clicks through these
                    in seconds, and 92 of the 207 are one publisher.

Only the second kind is worth a person's time, and separating them is the
whole job of this tool. `failure_reason` already carries the distinction;
nothing was reading it.

TWO HALVES, because a worklist alone does not help:

    triage    -> classify, summarise, emit a TSV of the navigable ones
    ingest    -> take PDFs a human downloaded into a drop directory, file
                 them into the workspace and mark the records retrieved

Usage:
    python tools/oa_triage.py --working-dir ~/corpora/ouroboros-spectra
    python tools/oa_triage.py --working-dir ... --worklist /tmp/oa_todo.tsv
    python tools/oa_triage.py --working-dir ... --ingest ~/Downloads/oa_drop

Stdlib only — runs under the repo venv, no extraction deps.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The buckets, in the order a reader should think about them.
NAVIGABLE = ("landing_page", "wrong_asset")

_BUCKET_NOTE = {
    "landing_page": "page fetched OK — PDF link not found on it",
    "wrong_asset": "followed a link to a figure/thumbnail, not the paper",
    "hard_wall": "403 WAF — polite crawler stops here",
    "async_pending": "202 — publisher is still preparing the file",
    "gone": "404 — location no longer exists",
    "other": "unclassified",
}


# ONE definition, and it lives beside the code that WRITES failure_reason
# (agent/actions/scholarly_actions.py). A second copy here would drift the
# moment a failure string changed, and this tool's whole job is reading those
# strings correctly. The repo root goes on the path because tools/ is not a
# package; the import is stdlib-only downstream.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.actions.scholarly_actions import classify_failure as bucket  # noqa: E402


def read_records(databank: Path) -> dict:
    """paper_key -> record, last-record-wins.

    papers.jsonl is an APPEND-ONLY LOG, not a table: ~24% of its lines are
    rewrites of records already present. Counting lines overstates the corpus
    by that much, which is a mistake worth not repeating.
    """
    recs: dict = {}
    path = databank / "papers.jsonl"
    if not path.is_file():
        sys.exit(f"no databank at {path}")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("paper_key"):
            recs[r["paper_key"]] = r
    return recs


def host_of(url: str) -> str:
    return url.split("/")[2] if "//" in (url or "") else "(none)"


def triage(recs: dict) -> dict:
    out: dict = collections.defaultdict(list)
    for r in recs.values():
        if r.get("access_status") != "oa_unresolved":
            continue
        out[bucket(r.get("failure_reason"))].append(r)
    return out


def cmd_triage(recs: dict, worklist: Path | None) -> None:
    groups = triage(recs)
    total = sum(len(v) for v in groups.values())
    print(f"cataloged (unique)  : {len(recs):,}")
    print(f"oa_unresolved       : {total:,}\n")
    for k, v in sorted(groups.items(), key=lambda x: -len(x[1])):
        mark = "<-- WORTH A PERSON'S TIME" if k in NAVIGABLE else ""
        print(
            f"  {k:14s} {len(v):5d}  {len(v)/max(total,1)*100:5.1f}%  "
            f"{_BUCKET_NOTE[k]:44s} {mark}"
        )

    nav = [r for k in NAVIGABLE for r in groups.get(k, [])]
    print(
        f"\n  NAVIGABLE: {len(nav)}  ({len(nav)/max(total,1)*100:.0f}% of unresolved)"
    )
    if nav:
        hosts = collections.Counter(host_of(r.get("oa_pdf_url") or "") for r in nav)
        print("  by host:")
        for h, n in hosts.most_common(8):
            print(f"    {n:5d}  {h}")

    if worklist and nav:
        worklist.parent.mkdir(parents=True, exist_ok=True)
        with worklist.open("w", encoding="utf-8") as fh:
            fh.write("paper_key\tdoi\thost\tbucket\ttried_url\tdoi_url\ttitle\n")
            # Grouped by host: a human working this list stays on one
            # publisher's layout at a time instead of relearning it each row.
            for r in sorted(
                nav,
                key=lambda r: (host_of(r.get("oa_pdf_url") or ""), r.get("doi") or ""),
            ):
                doi = (r.get("doi") or "").strip()
                fh.write(
                    "\t".join(
                        [
                            r.get("paper_key", ""),
                            doi,
                            host_of(r.get("oa_pdf_url") or ""),
                            bucket(r.get("failure_reason")),
                            (r.get("oa_pdf_url") or "").replace("\t", " "),
                            f"https://doi.org/{doi}" if doi else "",
                            (r.get("title") or "").replace("\t", " ")[:160],
                        ]
                    )
                    + "\n"
                )
        print(f"\n  worklist -> {worklist}  ({len(nav)} rows)")
        print("  Save each PDF as <paper_key>.pdf, then re-run with --ingest.")


def mission_is_running(working_dir: Path) -> bool:
    """Is an agent working this databank right now?

    `ps -Ao command=`, NOT `pgrep -fa`. macOS pgrep accepts `-a` and silently
    IGNORES it, printing bare PIDs — so matching a working-dir path against
    that output can never succeed and the guard returns False every time. It
    looked like it worked; it was off. Portable full command lines, and match
    on the resolved path.

    FAILS CLOSED. If the process table cannot be read we report True: a
    spurious refusal costs the operator one pause, a spurious allow costs a
    silent half-rewrite of papers.jsonl.
    """
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    target = str(working_dir)
    return any(
        "ouroboros.py start" in ln and (target in ln or working_dir.name in ln)
        for ln in out.splitlines()
    )


def cmd_ingest(working_dir: Path, recs: dict, drop: Path) -> int:
    """File hand-downloaded PDFs into the workspace and mark them retrieved."""
    # THE RACE THIS REFUSES. append_records is a read-modify-write of the WHOLE
    # papers.jsonl, so a write here while the mission is appending loses one
    # side's work entirely — whichever writes second wins. That is the exact
    # hazard read_databank's docstring documents. Refuse rather than corrupt.
    if mission_is_running(working_dir):
        print(
            "REFUSING: a mission is running on this workspace.\n"
            "  papers.jsonl is rewritten wholesale on append, so writing now "
            "would silently drop one side's records.\n"
            "  Pause the mission (ouroboros.py mission pause) and re-run."
        )
        return 2

    pdfs_dir = working_dir / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    found = sorted(p for p in drop.glob("*.pdf") if p.is_file())
    if not found:
        print(f"no .pdf files in {drop}")
        return 1

    updates, skipped = [], []
    for src in found:
        key = src.stem
        rec = recs.get(key)
        if rec is None:
            skipped.append((src.name, "no record with that paper_key"))
            continue
        head = src.open("rb").read(5)
        if head != b"%PDF-":
            skipped.append((src.name, f"not a PDF (magic {head!r})"))
            continue
        dest = pdfs_dir / f"{key}.pdf"
        shutil.copy2(src, dest)
        rec = dict(rec)
        rec["pdf_path"] = os.path.join("pdfs", dest.name)
        rec["access_status"] = "oa_pdf"
        rec["failure_reason"] = ""
        # Provenance: a human fetched this, and a later audit should be able
        # to tell it from a crawler retrieval without guessing.
        rec["retrieval_method"] = "manual"
        updates.append(rec)

    if updates:
        path = working_dir / "databank" / "papers.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            for rec in updates:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"ingested {len(updates)} PDF(s) into {pdfs_dir}")
    for name, why in skipped:
        print(f"  skipped {name}: {why}")
    if updates:
        print("Run the extractor to OCR them (they are now oa_pdf with a pdf_path).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--working-dir", required=True)
    ap.add_argument("--worklist", default="", help="write a TSV of navigable rows")
    ap.add_argument("--ingest", default="", help="directory of hand-downloaded PDFs")
    args = ap.parse_args()

    wd = Path(os.path.expanduser(args.working_dir)).resolve()
    recs = read_records(wd / "databank")

    if args.ingest:
        return cmd_ingest(wd, recs, Path(os.path.expanduser(args.ingest)).resolve())
    cmd_triage(recs, Path(args.worklist).resolve() if args.worklist else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
