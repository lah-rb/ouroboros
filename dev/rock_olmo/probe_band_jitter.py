#!/usr/bin/env python3
"""Is bands → name an exact-string lookup or approximate spectral matching? (§22g follow-up)

The granular arm (v3g) names trained species from their four strongest bands at 0.47 —
but also from ONE band at 0.28, above the 0.03 ceiling that ±10 cm-1 matching allows.
That pattern fits memorised exact integers ("1008" → Gypsum), not spectral matching. Here
every band in the prompt is shifted by ±δ cm-1 (seeded random sign per band), δ ∈
{0, 1, 2, 3, 5, 10}, well inside the ±10 tolerance used everywhere else. If the score
survives δ = 1–3, the model matches approximately; if it collapses, it looks up strings.
LIBS lines are left exact in the bands + lines pair. Also reports the EXACT-value
ceiling: 1 / species sharing the identical integers.

  ./.venv/bin/python probe_band_jitter.py --models v3,v3g
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corpus_xml as cx  # noqa: E402
import probe_chains as pc  # noqa: E402
import probe_pairs as pp  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

PAIRS = ((("bands1",), "name"), (("bands2",), "name"), (("bands",), "name"), (("bands", "lines"), "name"))
DELTAS = (0, 1, 2, 3, 5, 10)


def jitter(bands: list[int], delta: int, rng: random.Random) -> list[int]:
    return [b + (delta if rng.random() < 0.5 else -delta) for b in bands]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="v3,v3g")
    ap.add_argument("--sample", default="T=60,V=0,U=0")
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/v3/pair_probe/band_jitter.json"))
    args = ap.parse_args()
    groups = pp.sample_species({k: int(v) for k, v in (kv.split("=") for kv in args.sample.split(","))}, args.seed)
    recs = json.load(open(pc.RECORDS))
    allr = recs["trained"] + recs["untouched"]
    # exact-value ceilings: species sharing the identical first-k integers
    exact = collections.Counter()
    for r in allr:
        for k in (1, 2, 4):
            exact[(k, tuple(sorted(r["bands"][:k])))] += 1
    jobs = []
    for sp in groups["T"]:
        g = pc.load_gold(sp)
        for d in DELTAS:
            rng = random.Random(f"{args.seed}|{sp}|{d}")
            gj = dict(g, bands=jitter(g["bands"], d, rng))
            for cues, target in PAIRS:
                text = cx.stripped_record(pp._rec(gj), set(cues), target)
                pre, suf = text.split(cx.BLANK)
                k = {"bands1": 1, "bands2": 2}.get(cues[0], 4)
                for order in cx.ORDERS:
                    jobs.append({"species": sp, "delta": d, "pair": "+".join(cues) + ">" + target, "order": order,
                                 "prompt": fim_wrap(pre, "", suf, order), "gold_rec": g,
                                 "exact_ceiling": 1.0 / exact[(k, tuple(sorted(g["bands"][:k])))]})
    print(f"{len(groups['T'])} species x {len(DELTAS)} deltas x {len(PAIRS)} pairs x 2 orders = {len(jobs):,} prompts", flush=True)
    out = {"deltas": DELTAS, "models": {}}
    for m in args.models.split(","):
        gens = pc.generate(pp.ALL_MODELS[m], [j["prompt"] for j in jobs], "cuda:0", 24)
        tab = collections.defaultdict(list)
        for j, gt in zip(jobs, gens):
            tab[(j["pair"], j["delta"])].append(pc.score("name", pc.extract(gt, "xml", "name"), j["gold_rec"]) == "HIT")
        res = {f"{p}|{d}": round(sum(v) / len(v), 3) for (p, d), v in tab.items()}
        out["models"][m] = res
        print(f"\n{m}: HIT rate by jitter δ (cm-1)")
        print(f"  {'pair':22s} " + " ".join(f"δ={d:<3d}" for d in DELTAS))
        for cues, target in PAIRS:
            p = "+".join(cues) + ">" + target
            print(f"  {p:22s} " + " ".join(f"{res[f'{p}|{d}']:.2f} " for d in DELTAS))
    ex = collections.defaultdict(list)
    for j in jobs:
        if j["delta"] == 0 and j["order"] == "psm":
            ex[j["pair"]].append(j["exact_ceiling"])
    out["exact_ceiling"] = {p: round(sum(v) / len(v), 3) for p, v in ex.items()}
    print("\nexact-value ceiling (1 / species sharing the identical integers):", out["exact_ceiling"])
    json.dump(out, open(args.out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
