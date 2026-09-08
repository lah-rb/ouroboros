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

TWO PHASES, because the ranking needs the corpus. `select` writes the
candidate set. `verify --docs <stage1 docs dir>` counts how many rendered
documents name each candidate and keeps the LEAST-EXPOSED hundred. Nothing
is withheld from training (operator ruling 2026-09-07: include everything;
judge on real but obscure systems): the probe species are in the corpus
through their own reference frames and species sheets, and the probe
measures recall at the low end of exposure. The final list is what every
instrument reads (probe_polymorph, probe_recall, train_spectra_head
--holdout-file); facts.build_facts is called with NO exclusion.

  ../../.venv/bin/python probe_species.py select              # -> probe_species.json
  ../../.venv/bin/python probe_species.py verify --docs ~/corpora/rock-olmo-training/v4/stage1/docs
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

from assemble import _implied_class, load_ima

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


def _mention_counts(doc_dirs: list[str], names: list[str]) -> collections.Counter:
    """How many stage-1 documents name each species (word-boundary match).
    One alternation regex per pass: the per-name loop took 45 minutes over
    the v4 docs (2026-09-07); this takes about a minute."""
    rx = re.compile(
        r"(?<![a-z])("
        + "|".join(re.escape(n.lower()) for n in sorted(names, key=len, reverse=True))
        + r")(?![a-z])"
    )
    canon = {n.lower(): n for n in names}
    hits: collections.Counter = collections.Counter()
    files = [f for d in doc_dirs for f in sorted(glob.glob(os.path.join(d, "*.jsonl")))]
    for f in files:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    text = json.loads(line).get("text") or ""
                except Exception:  # noqa: BLE001
                    continue
                for m in set(rx.findall(text.lower())):
                    hits[canon[m]] += 1
    return hits


def _exposure_summary(chosen: list[str], hits: collections.Counter) -> dict:
    """min/median/max over the CHOSEN set. `chosen` is sorted by NAME for the
    file, so an index into it is not an exposure rank -- read the counts."""
    if not chosen:
        return {}
    counts = sorted(hits.get(n, 0) for n in chosen)
    return {"min": counts[0], "median": counts[len(counts) // 2], "max": counts[-1],
            "next_out": None}


def verify(doc_dirs: list[str], probe: dict) -> dict:
    """Rank candidates by EXPOSURE in the rendered stage-1 text and keep the
    least-exposed `target`.

    WHY EXPOSURE, NOT ABSENCE. Operator ruling (2026-09-07): everything
    trains, and the model is judged on real but obscure systems. With the
    encyclopaedic sources in the mix (webmineral and HOM name related
    species in their classification and association lines; mindat's prose
    too), every IMA species is named somewhere — the first version of this
    step swapped out all 100 candidates and all 1,014 reserves and left an
    empty set. So the probe species stay IN training, seen only through their
    own sheets and reference frames, and the probe measures recall at the low
    end of exposure. `mentions` records how many documents name each one.
    """
    names = list(probe["species"]) + list(probe["reserve"])
    hits = _mention_counts(doc_dirs, names)
    ranked = sorted(names, key=lambda n: (hits.get(n, 0), n))
    target = int(probe["criteria"].get("target", TARGET))
    chosen = sorted(ranked[:target])
    rest = [n for n in ranked[target:]]
    return {
        **probe,
        "species": chosen,
        "reserve": rest,
        "next_excluded_exposure": hits.get(rest[0], 0) if rest else None,
        "mentions": {n: hits.get(n, 0) for n in chosen},
        "verified_against": doc_dirs,
        "semantics": "lowest-exposure reference-only species, IN training (never withheld)",
        "exposure_summary": _exposure_summary(chosen, hits),
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
    if probe.get("exposure_summary"):
        print("exposure (docs naming each probe species):", probe["exposure_summary"])
    classes = collections.Counter(
        _implied_class(load_ima().get(n, "")) for n in probe["species"]
    )
    print("class mix:", dict(classes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
