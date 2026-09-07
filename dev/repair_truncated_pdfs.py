#!/usr/bin/env python3
"""Route truncated PDFs back to the acquisition lane. One-shot.

WHY THESE ARE STUCK. A download whose connection was cut mid-stream lands
with a valid `%PDF-` head and no `%%EOF` trailer. Every check in
`http_download` looked at the head, so `dl.success` was True and
`action_download_papers` booked a `pdf_path` — and that same action skips
any record that already has one (`scholarly_actions.py`:
`if rec.get("access_status") != "oa_pdf" or rec.get("pdf_path"): continue`).
The `recover` lane only selects `oa_unresolved` records. So the file is
frozen: no path re-fetches it, and OCR either fails on a fragment or, worse,
extracts a *faithful extraction of a fragment* (pymupdf silently rebuilds a
missing xref) that then reaches the curator as `extract_unverified`.

Measured 2026-08-25: 27 of 3,134 PDFs on disk, 22 at exactly 1 MiB and 3 at
exactly 5 MiB — a server- or CDN-side cut at a buffer boundary, not anything
this repo does.

WHAT THIS DOES. For each affected record: clear `pdf_path`, set
`access_status="oa_unresolved"`, and clear `oa_recover_attempted_at`. That is
the exact shape the existing `recover` lane selects, so the papers re-enter
acquisition with NO new wiring. The truncated file itself is deleted, because
a partial PDF on disk is precisely what later `os.path.exists` logic adopts.

Re-running is safe: a record already repaired no longer has a `pdf_path`, and
a file already deleted is skipped.

    python dev/repair_truncated_pdfs.py --dry-run    # report only
    python dev/repair_truncated_pdfs.py             # apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import append_records, read_databank  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
TAIL_BYTES = 4096


def is_truncated(path: str) -> bool:
    """A whole PDF ends with a %%EOF trailer. Read a window, not the last
    bytes: real files carry incremental-update padding after it."""
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return True
        with open(path, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                return False  # not a PDF at all; a different problem
            fh.seek(max(0, size - TAIL_BYTES))
            return b"%%EOF" not in fh.read()
    except OSError:
        return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument(
        "--force",
        action="store_true",
        help="write even with a mission running (loses appends)",
    )
    args = ap.parse_args()

    if not args.dry_run and not args.force:
        import subprocess

        out = subprocess.run(
            ["pgrep", "-f", "[o]uroboros.py start"], capture_output=True, text=True
        )
        if out.stdout.strip():
            print(
                "REFUSING: a mission is running (pid "
                f"{out.stdout.split()[0]}).\n"
                "papers.jsonl has ONE writer by design — both this script and\n"
                "the mission do a whole-file read-modify-write, so running\n"
                "them together loses appends. Stop the mission, or --force if\n"
                "you know it is parked."
            )
            return 2

    fx = LocalEffects(args.root)
    bank = await read_databank(fx)

    hits = []
    for key, rec in bank.items():
        p = rec.get("pdf_path")
        if not p:
            continue
        full = p if os.path.isabs(p) else os.path.join(args.root, p)
        if is_truncated(full):
            hits.append((key, rec, full))

    print(f"scanned {len(bank)} records; {len(hits)} truncated PDFs")
    if not hits:
        return 0

    for key, rec, full in hits:
        size = os.path.getsize(full) if os.path.exists(full) else 0
        mib = size / (1024 * 1024)
        note = "  <- exactly N MiB" if size and size % (1024 * 1024) == 0 else ""
        print(f"  {size:>10,} B ({mib:5.2f} MiB){note}  {key[:56]}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    repaired = []
    for key, rec, full in hits:
        try:
            if os.path.exists(full):
                os.unlink(full)
        except OSError as e:
            print(f"  ! could not remove {full}: {e}")
            continue
        # FULL MERGED RECORD, never a partial one. append_records has
        # last-row-REPLACES semantics: a row carrying only the changed keys
        # wipes every field it omits — title, abstract, doi, source_aspects,
        # the lot. Two incidents on 2026-08-22 were exactly this.
        merged = dict(rec)
        merged["paper_key"] = key
        merged["pdf_path"] = ""
        merged["access_status"] = "oa_unresolved"
        # Clearing the stamp is what makes the recover lane eligible again —
        # it selects on `not oa_recover_attempted_at`.
        merged["oa_recover_attempted_at"] = ""
        merged["failure_reason"] = "pdf truncated on download; re-queued"
        repaired.append(merged)

    if repaired:
        await append_records(fx, repaired)
    print(f"\nrepaired {len(repaired)} records -> access_status=oa_unresolved")
    print("the existing `recover` lane will re-attempt them; no new wiring")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
