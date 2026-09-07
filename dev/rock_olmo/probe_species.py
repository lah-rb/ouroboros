#!/usr/bin/env python3
"""The generalisation probe set for corpus v4 (root venv).

WHAT REPLACES THE SPECIES HOLDOUT. Operator ruling (2026-09-07): everything
trains; the model is judged on what it RECALLS about species it saw and on
how it handles real but obscure systems. The obscure systems are RRUFF
species that appear in NO paper of the corpus — reference-only species —
so withholding them costs zero paper text (the old holdout cost 446 papers,
14.4M tokens). About a hundred of them, each with >=2 excellent spectra so a
measurement-noise ceiling exists for the head instrument, chosen for
mineral-class breadth the same way the assembler ranks reference-only
species, then evenly spaced down that ranking so no single class dominates.

TWO PHASES, because the check needs the corpus. `select` writes the
candidate set. `verify --docs <stage1 docs dir>` scans every rendered
stage-1 document for the names (word-boundary match, emit.species_mentioned)
and SWAPS OUT any species a paper, figtext, HOM sheet or webmineral page
happens to name — never drops a document. The final list is what every
instrument reads (probe_polymorph, probe_recall, train_spectra_head
--holdout-file), and what assemble()/facts exclude from every view.

  ../../.venv/bin/python probe_species.py select              # -> probe_species.json
  ../../.venv/bin/python probe_species.py verify --docs ~/corpora/rock-olmo-training/v4/stage1/docs
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os

from assemble import _implied_class, load_ima
from emit import species_mentioned

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "probe_species.json")
SPECTRA = os.path.expanduser("~/corpora/rock-olmo-training/spectra_binned.json")
SEED = 20260824
TARGET = 100
MIN_SPECTRA = 2


def candidates() -> list[str]:
    """Reference-only RRUFF species with >= MIN_SPECTRA binned spectra."""
    data = json.load(open(SPECTRA))
    paper_species = set(
        json.load(open(os.path.join(HERE, "species_papers.json")))["papers"]
    )
    from holdout import AMBIGUOUS_NAMES

    ima = load_ima()
    out = []
    for name, rec in data.items():
        # IMA species only: RRUFF also carries organics and synthetics
        # ("Alpha-D lactose monohydrate"), which are not mineral systems.
        if name not in ima or name in paper_species or name.lower() in AMBIGUOUS_NAMES:
            continue
        if len(rec.get("spectra") or []) < MIN_SPECTRA:
            continue
        out.append(name)
    return sorted(out)


def rank_for_breadth(names: list[str], ima: dict[str, str]) -> list[str]:
    """Rarest implied class first, then name — the assembler's own ordering."""
    classes = collections.Counter(_implied_class(ima.get(n, "")) for n in names)
    return [
        n
        for _, _, n in sorted(
            (classes[_implied_class(ima.get(n, ""))], _implied_class(ima.get(n, "")), n)
            for n in names
        )
    ]


def spaced(ranked: list[str], n: int) -> list[str]:
    """Every k-th species down the breadth ranking: breadth without a
    single rare class monopolising the set."""
    if len(ranked) <= n:
        return list(ranked)
    step = len(ranked) / n
    return sorted({ranked[int(i * step)] for i in range(n)})


def select() -> dict:
    ima = load_ima()
    cands = candidates()
    ranked = rank_for_breadth(cands, ima)
    chosen = spaced(ranked, TARGET)
    reserve = [n for n in ranked if n not in set(chosen)]
    return {
        "species": chosen,
        "reserve": reserve,
        "criteria": {
            "reference_only": True,
            "min_spectra": MIN_SPECTRA,
            "target": TARGET,
            "candidates": len(cands),
            "seed": SEED,
        },
        "verified_against": None,
    }


def verify(doc_dirs: list[str], probe: dict) -> dict:
    """Swap out any probe species that the rendered stage-1 text names."""
    names = {s.lower(): s for s in probe["species"]}
    reserve = list(probe["reserve"])
    hits: collections.Counter = collections.Counter()
    files = [f for d in doc_dirs for f in sorted(glob.glob(os.path.join(d, "*.jsonl")))]
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    text = json.loads(line).get("text") or ""
                except Exception:  # noqa: BLE001
                    continue
                for s in species_mentioned(text, names):
                    hits[s] += 1
    swapped = []
    kept = [s for s in probe["species"] if s not in hits]
    # reserve species must ALSO be unmentioned; check the reserve lazily
    reserve_names = {s.lower(): s for s in reserve}
    reserve_hits: set[str] = set()
    if len(kept) < len(probe["species"]):
        for f in files:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        text = json.loads(line).get("text") or ""
                    except Exception:  # noqa: BLE001
                        continue
                    reserve_hits.update(species_mentioned(text, reserve_names))
    for s in probe["species"]:
        if s in hits:
            repl = next(
                (
                    r
                    for r in reserve
                    if r not in reserve_hits and r not in kept and r not in swapped
                ),
                None,
            )
            if repl is None:
                break
            swapped.append(repl)
            kept.append(repl)
    kept = sorted(set(kept))
    return {
        **probe,
        "species": kept,
        "reserve": [r for r in reserve if r not in kept],
        "verified_against": doc_dirs,
        "swapped_out": {s: hits[s] for s in hits},
        "swapped_in": swapped,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("select", "verify"))
    ap.add_argument("--docs", nargs="*", default=[])
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    if args.cmd == "select":
        probe = select()
    else:
        probe = verify(args.docs, json.load(open(args.out)))
    json.dump(probe, open(args.out, "w"), indent=1)
    print(
        f"{len(probe['species'])} probe species -> {args.out} (candidates {probe['criteria']['candidates']}, reserve {len(probe['reserve'])})"
    )
    if probe.get("swapped_out"):
        print("swapped out (named in stage-1 text):", probe["swapped_out"])
    classes = collections.Counter(
        _implied_class(load_ima().get(n, "")) for n in probe["species"]
    )
    print("class mix:", dict(classes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
