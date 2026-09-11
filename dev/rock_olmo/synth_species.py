#!/usr/bin/env python3
"""Frozen species groups for the synthetic-corpus pilot (root venv).

GROUPS. 500 targets that receive synthetic documents and 500 controls that
do not, all carrying identical v4 reference exposure in the packed stream:
  T_RL 100  Raman AND LIBS synthetic families (the multi-instrument arm)
  T_L  200  LIBS families only (+ identity: formula, structure)
  T_R  200  Raman families only (+ identity)
  C    500  identity facts exist, nothing synthesised -- the contrast
Every group is drawn from the LIBS-eligible pool when it is large enough, so
a T_R species can be ASKED LIBS questions (the unseen-instrument probe) and
a control has the same fact inventory as a target.

ELIGIBILITY. A formula fact, a canonical Raman fact and a structure fact
with a crystal system; LIBS-eligible adds a libs_lines fact. Excluded: the
100 probe species (kept as an untouched third population), the
element-metal/ambiguous names (holdout.AMBIGUOUS_NAMES) and single-element
formulas.

STRATIFICATION. Tertiles of PROSE exposure -- how many stage-1 prose docs
(papers, hom, webmineral, mindat_prose, packs; not reference.jsonl, which
names every species by construction) mention the name -- so targets and
controls match on natural frequency. Seeded; the file is frozen before §21
and never regenerated afterwards.

    ../../.venv/bin/python synth_species.py            # writes synth_species.json
    ../../.venv/bin/python synth_species.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SEED = 20260911
OUT = os.path.join(HERE, "synth_species.json")
DOCS = os.path.expanduser("~/corpora/rock-olmo-training/v4/stage1/docs")
PROSE_FILES = (
    "papers.jsonl",
    "hom.jsonl",
    "webmineral.jsonl",
    "mindat_prose.jsonl",
    "packs.jsonl",
)
GROUPS = (("T_RL", 100), ("T_L", 200), ("T_R", 200), ("C", 500))
_SINGLE_ELEMENT = re.compile(r"^[A-Z][a-z]?$")


def inventory(facts) -> dict[str, dict]:
    """species -> which fact kinds it carries (canonical raman only)."""
    inv: dict[str, dict] = {}
    for f in facts:
        if not isinstance(f.species, str):
            continue
        d = inv.setdefault(
            f.species,
            {"formula": "", "raman": False, "structure": False, "libs": False},
        )
        if f.kind == "formula":
            d["formula"] = f.formula
        elif f.kind == "raman_bands" and f.canonical:
            d["raman"] = True
        elif f.kind == "structure" and f.payload.get("crystal_system"):
            d["structure"] = True
        elif f.kind == "libs_lines":
            d["libs"] = True
    return inv


def eligible(
    inv: dict[str, dict], exclude: set[str], ambiguous: set[str]
) -> dict[str, bool]:
    """species -> libs_eligible, for every species meeting the base criteria."""
    out = {}
    for sp, d in inv.items():
        if sp in exclude or sp.lower() in ambiguous:
            continue
        if not (d["formula"] and d["raman"] and d["structure"]):
            continue
        if _SINGLE_ELEMENT.match(d["formula"]):
            continue
        out[sp] = bool(d["libs"])
    return out


def strata(names: list[str], mentions: dict[str, int]) -> dict[str, int]:
    """Tertile of prose exposure (0 = rarest) with a deterministic tie-break."""
    order = sorted(names, key=lambda n: (mentions.get(n, 0), n))
    k = max(1, len(order))
    return {n: min(2, (i * 3) // k) for i, n in enumerate(order)}


def assign(pool: dict[str, bool], mentions: dict[str, int], seed: int = SEED) -> dict:
    """Stratified, seeded draw of the four groups. Prefers LIBS-eligible
    species for every group; falls back to Raman-only species when a stratum
    runs short (recorded in `notes`)."""
    rng = random.Random(seed)
    st = strata(sorted(pool), mentions)
    by_stratum = {s: sorted(n for n in pool if st[n] == s) for s in (0, 1, 2)}
    groups = {g: [] for g, _ in GROUPS}
    notes = []
    for s in (0, 1, 2):
        names = by_stratum[s]
        rng.shuffle(names)
        libs_first = [n for n in names if pool[n]] + [n for n in names if not pool[n]]
        cursor = 0
        for g, total in GROUPS:
            want = total // 3 + (1 if s < total % 3 else 0)
            take = libs_first[cursor : cursor + want]
            cursor += want
            if g in ("T_RL", "T_L"):
                bad = [n for n in take if not pool[n]]
                if bad:
                    notes.append(
                        f"stratum {s}: {len(bad)} non-LIBS species fell into {g}; pool too small"
                    )
            if len(take) < want:
                notes.append(f"stratum {s}: {g} short by {want - len(take)}")
            groups[g].extend(take)
    return {
        "groups": {g: sorted(v) for g, v in groups.items()},
        "strata": st,
        "notes": notes,
    }


def build(
    facts,
    exclude: set[str],
    ambiguous: set[str],
    mentions: dict[str, int],
    papers: dict[str, int],
    seed: int = SEED,
) -> dict:
    inv = inventory(facts)
    pool = eligible(inv, exclude, ambiguous)
    res = assign(pool, mentions, seed)
    chosen = [n for v in res["groups"].values() for n in v]
    return {
        "seed": seed,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "criteria": {
            "require": ["formula", "canonical raman_bands", "structure.crystal_system"],
            "libs_eligible": "libs_lines fact",
            "exclude": [
                "probe_species.json",
                "holdout.AMBIGUOUS_NAMES",
                "single-element formulas",
            ],
            "strata": "tertiles of prose-document mentions",
            "prose_files": list(PROSE_FILES),
        },
        "counts": {
            "species_with_facts": len(inv),
            "eligible": len(pool),
            "libs_eligible": sum(pool.values()),
            "by_stratum": {
                str(s): sum(1 for n in pool if res["strata"][n] == s) for s in (0, 1, 2)
            },
            "libs_in_group": {
                g: sum(1 for n in v if pool[n]) for g, v in res["groups"].items()
            },
        },
        "notes": res["notes"],
        "mentions": {n: int(mentions.get(n, 0)) for n in chosen},
        "papers": {n: int(papers.get(n, 0)) for n in chosen},
        "strata": {n: res["strata"][n] for n in chosen},
        "groups": res["groups"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--docs", default=DOCS)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from facts import build_facts
    from holdout import AMBIGUOUS_NAMES
    from probe_species import _mention_counts

    t0 = time.time()
    facts = build_facts()
    print(f"[{time.time()-t0:.0f}s] {len(facts):,} facts", flush=True)
    probe = json.load(open(os.path.join(HERE, "probe_species.json")))
    exclude = set(probe.get("species") or [])
    inv = inventory(facts)
    pool = eligible(inv, exclude, set(AMBIGUOUS_NAMES))
    print(
        f"[{time.time()-t0:.0f}s] eligible {len(pool):,} (LIBS-eligible {sum(pool.values()):,})",
        flush=True,
    )
    mentions = _mention_counts([args.docs], sorted(pool), files=PROSE_FILES)
    print(f"[{time.time()-t0:.0f}s] prose mentions counted", flush=True)
    sp_papers = json.load(open(os.path.join(HERE, "species_papers.json")))["papers"]
    papers = {k: len(v) for k, v in sp_papers.items()}
    out = build(facts, exclude, set(AMBIGUOUS_NAMES), dict(mentions), papers, args.seed)
    for g, v in out["groups"].items():
        ms = sorted(out["mentions"][n] for n in v)
        print(
            f"  {g:5} {len(v):4} species; libs {out['counts']['libs_in_group'][g]:4}; mentions min/med/max {ms[0]}/{ms[len(ms)//2]}/{ms[-1]}"
        )
    for n in out["notes"]:
        print("  NOTE", n)
    if args.dry_run:
        return 0
    if os.path.exists(args.out):
        print(f"REFUSING to overwrite frozen {args.out}; delete it deliberately first")
        return 2
    json.dump(out, open(args.out, "w"), indent=1)
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
