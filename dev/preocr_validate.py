#!/usr/bin/env python3
"""Score the real triage_one() against the hand-labelled 40.

Uses the SHIPPING code path, not a probe reimplementation — the point is to
measure what will actually run, including its render, its prompts and its
parser.
"""

from __future__ import annotations
import asyncio, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.effects.local import LocalEffects
from agent.actions.scholarly_actions import read_databank
from agent.actions.preocr_triage import triage_one

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    fx = LocalEffects(ROOT)
    bank = await read_databank(fx)
    lab = {
        r["key"]: r
        for r in json.load(open(os.path.join(ROOT, "label_subset_labelled.json")))
    }
    rows, t0 = [], time.time()
    for key, L in list(lab.items())[:n]:
        rec = bank.get(key) or {}
        pdf = rec.get("pdf_path")
        if not pdf:
            continue
        t1 = time.time()
        v = await triage_one(fx, key, pdf)
        dt = time.time() - t1
        rows.append((key, L, v, dt))
        print(
            f"  {dt:5.1f}s truth={L['type']:8s} -> {v['verdict']:8s} "
            f"bin={v['bin']:10s} {v['reason'][:44]}"
        )
    if not rows:
        print("no rows")
        return 1
    print(
        f"\ntotal {time.time()-t0:.0f}s, median {sorted(r[3] for r in rows)[len(rows)//2]:.1f}s/paper"
    )
    skipped_review = sum(
        1 for _k, L, v, _ in rows if L["type"] == "review" and v["verdict"] == "skip"
    )
    n_review = sum(1 for _k, L, _v, _ in rows if L["type"] == "review")
    lost = [
        (L, v)
        for _k, L, v, _ in rows
        if L["type"] == "research" and v["verdict"] == "skip"
    ]
    unknown = sum(1 for _k, _L, v, _ in rows if v["verdict"] == "unknown")
    print(f"  reviews correctly skipped : {skipped_review}/{n_review}")
    print(f"  RESEARCH WRONGLY SKIPPED  : {len(lost)}  <- each is a lost paper")
    for L, v in lost:
        print(f"      ! {L.get('why','')[:60]} => {v['reason'][:50]}")
    print(f"  fell through as unknown   : {unknown}  (these still get OCR'd)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
