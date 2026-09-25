#!/usr/bin/env python3
"""Aggregate cue → target probe over ~100 species (PROCEDURE §22g; rock venv).

The same controlled design as probe_chains.py (each prompt shows ONLY its cue fields;
cues that were earlier chain targets appear as gold Q/A turns), run over every
(cue set, target) pair of corpus_xml_granular.PAIRS instead of three minerals. XML prompts
come from corpus_xml.stripped_record — the builder the granular stage 2 trained on — so
train and probe records are byte-identical where a pair is trained.

Groups: T = seeded trained species (records in both stage-2 arms), V = the §22 val species
(never in either arm's XML training), U = seeded untouched probe species (never rendered).
Formats: prose; xml (species-first, raman-first; PSM and SPM, averaged); xml·held-out
(formula-first attribute order — never trained by either arm), where both attributes
appear. Every rate is reported beside its CEILING: per item 1 / (number of distinct target
values among all 1,785 records matching the cues — bands ±10 cm-1 as a set of the k
strongest, lines ±0.2 nm, formula exact), averaged over the group.

  ./.venv/bin/python probe_pairs.py --models v2,v3,v3g --sample T=60,V=19,U=21
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np  # noqa: E402

import corpus_xml as cx  # noqa: E402
import probe_chains as pc  # noqa: E402
from corpus_xml_granular import PAIRS, TRAIN_PAIRS, pair_kind  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

M = os.path.expanduser("~/models/olmo2-1b-spectra-full")
ALL_MODELS = {"v2": f"{M}/stage2/final", "v3": f"{M}/v3_stage2/final", "v3g": f"{M}/v3_stage2g/final", "v3r": f"{M}/v3_stage2r/final", "v3d": f"{M}/v3_stage2d/final", "v3b": f"{M}/v3_stage2b/final", "v3k": f"{M}/v3_stage2k/final"}
SPECTRAL = ("bands", "bands1", "bands2", "bands3", "lines")
_WORD = {1: "strongest Raman band is", 2: "two strongest Raman bands are", 3: "three strongest Raman bands are"}


def opening(g: dict, cue: str, first: bool) -> str:
    if cue in ("bands1", "bands2", "bands3"):
        k = int(cue[-1])
        vals = g["bands"][:k]
        body = str(vals[0]) if k == 1 else ", ".join(map(str, vals[:-1])) + f" and {vals[-1]}"
        return f"A mineral's {_WORD[k]} at {body} cm-1" + ("." if k == 1 else ", strongest first.")
    if cue == "lines" and not first:
        return pc.FOLLOW_ON["lines"](g)
    return pc.OPENING[cue](g)


def prose_prompt(g: dict, cues: tuple[str, ...], target: str) -> str:
    spectral = [c for c in cues if c in SPECTRAL]
    rest = [c for c in cues if c not in SPECTRAL]
    given = spectral if spectral else rest[:1]
    asked = rest if spectral else rest[1:]
    lines = [" ".join(opening(g, c, i == 0) for i, c in enumerate(given))]
    for t in asked:
        lines += [f"Q: {pc.QUESTION[t]}", f"A: {pc.gold_answer(g, t)}"]
    lines += [f"Q: {pc.QUESTION[target]}", "A:"]
    return "\n".join(lines)


def _rec(g: dict) -> cx.XmlRecord:
    return cx.XmlRecord(g["name"], g["formula"], "", g["bands"], "", g["lines"])


def xml_prompts(g: dict, cues: tuple[str, ...], target: str) -> list[tuple[str, str]]:
    """[(format label, prompt)]: trained order (psm, spm) and, when both attributes are in
    the record, the held-out formula-first order (psm, spm)."""
    out = []
    both_attrs = sum(1 for k in ("name", "formula") if k in cues or k == target) == 2
    for ff in ((False, True) if both_attrs else (False,)):
        text = cx.stripped_record(_rec(g), set(cues), target, formula_first=ff)
        pre, suf = text.split(cx.BLANK)
        for order in cx.ORDERS:
            out.append((("xml·held-out·" if ff else "xml·") + order, fim_wrap(pre, "", suf, order)))
    return out


# ── ceilings ─────────────────────────────────────────────────────────
class Ceilings:
    def __init__(self, recs: list[dict]):
        self.names = [r["species"] for r in recs]
        self.idx = {n: i for i, n in enumerate(self.names)}
        self.formula = [r["formula"] for r in recs]
        B = np.array([r["bands"] for r in recs], float)  # strongest first
        L = np.array([sorted(r["libs"]) for r in recs], float)
        self.m = {}
        for k in (1, 2, 3, 4):
            S = np.sort(B[:, :k], axis=1)
            self.m[f"bands{k}"] = np.all(np.abs(S[:, None, :] - S[None, :, :]) <= 10.0, axis=2)
        self.m["bands"] = self.m.pop("bands4")
        self.m["lines"] = np.all(np.abs(L[:, None, :] - L[None, :, :]) <= 0.2, axis=2)
        F = np.array(self.formula)
        self.m["formula"] = F[:, None] == F[None, :]
        n = len(recs)
        self.m["name"] = np.eye(n, dtype=bool)

    def ceiling(self, species: str, cues: tuple[str, ...], target: str) -> float:
        if target in ("top", "line"):
            return 1.0
        i = self.idx[species]
        keep = np.ones(len(self.names), dtype=bool)
        for c in cues:
            if c in self.m:
                keep &= self.m[c][i]
        vals = {self.names[j] if target == "name" else self.formula[j] for j in np.where(keep)[0]}
        return 1.0 / max(1, len(vals))


def sample_species(spec: dict[str, int], seed: int) -> dict[str, list[str]]:
    recs = json.load(open(pc.RECORDS))
    man = json.load(open(os.path.join(os.path.dirname(os.path.dirname(pc.RECORDS)), "docs_manifest.json")))
    val = set(man["species_val"])
    T = sorted(r["species"] for r in recs["trained"] if r["species"] not in val)
    U = sorted(r["species"] for r in recs["untouched"])
    rng = random.Random(seed)
    rng.shuffle(T)
    rng.shuffle(U)
    return {"T": T[: spec.get("T", 60)], "V": sorted(val)[: spec.get("V", len(val))], "U": U[: spec.get("U", 21)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="v2,v3", help=f"subset of {list(ALL_MODELS)}")
    ap.add_argument("--sample", default="T=60,V=19,U=21")
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--max-new", type=int, default=40)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/v3/pair_probe"))
    args = ap.parse_args()
    groups = sample_species({k: int(v) for k, v in (kv.split("=") for kv in args.sample.split(","))}, args.seed)
    recs = json.load(open(pc.RECORDS))
    ceil = Ceilings(recs["trained"] + recs["untouched"])
    jobs = []
    for grp, names in groups.items():
        for sp in names:
            g = pc.load_gold(sp)
            for cues, target in PAIRS:
                base = {"group": grp, "species": sp, "pair": pair_kind(cues, target), "cues": list(cues), "target": target,
                        "trained_in_g": (cues, target) in TRAIN_PAIRS, "gold": pc.gold_answer(g, target),
                        "ceiling": ceil.ceiling(sp, cues, target), "gold_rec": g}
                jobs.append({**base, "fmt": "prose", "prompt": prose_prompt(g, cues, target)})
                for label, p in xml_prompts(g, cues, target):
                    jobs.append({**base, "fmt": label, "prompt": p})
    os.makedirs(args.out, exist_ok=True)
    frozen = {"groups": groups, "pairs": [pair_kind(c, t) for c, t in PAIRS], "n_prompts": len(jobs)}
    json.dump(frozen, open(os.path.join(args.out, "items_frozen.json"), "w"), indent=1)
    print(f"{sum(len(v) for v in groups.values())} species {dict((k, len(v)) for k, v in groups.items())} x {len(PAIRS)} pairs -> {len(jobs):,} prompts per model", flush=True)
    results = []
    rpath = os.path.join(args.out, "pair_results.jsonl")
    done_models = set()
    if os.path.exists(rpath):
        for line in open(rpath):
            r = json.loads(line)
            results.append(r)
            done_models.add(r["model"])
    for m in args.models.split(","):
        if m in done_models:
            print(f"{m}: already in {rpath}, reusing", flush=True)
            continue
        t0 = time.time()
        gens = pc.generate(ALL_MODELS[m], [j["prompt"] for j in jobs], args.device, args.max_new)
        with open(rpath, "a", encoding="utf-8") as fh:
            for j, gt in zip(jobs, gens):
                kind = "xml" if j["fmt"].startswith("xml") else "prose"
                ans = pc.extract(gt, kind, j["target"])
                row = {k: v for k, v in j.items() if k not in ("gold_rec", "prompt")}
                row.update(model=m, response=gt[:120], answer=ans[:80], status=pc.score(j["target"], ans, j["gold_rec"]))
                results.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{m}: {len(jobs):,} prompts in {time.time() - t0:.0f}s", flush=True)
    write_report(results, groups, os.path.join(args.out, "pair_report.md"))
    return 0


def _rate(rows: list[dict]) -> str:
    return f"{sum(r['status'] == 'HIT' for r in rows) / len(rows):.2f}" if rows else "—"


def write_report(results: list[dict], groups: dict, path: str) -> None:
    models = [m for m in ALL_MODELS if any(r["model"] == m for r in results)]
    by = collections.defaultdict(list)
    for r in results:
        fam = "prose" if r["fmt"] == "prose" else ("xml·held-out" if "held-out" in r["fmt"] else "xml")
        by[(r["model"], r["group"], r["pair"], fam)].append(r)
    ceil = collections.defaultdict(list)
    seen = set()
    for r in results:
        k = (r["group"], r["pair"], r["species"])
        if k not in seen:
            seen.add(k)
            ceil[(r["group"], r["pair"])].append(r["ceiling"])
    L = [f"# Cue → target probe, {sum(len(v) for v in groups.values())} species\n",
         f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} by `dev/rock_olmo/probe_pairs.py`. "
         "Each prompt shows only its cue fields (no crystal system, no laser). HIT rate per cell; "
         "xml = trained attribute order, PSM and SPM averaged; held-out = formula-first order, never trained. "
         "Ceiling = mean over the group of 1 / (distinct answers among all records matching the cues). "
         "★ = a pair the granular arm (v3g) trained, 8 exposures per species; the other pairs are transfer for every model.\n",
         f"Groups: T {len(groups['T'])} trained species, V {len(groups['V'])} val species (never in either arm's XML training), "
         f"U {len(groups['U'])} untouched species (never rendered).\n"]
    trained = {r["pair"] for r in results if r.get("trained_in_g")}
    for grp in ("T", "V", "U"):
        L.append(f"\n## Group {grp}\n")
        hdr = "| pair | ceiling | " + " | ".join(f"{m} prose | {m} xml | {m} held-out" for m in models) + " |"
        L.append(hdr)
        L.append("|---|---|" + "---|---|---|" * len(models))
        for c, t in PAIRS:
            p = pair_kind(c, t)
            cs = ceil[(grp, p)]
            row = f"| {'★ ' if p in trained else ''}{' + '.join(c)} → {t} | {np.mean(cs):.2f} | "
            row += " | ".join(f"{_rate(by[(m, grp, p, 'prose')])} | {_rate(by[(m, grp, p, 'xml')])} | {_rate(by[(m, grp, p, 'xml·held-out')])}" for m in models)
            L.append(row + " |")
    L.append("\n## Examples (first T species, trained-order XML, PSM)\n")
    first = groups["T"][0]
    for c, t in PAIRS:
        p = pair_kind(c, t)
        rows = [r for r in results if r["species"] == first and r["pair"] == p and r["fmt"] == "xml·psm"]
        if not rows:
            continue
        L.append(f"**{' + '.join(c)} → {t}** ({first}, gold `{rows[0]['gold']}`): " +
                 " · ".join(f"{r['model']} `{pc._cell(r['answer'], 40)}` {pc.MARK[r['status']]}" for r in sorted(rows, key=lambda r: models.index(r["model"]))))
        L.append("")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"-> {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
