#!/usr/bin/env python3
"""Does bands → name survive measurement variation? (PROCEDURE §22h; rock venv)

Stripped records (only the cue fields; corpus_xml.stripped_record), two pairs — bands →
name and bands + lines → name (LIBS lines exact) — under four kinds of band values:

  exact      the canonical strongest four, as §22g trained them
  synthetic  FRESH instrument draws (a probe-only seed namespace, never trained), forced
             per class — lab / portable / handheld — jittered as synth_variance models and
             rounded to the class grid (1 / 5 / 10 cm-1), 4 draws per species and class
  real       a HELD-OUT real RRUFF / ROD re-measurement of the same species
             (real_remeasurements.json: its own strongest four), at native integers and
             rounded to 5 and 10 cm-1
and three renderings of the resolution field: none, correct (the grid actually used),
wrong (1 for coarse draws, 10 for lab draws) — so the probe shows whether the model reads
the field. PSM and SPM. Groups: T (the probe_pairs seeded 60), R (100 seeded trained
species that have a real re-measurement), V (the val species, exact only).

  ./.venv/bin/python probe_resolution.py --models v3g,v3r
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corpus_xml as cx  # noqa: E402
import probe_chains as pc  # noqa: E402
import probe_pairs as pp  # noqa: E402
import synth_variance as sv  # noqa: E402
from corpus_xml_resolution import GRID, quantize  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

REAL = os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2r/docs/real_remeasurements.json")
PAIRS = ((("bands",), "name"), (("bands", "lines"), "name"))
DRAWS = 4


def forced_draw(rec: cx.XmlRecord, klass: str, k: int) -> tuple[cx.XmlRecord, int]:
    """A fresh draw from one instrument class, as synth_variance.jitter_raman models it."""
    rng = sv.rng_for("probe-s2r", rec.species, klass, k)
    off = 0.0
    if klass != "lab" and rng.random() < sv.OUTLIER_SHARE:
        off = rng.choice((-1, 1)) * rng.uniform(4.0, 9.0)
    grid = GRID[klass]
    bands = [quantize(b + rng.gauss(0.0, sv.RAMAN_SIGMA[klass]) + off, grid) for b in rec.bands]
    return dataclasses.replace(rec, bands=bands), grid


def prompts_for(rec: cx.XmlRecord, grid: int, wrong: int) -> list[tuple[str, str, str]]:
    """[(pair, rendering, prompt)] for both pairs, three field renderings, both orders."""
    out = []
    for cues, target in PAIRS:
        for rendering, res in (("none", None), ("correct", grid), ("wrong", wrong)):
            if res is not None and wrong is None and rendering == "wrong":
                continue
            text = cx.stripped_record(rec, set(cues), target, raman_resolution=res)
            pre, suf = text.split(cx.BLANK)
            for order in cx.ORDERS:
                out.append(("+".join(cues) + ">" + target, rendering, fim_wrap(pre, "", suf, order)))
    return out


def build_jobs(groups: dict, real: dict) -> list[dict]:
    jobs = []

    def add(species, group, condition, rec, grid, wrong, extra=None):
        g = pc.load_gold(species)
        for pair, rendering, prompt in prompts_for(rec, grid, wrong):
            jobs.append({"species": species, "group": group, "condition": condition, "pair": pair, "rendering": rendering,
                         "prompt": prompt, "gold_rec": g, **(extra or {})})

    for grp in ("T", "V"):
        for sp in groups[grp]:
            g = pc.load_gold(sp)
            rec = pp._rec(g)
            add(sp, grp, "exact", rec, 1, 10)
            if grp == "T":
                for klass in ("lab", "portable", "handheld"):
                    for k in range(DRAWS):
                        jrec, grid = forced_draw(rec, klass, k)
                        add(sp, grp, f"synthetic·{klass}", jrec, grid, 10 if klass == "lab" else 1)
    for sp in groups["R"]:
        m = real[sp]
        g = pc.load_gold(sp)
        base = dataclasses.replace(pp._rec(g), bands=list(m["bands"]))
        extra = {"set_match": m["set_matches_canonical_5"], "top_match": m["top_matches_canonical_5"]}
        add(sp, "R", "real·native", base, 1, 10, extra)
        for grid in (5, 10):
            add(sp, "R", f"real·grid{grid}", dataclasses.replace(base, bands=[quantize(b, grid) for b in base.bands]), grid, 1, extra)
    return jobs


def summarise(results: list[dict]) -> dict:
    tab = collections.defaultdict(list)
    for r in results:
        hit = r["status"] == "HIT"
        tab[(r["model"], r["group"], r["condition"], r["pair"], r["rendering"])].append(hit)
        if r["group"] == "R":
            sub = "set-match" if r.get("set_match") else ("top-match" if r.get("top_match") else "neither")
            tab[(r["model"], f"R:{sub}", r["condition"], r["pair"], r["rendering"])].append(hit)
    return {"|".join(k): (round(sum(v) / len(v), 3), len(v)) for k, v in tab.items()}


def write_report(summary: dict, models: list[str], groups: dict, path: str) -> None:
    L = ["# Resolution probe: does bands → name survive measurement variation?\n",
         f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} by `dev/rock_olmo/probe_resolution.py`. "
         "HIT rate, PSM and SPM pooled. Rendering = the resolution field: none / correct (the grid actually used) / wrong.\n"]
    def cell(m, g, c, p, r):
        v = summary.get(f"{m}|{g}|{c}|{p}|{r}")
        return f"{v[0]:.2f}" if v else "—"
    for p in ("bands>name", "bands+lines>name"):
        L.append(f"\n## {p.replace('>', ' → ')}\n")
        L.append("| group | condition | " + " | ".join(f"{m} none | {m} correct | {m} wrong" for m in models) + " |")
        L.append("|---|---|" + "---|---|---|" * len(models))
        rows = [("T", "exact"), ("T", "synthetic·lab"), ("T", "synthetic·portable"), ("T", "synthetic·handheld"),
                ("R", "real·native"), ("R", "real·grid5"), ("R", "real·grid10"),
                ("R:set-match", "real·native"), ("R:set-match", "real·grid5"), ("R:set-match", "real·grid10"),
                ("R:top-match", "real·grid10"), ("R:neither", "real·grid10"), ("V", "exact")]
        for g, c in rows:
            L.append(f"| {g} | {c} | " + " | ".join(f"{cell(m, g, c, p, 'none')} | {cell(m, g, c, p, 'correct')} | {cell(m, g, c, p, 'wrong')}" for m in models) + " |")
    n = {k: sum(1 for s in groups[k]) for k in groups}
    L.append(f"\nGroups: T {n['T']}, R {n['R']} (real re-measurements), V {n['V']}.")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"-> {path}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="v3g")
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--n-real", type=int, default=100)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/v3/pair_probe"))
    args = ap.parse_args()
    groups = pp.sample_species({"T": 60, "V": 19, "U": 0}, args.seed)
    real = json.load(open(REAL))
    val = set(groups["V"])
    trained_names = {r["species"] for r in json.load(open(pc.RECORDS))["trained"]}
    cands = sorted(s for s in real if s in trained_names and s not in val)
    random.Random(args.seed).shuffle(cands)
    groups["R"] = cands[: args.n_real]
    jobs = build_jobs(groups, real)
    os.makedirs(args.out, exist_ok=True)
    json.dump({k: v for k, v in groups.items()}, open(os.path.join(args.out, "resolution_items_frozen.json"), "w"), indent=1)
    print(f"groups T {len(groups['T'])} R {len(groups['R'])} V {len(groups['V'])} -> {len(jobs):,} prompts per model", flush=True)
    rpath = os.path.join(args.out, "resolution_results.jsonl")
    results, done = [], set()
    if os.path.exists(rpath):
        for line in open(rpath):
            r = json.loads(line)
            results.append(r)
            done.add(r["model"])
    for m in args.models.split(","):
        if m in done:
            print(f"{m}: cached", flush=True)
            continue
        t0 = time.time()
        gens = pc.generate(pp.ALL_MODELS[m] if m in pp.ALL_MODELS else os.path.expanduser(m), [j["prompt"] for j in jobs], "cuda:0", 24)
        with open(rpath, "a", encoding="utf-8") as fh:
            for j, gt in zip(jobs, gens):
                ans = pc.extract(gt, "xml", "name")
                row = {k: v for k, v in j.items() if k not in ("gold_rec", "prompt")}
                row.update(model=m, answer=ans[:60], status=pc.score("name", ans, j["gold_rec"]))
                results.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{m}: {len(jobs):,} prompts in {time.time() - t0:.0f}s", flush=True)
    summary = summarise(results)
    json.dump(summary, open(os.path.join(args.out, "resolution_summary.json"), "w"), indent=1)
    models = [m for m in ("v3g", "v3r") if any(r["model"] == m for r in results)]
    write_report(summary, models, groups, os.path.join(args.out, "resolution_report.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
