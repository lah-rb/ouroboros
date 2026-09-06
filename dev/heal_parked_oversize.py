#!/usr/bin/env python3
"""Heal curate_oversize parks that wiped their papers' extraction rows, and
un-park the ones a seat can now take.

WHAT WENT WRONG. `_book_curate_oversize` appended a THREE-field row
({paper_key, extraction_status, failure_reason}) to the extraction sidecar,
which is last-row-wins on read. Every park therefore erased md_path,
figure_count, extraction_method, extraction_quality -- and on translated
papers, translated / md_en_path / translation_quality too. Measured
2026-09-06: 0 of 28 parked rows still carried extraction_quality; the
history row beneath each still did (the file is append-only, so nothing is
lost, only shadowed). Same incident class as 2026-08-22 (memory: never
append partial databank rows).

Many were also parked against the 65,536 local seat before the 262,144
remote seat was visible to the selector, or before the local seat grew to
131,072: 28 of 37 now fit a seat that exists.

WHAT THIS DOES, per parked paper: take the LAST non-park row from history
(the full record), then
  - if its deepest-compression floor fits the largest seat: restore that
    row verbatim (its prior extraction_status back, failure_reason cleared)
    -> the paper re-enters curation with its metadata intact;
  - otherwise: restore the full row but KEEP extraction_status
    curate_oversize and the park reason -> still parked, no longer wiped.

    python dev/heal_parked_oversize.py            # dry run
    python dev/heal_parked_oversize.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.scholarly_actions import (  # noqa: E402
    EXTRACTION_PATH,
    append_extraction_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
REMOTE_SEAT = 262_144  # llmvp_domains.curate_remote.seat_tokens in .agent/mission.json


async def main_async(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    parked = sorted(
        k for k, r in bank.items() if r.get("extraction_status") == "curate_oversize"
    )
    hist: dict[str, list[dict]] = collections.defaultdict(list)
    with open(os.path.join(a.root, EXTRACTION_PATH), encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if r.get("paper_key") in parked:
                hist[r["paper_key"]].append(r)
    usable = int(
        (max(REMOTE_SEAT, ca._CURATE_SEAT_TOKENS) - ca._CURATE_TURN_OVERHEAD_TOKENS)
        * ca._CURATE_OVERSIZE_PARK_MARGIN
    )
    out: list[dict] = []
    tally: collections.Counter = collections.Counter()
    for k in parked:
        prior = [r for r in hist[k] if r.get("extraction_status") != "curate_oversize"]
        if not prior:
            tally["no_history"] += 1
            print(f"  NO PRE-PARK ROW  {k[:70]}")
            continue
        row = dict(prior[-1])
        _, floor_chars = await ca._curate_doc_sizes(fx, k)
        floor_tok = int(floor_chars / ca._CURATE_CHARS_PER_TOKEN)
        if 0 < floor_tok <= usable:
            row["failure_reason"] = ""  # prior status restored as-is
            tally[f"unpark->{row.get('extraction_status')}"] += 1
            verdict = "UNPARK"
        else:
            row["extraction_status"] = "curate_oversize"
            row["failure_reason"] = str(bank[k].get("failure_reason") or "")[:300]
            tally["heal_keep_parked"] += 1
            verdict = "heal  "
        out.append(row)
        if a.show:
            print(
                f"  {verdict} ~{floor_tok:>8,} tok  restores {len(row):>2} fields  {k[:56]}"
            )
    print(
        f"\nparked {len(parked)} -> {dict(tally)}  (largest usable floor {usable:,} tok)"
    )
    if not a.apply:
        print("(dry run: nothing written)")
        return 0
    await append_extraction_records(fx, out)
    print(f"appended {len(out)} full extraction rows")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--show", action="store_true", default=True)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
