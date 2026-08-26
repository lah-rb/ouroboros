#!/usr/bin/env python3
"""Route legacy CJK-heavy extractions to the translation drain. One-shot.

WHY THESE ARE STUCK. The lingual gate (extraction_actions:1650) routes a
substantially non-Latin extraction to `extract_lingual` by reading the
toolchain's `script_profile` — but extractions that PREDATE the profile
machinery carry none, and `script_nonlatin_frac({}) == 0.0` reads as pure
Latin. Those papers booked plain `extracted` and entered the curate queue
as raw CJK markdown, where char-based sizing under-counted them ~2.4x
(the 2026-08-26 poison-pill incident was one of them). The gate itself is
NOT broken: every profile-carrying CJK paper checked on 2026-08-26 was
correctly routed, translated, and booked back with an en.md.

WHAT THIS DOES. For each record with extraction_status == "extracted",
no review verdict yet, no en.md, and a markdown whose letters are >= 15%
CJK (the same LINGUAL_NONLATIN_MIN bar the live gate uses): book
`extract_lingual` to the extraction sidecar with the computed profile and
a reason naming this script. The existing translation drain then owns
them — no new wiring. Papers already reviewed keep their verdicts; parked
curate_oversize papers stay parked.

Dry-run on 2026-08-26 found 13 such records of 2,529 extracted.

    python dev/route_legacy_cjk_to_translation.py --dry-run
    python dev/route_legacy_cjk_to_translation.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.extraction_actions import LINGUAL_NONLATIN_MIN  # noqa: E402
from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
_CJK = re.compile(r"[　-鿿가-힯豈-﫿＀-￯]")
_LATIN = re.compile(r"[A-Za-z]")


def _profile(text: str) -> dict:
    """Letters-only, matching extract_batch._script_profile semantics."""
    cjk, lat = len(_CJK.findall(text)), len(_LATIN.findall(text))
    total = cjk + lat
    if not total:
        return {}
    return {
        "latin": round(lat / total, 3),
        "cjk": round(cjk / total, 3),
        "nonlatin": round(cjk / total, 3),
    }


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
            print("REFUSING: a mission is running. Stop it, or --force.")
            return 2

    fx = LocalEffects(args.root)
    bank = await read_databank(fx)
    hits = []
    for key, rec in bank.items():
        if rec.get("extraction_status") != "extracted":
            continue
        if rec.get("review_status") or rec.get("md_en_path"):
            continue
        md = rec.get("md_path")
        if not md:
            continue
        path = md if os.path.isabs(md) else os.path.join(args.root, md)
        try:
            text = open(path, encoding="utf-8", errors="replace").read(200_000)
        except OSError:
            continue
        prof = _profile(text)
        if prof and prof["nonlatin"] >= LINGUAL_NONLATIN_MIN:
            hits.append((key, prof))

    print(f"scanned {len(bank):,} records; {len(hits)} legacy CJK extractions")
    for key, prof in hits:
        print(f"  nonlatin {prof['nonlatin']:.2f}  {key[:70]}")
    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    rows = [
        {
            "paper_key": key,
            "extraction_status": "extract_lingual",
            "script_profile": prof,
            "failure_reason": (
                "legacy extraction predates the script profile; routed to "
                f"translation (nonlatin {prof['nonlatin']:.2f}, "
                "dev/route_legacy_cjk_to_translation.py)"
            ),
        }
        for key, prof in hits
    ]
    if rows:
        await append_extraction_records(fx, rows)
    print(f"\nrouted {len(rows)} records -> extract_lingual (translation drain)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
