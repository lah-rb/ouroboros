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

The already-reviewed-md-only papers KEEP figtext_failed (clearing it
would re-arm decided papers — _fig_pending does not read review_status)
but their failure_reason is canonicalized to name the port-day artifact,
so every failure row in the databank self-describes its true cause and
NEW failures stand out. The 623 ACCEPTED among them have packs built
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

from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--force", action="store_true", help="run beside a live mission")
    ap.add_argument(
        "--accepted",
        action="store_true",
        help=(
            "ALSO re-arm the accepted-md-only cohort (the 624): their "
            "figure sets are complete on disk (verified 2026-08-26: "
            "624/624 full PNG sets, 624/624 anchored markdown) and only "
            "the descriptions are missing. Clearing figtext_failed sends "
            "them through the fig pass; repack is booked separately once "
            "descriptions exist. Operator-gated because it is ~18.5k "
            "figures of vision work."
        ),
    )
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
    PORT_MARK = (
        "fig_review: mass failure 2026-08-22 (cross-machine port, tool "
        "produced no reports); reviewed md-only, verdict stands"
    )
    hits, closed = [], []
    for key, rec in bank.items():
        if rec.get("figtext_status") != "figtext_failed":
            continue
        if rec.get("review_status"):
            if args.accepted and rec.get("review_status") == "accepted":
                hits.append((key, rec))  # campaign: describe, repack later
                continue
            # Reviewed md-only: keep the terminal status, but stamp the
            # true historical cause over whatever stale reason later
            # appends left behind — the failure list then reads clean.
            if rec.get("failure_reason") != PORT_MARK:
                closed.append((key, rec))
            continue
        if rec.get("extraction_status") not in (
            "extracted",
            "extract_unverified",
            # Routed to translation (route_legacy_cjk_to_translation.py):
            # clearing figtext now is inert — _fig_pending requires a
            # usable status — and arms the fig pass for the moment the
            # translation drain books the paper back to "extracted".
            "extract_lingual",
        ):
            continue
        hits.append((key, rec))

    figs = sum(int(r.get("figure_count") or 0) for _, r in hits)
    print(
        f"scanned {len(bank):,} records; {len(hits)} to re-arm "
        f"({figs:,} figures); {len(closed)} reviewed rows to stamp"
    )
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
    # The stamp goes to the EXTRACTION sidecar, not papers.jsonl:
    # failure_reason is an extraction-owned field, so the sidecar shadows
    # the papers side on read — a papers-side stamp is invisible in the
    # merged view (discovered when the first stamping pass re-stamped).
    # FULL merged records, exactly as on the papers side: the sidecar is
    # ALSO last-row-replaces per key, and a stamp-only row wiped the
    # extraction fields of 357 records on 2026-08-26 (healed from
    # history the same day). append_extraction_records filters a merged
    # record to the owned fields — that filter is what makes passing the
    # whole record safe, and passing less than the whole record is what
    # is NOT safe.
    if closed:
        await append_extraction_records(
            fx,
            [
                {**dict(rec), "paper_key": key, "failure_reason": PORT_MARK}
                for key, rec in closed
            ],
        )
    print(
        f"\nre-armed {len(hits)} records; stamped {len(closed)} reviewed "
        "rows with the port-day marker"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
