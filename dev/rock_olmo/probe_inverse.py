#!/usr/bin/env python3
"""Inverse-identification probe with its kNN control (rock venv, GPU if present).

Items: docs/probe_items.json from corpus_inverse.py — held-out RRUFF spectra
(one per species, in-distribution instrument) and ALL of ROD (unseen instrument
family), each with the seeker's base peak list and the true species.

Control: nearest-neighbour peak matching against docs/knn_library.json (the
TRAIN spectra's peak lists). Score(query, library spectrum) = mean over the
query's 6 strongest peaks of [a library peak lies within ±10 cm⁻¹], symmetrised
with the library spectrum's 6 strongest; a species' score is its best spectrum.
If the language model cannot beat this on library spectra it is adding nothing
— the rule from §18: never quote a headline without its control.

Model: the identify prompt (same schema as training), greedy decode; top-1 =
the true species is the FIRST phase named; top-3 = it appears anywhere in the
completion (candidate list). Also reports how many distinct first answers the
model gave — the collapse signature ("Pyrope" for everything) shows here.

    ./.venv/bin/python probe_inverse.py --corpus ~/corpora/rock-olmo-training/v4/inverse_exp \
        --models s1=~/models/olmo2-1b-spectra-full/stage1_final,inv=~/models/olmo2-1b-spectra-full/inverse_exp/final
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from corpus_inverse import identify_prompt  # noqa: E402

TOL = 10.0


def top_positions(peaks, n=6):
    return [p["position_cm-1"] for p in sorted(peaks, key=lambda p: -p["relative_intensity"])[:n]]


def knn(items, library):
    """Vectorised nearest-neighbour peak matching (numpy): symmetric fraction of the six
    strongest peaks matched within ±TOL; species score = best of its library spectra."""
    import numpy as np
    names, mats = [], []
    for sp, specs in library.items():
        for s in specs:
            pos = top_positions(s["peaks"])
            if not pos:
                continue
            row = pos + [float("nan")] * (6 - len(pos))
            names.append(sp)
            mats.append(row)
    L = np.array(mats, dtype=float)              # (N, 6) with NaN padding
    names = np.array(names)
    hits1 = hits3 = 0
    for it in items:
        q = np.array(top_positions(it["peaks"]) + [float("nan")] * 6, dtype=float)[:6]
        d = np.abs(q[None, :, None] - L[:, None, :])          # (N, 6q, 6l)
        qm = ~np.isnan(q)
        lm = ~np.isnan(L)                                        # (N, 6)
        hit = d <= TOL
        f = np.where(qm[None, :], np.any(hit & lm[:, None, :], axis=2), False).sum(1) / max(1, qm.sum())
        g = (np.any(hit & qm[None, :, None], axis=1) & lm).sum(1) / np.maximum(1, lm.sum(1))
        score = 0.5 * (f + g)
        best = {}
        for sp, sc in zip(names, score):
            if sc > best.get(sp, -1):
                best[sp] = sc
        ranked = sorted(best, key=lambda s: -best[s])
        hits1 += ranked[0] == it["species"]
        hits3 += it["species"] in ranked[:3]
    return hits1 / len(items), hits3 / len(items)


def first_phase(text: str) -> str:
    t = text.strip().split("\n")[0]
    t = re.split(r"[;(]", t)[0]
    return t.strip(" .,").lower()


def run_model(name, path, items, max_new, device):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(os.path.expanduser("~/models/OLMo-2-0425-1B"))
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).to(device).eval()
    out = []
    for it in items:
        prompt = identify_prompt(it["peaks"], it["laser"])
        enc = tok(prompt, return_tensors="pt").to(device)
        with torch.no_grad():
            g = model.generate(**enc, max_new_tokens=max_new, do_sample=False, pad_token_id=tok.pad_token_id)
        text = tok.decode(g[0, enc["input_ids"].shape[1]:], skip_special_tokens=True)
        out.append(text)
    del model
    if device == "cuda":
        torch.cuda.empty_cache()
    return out


def score_model(items, gens):
    h1 = h3 = 0
    firsts = collections.Counter()
    for it, g in zip(items, gens):
        sp = it["species"].lower()
        fp = first_phase(g)
        firsts[fp] += 1
        h1 += fp == sp
        h3 += sp in g.lower()
    return h1 / len(items), h3 / len(items), len(firsts), firsts.most_common(3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--models", default="", help="name=DIR[,name=DIR...]")
    ap.add_argument("--max-new", type=int, default=48)
    ap.add_argument("--n", type=int, default=0, help="cap items per split (dev)")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    docs = os.path.join(os.path.expanduser(args.corpus), "docs")
    items = json.load(open(os.path.join(docs, "probe_items.json")))
    library = json.load(open(os.path.join(docs, "knn_library.json")))
    splits = {k: (v[: args.n] if args.n else v) for k, v in items.items()}
    train_species = set(library)
    res = {"knn": {}, "models": {}}
    t0 = time.time()
    for name, its in splits.items():
        seen = [i for i in its if i["species"] in train_species]
        a1, a3 = knn(its, library)
        res["knn"][name] = {"n": len(its), "n_species_seen": len(seen), "top1": round(a1, 3), "top3": round(a3, 3)}
        print(f"kNN control  {name:8} n={len(its):4} (species in train: {len(seen)})  top1 {a1:.3f}  top3 {a3:.3f}  [{time.time()-t0:.0f}s]", flush=True)
    if args.models:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        for spec in args.models.split(","):
            name, path = spec.split("=", 1)
            res["models"][name] = {}
            for split, its in splits.items():
                gens = run_model(name, os.path.expanduser(path), its, args.max_new, device)
                h1, h3, distinct, common = score_model(its, gens)
                res["models"][name][split] = {"top1": round(h1, 3), "top3": round(h3, 3), "distinct_first_answers": distinct,
                                              "most_common_first": common, "samples": [(its[i]["species"], gens[i][:80]) for i in range(min(5, len(its)))]}
                print(f"{name:12} {split:8} n={len(its):4}  top1 {h1:.3f}  top3 {h3:.3f}  distinct first answers {distinct}  most common {common[:2]}  [{time.time()-t0:.0f}s]", flush=True)
                for sp_, g in res["models"][name][split]["samples"][:3]:
                    print(f"      {sp_:20} -> {g!r}")
    if args.out:
        json.dump(res, open(os.path.expanduser(args.out), "w"), indent=1)
        print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
