"""Build the candidate shortlist the SELECT prompt triages.

One figure per paper, stratified across the corpus, SEEDED so a re-run
reproduces the same shortlist — a disputed selection has to be traceable.
Size-filtered because a 60KB PNG in this corpus is usually a logo or a rule,
not a figure with checkable content.
"""

import argparse
import json
import random
from pathlib import Path

DEFAULT_ROOT = Path.home() / "corpora" / "ouroboros-spectra" / "databank" / "figures"

ap = argparse.ArgumentParser()
ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
ap.add_argument("--out", type=Path, required=True)
ap.add_argument("--n", type=int, default=30)
ap.add_argument("--seed", type=int, default=20260823)
ap.add_argument("--min-bytes", type=int, default=120_000)
a = ap.parse_args()

cands = [
    f
    for p in sorted(x for x in a.root.iterdir() if x.is_dir())
    for f in sorted(p.glob("*.png"))
    if f.stat().st_size > a.min_bytes
]
random.seed(a.seed)
random.shuffle(cands)

seen: set[str] = set()
short: list[Path] = []
for f in cands:
    if f.parent.name in seen:
        continue
    seen.add(f.parent.name)
    short.append(f)
    if len(short) == a.n:
        break

a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps([str(f) for f in short], indent=1))
print(f"{len(short)} candidates from {len(seen)} papers -> {a.out}")
