#!/usr/bin/env python3
"""Re-arm figtext for the port-day mass failure. One-shot.

WHY THESE ARE STUCK. On 2026-08-22 — the day the corpus moved machines —
the fig_review sidecar produced no reports (the cross-machine-port
failure shape: subprocess tools fail silently on a moved tree), and the
batch booking path mass-booked 965 papers `figtext_failed` in one day
("no report from tool"). The lane has been healthy since (1,325 done,
zero failures in current runs), so the status is an artifact, not a
verdict on the figures.

WHAT THIS DOES. For the failed papers that are STILL UNREVIEWED (the
curate pass hasn't happened, so figure text still improves the verdict
and the pack): clear figtext_status/figtext_progress so _fig_pending
selects them again. 25 papers, 1,992 figures (~20h of vision at the
measured ~37s/figure) on the 2026-08-26 dry run.

The 1,051 already-reviewed-md-only papers are LEFT ALONE — their
verdicts stand. The 623 of them that were ACCEPTED have packs built
without figure text; re-describing and repacking that cohort is a
separate, operator-sized decision (~15-20k figures), not this script.

Writes FULL merged records (append_records is last-row-replaces; partial
rows wipe fields — two incidents on 2026-08-22 were exactly this).

    python dev/rearm_failed_figtext.py --dry-run
    python dev/rearm_failed_figtext.py
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


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--force", action="store_true", help="run beside a live mission")
    args = ap.parse_args()

    if not args.dry_run and not args.force:
        import subprocess

        out = subprocess.run(
            ["pgrep", "-f", "[o]uroboros.py start"], capture_output=True, text=True
        )
        if out.stdout.strip():
            print(
                "REFUSING: a mission is running — papers.jsonl has ONE writer.\n"
                "Stop the mission, or --force if you know it is parked."
            )
            return 2

    fx = LocalEffects(args.root)
    bank = await read_databank(fx)
    hits = []
    for key, rec in bank.items():
        if rec.get("figtext_status") != "figtext_failed":
            continue
        if rec.get("review_status"):
            continue  # reviewed md-only; the verdict stands
        if rec.get("extraction_status") not in ("extracted", "extract_unverified"):
            continue
        hits.append((key, rec))

    figs = sum(int(r.get("figure_count") or 0) for _, r in hits)
    print(f"scanned {len(bank):,} records; {len(hits)} to re-arm ({figs:,} figures)")
    for key, rec in sorted(hits, key=lambda kr: int(kr[1].get("figure_count") or 0)):
        print(f"  {int(rec.get('figure_count') or 0):>4} figs  {key[:64]}")
    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    rows = []
    for key, rec in hits:
        merged = dict(rec)  # FULL record, never a partial one
        merged["paper_key"] = key
        merged["figtext_status"] = ""
        merged["figtext_progress"] = ""
        merged["failure_reason"] = ""
        rows.append(merged)
    if rows:
        await append_records(fx, rows)
    print(f"\nre-armed {len(rows)} records; the figtext lane re-selects them")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
