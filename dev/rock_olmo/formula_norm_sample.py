#!/usr/bin/env python3
"""Show the normaliser at work on seeded random samples from EVERY corpus source.

Root venv (reference_layer). Prints before → after per source, then the statistics
that matter: fraction changed, residual markup after normalisation, and the
cross-source agreement for species shared by ≥ 3 sources (distinct spellings per
species before/after, and after the species-formula lookup).

    ../../.venv/bin/python formula_norm_sample.py [--n 8] [--seed 1]
"""

from __future__ import annotations

import argparse
import collections
import glob
import itertools
import json
import os
import random
import re
import statistics as st
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference_layer as rl  # noqa: E402
from formula_norm import normalize_formula, normalize_text_formulas, residual_markup, species_formula  # noqa: E402


def sources() -> dict[str, list[tuple[str, str]]]:
    """source -> [(species_or_context, raw_formula)]"""
    out: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for r in rl.iter_rruff("excellent_unoriented.zip"):
        if r.get("ideal_formula"):
            out["rruff_ideal"].append((r["species"], r["ideal_formula"]))
        if r.get("measured_formula"):
            out["rruff_measured"].append((r["species"], r["measured_formula"]))
    for r in rl.iter_rod():
        if r.get("formula"):
            out["rod"].append((r["species"], r["formula"]))
    p = os.path.expanduser("~/corpora/mineral-refs/mindat/geomaterials.jsonl")
    for line in itertools.islice(open(p, errors="replace"), 200000):
        try:
            d = json.loads(line)
        except Exception:
            continue
        f = d.get("ima_formula") or d.get("mindat_formula") or ""
        if f and d.get("name"):
            out["mindat"].append((d["name"], f))
    for line in open(os.path.expanduser("~/corpora/rock-olmo-training/v4/stage1/docs/webmineral.jsonl")):
        d = json.loads(line)
        m = re.search(r"Chemical Formula:\s*([^\n]+)", d["text"])
        if m:
            out["webmineral"].append((d["doc_id"].split(":", 1)[1], m.group(1).strip()))
    for line in open(os.path.expanduser("~/corpora/rock-olmo-training/v4/stage1/docs/hom.jsonl")):
        d = json.loads(line)
        first = d["text"].split("\n", 1)[0].split(" ", 1)
        if len(first) > 1:
            out["hom"].append((first[0], first[1].strip()))
    for f in glob.glob(os.path.expanduser("~/corpora/ouroboros-spectra/databank/dataset/*.json")):
        if any(x in f for x in ("registry", "aliases", "quarantine")):
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for v in (d.get("data") or {}).get("composition_formula") or []:
            out["packs"].append((d["paper_key"][:28], str(v)))
    return out


def paper_spans(n: int, rng: random.Random) -> list[tuple[str, str]]:
    """Decorated formula spans from random paper paragraphs (context, span)."""
    docs = [json.loads(l) for l in open(os.path.expanduser("~/corpora/rock-olmo-training/v4/stage1/docs/papers.jsonl"))]
    rng.shuffle(docs)
    pat = re.compile(r"[^\n.]{0,60}(?:[A-Z][a-z]?(?:_\{\d+\}|\$_\{?\d+\}?\$|_\d|[₀-₉]|<sub>\d+</sub>)[^\n.]{0,60})")
    out = []
    for d in docs:
        for m in pat.finditer(d["text"]):
            out.append((d["doc_id"][6:30], m.group(0).strip()))
            if len(out) >= n * 40:
                break
        if len(out) >= n * 40:
            break
    rng.shuffle(out)
    return out[:n]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    src = sources()
    print("=" * 100)
    stats = {}
    for name, rows in src.items():
        sample = rng.sample(rows, min(args.n, len(rows)))
        print(f"\n--- {name} ({len(rows):,} formulas) ---")
        for ctx, raw in sample:
            new = normalize_formula(raw)
            flag = "" if new != raw else "   (unchanged)"
            print(f"  {ctx[:22]:22} {raw[:46]!r:48} -> {new[:40]!r}{flag}")
        changed = sum(1 for _, raw in rows if normalize_formula(raw) != raw)
        resid = collections.Counter(t for _, raw in rows for t in residual_markup(normalize_formula(raw)))
        before = collections.Counter(t for _, raw in rows for t in residual_markup(raw))
        stats[name] = (len(rows), changed, dict(before), dict(resid))
    print("\n--- paper markdown: decorated spans in context ---")
    for ctx, span in paper_spans(args.n, rng):
        print(f"  {ctx:24} {span[:70]!r}\n  {'':24} -> {normalize_text_formulas(span)[:70]!r}")
    print("\n" + "=" * 100 + "\nSTATISTICS")
    print(f"{'source':16} {'formulas':>9} {'changed':>8}  markup before -> after normalisation")
    for name, (n, ch, before, resid) in stats.items():
        print(f"{name:16} {n:9,} {100*ch/max(1,n):7.1f}%  {dict(sorted(before.items()))} -> {dict(sorted(resid.items())) or 'none'}")
    # cross-source agreement
    by = collections.defaultdict(lambda: collections.defaultdict(set))
    for name in ("rruff_ideal", "mindat", "webmineral", "hom", "rod"):
        for sp, raw in src[name]:
            by[sp.lower()][name].add(raw)
    shared = [s for s, d in by.items() if len(d) >= 3]
    raw_n = [len({f for fs in d.values() for f in fs}) for s, d in by.items() if s in set(shared)]
    norm_n = [len({normalize_formula(f) for fs in by[s].values() for f in fs}) for s in shared]
    look_n = []
    for s in shared:
        canon = species_formula(s)
        forms = {canon} if canon else {normalize_formula(f) for fs in by[s].values() for f in fs}
        look_n.append(len(forms))
    print(f"\nspecies shared by >=3 sources: {len(shared):,}")
    print(f"  distinct spellings/species  raw {st.mean(raw_n):.2f}  |  string-normalised {st.mean(norm_n):.2f} (=1 for {100*sum(x==1 for x in norm_n)/len(norm_n):.0f}%)  |  species lookup {st.mean(look_n):.2f} (=1 for {100*sum(x==1 for x in look_n)/len(look_n):.0f}%)")
    miss = sum(1 for s in shared if not species_formula(s))
    print(f"  shared species with NO table entry: {miss}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
