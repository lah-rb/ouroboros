"""De-anonymise the per-figure judge scores and aggregate.

Each figure used a DIFFERENT letter shuffle, so letters are only meaningful
against that figure's keymap entry. This is the single place the mapping is
applied — nothing upstream of here knows which candidate is which model, which
is the property that makes the scores worth having.

Reads the one-line verdicts (figure A=n B=n ... of N | fabrications: X,Y) from
a verdicts file, one per line.
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = Path(os.environ.get("VIS_BENCH_BUNDLES", HERE / "results" / "bundles"))
keymap = json.loads((BUNDLE / "_keymap.json").read_text())
verdicts = Path(sys.argv[1] if len(sys.argv) > 1 else HERE / "results" / "verdicts.txt")

got = defaultdict(dict)  # model -> figure -> score
total = {}  # figure -> fact count
fabs = defaultdict(list)  # model -> [figures]
LINE = re.compile(
    r"^(?P<fig>\S+)\s+(?P<scores>(?:[A-E]=\d+\s*)+)of\s+(?P<total>\d+)"
    r".*?fabrications:\s*(?P<fab>.*)$"
)

for raw in verdicts.read_text().splitlines():
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        continue
    m = LINE.match(raw)
    if not m:
        print(f"  UNPARSED: {raw}")
        continue
    fig = m.group("fig")
    if fig not in keymap:
        print(f"  UNKNOWN FIGURE: {fig}")
        continue
    total[fig] = int(m.group("total"))
    for letter, n in re.findall(r"([A-E])=(\d+)", m.group("scores")):
        model = keymap[fig].get(letter)
        if model:
            got[model][fig] = int(n)
    fab = m.group("fab").strip()
    if fab.lower() not in ("none", "-", ""):
        for letter in re.findall(r"[A-E]", fab):
            model = keymap[fig].get(letter)
            if model:
                fabs[model].append(fig)

figs = [f for f in keymap if f in total]
models = sorted(got, key=lambda m: -sum(got[m].values()))
w = max((len(m) for m in models), default=10)

print(
    f"\n{'model':<{w}} "
    + " ".join(f"{f[:9]:>9}" for f in figs)
    + f"{'TOTAL':>9} {'%':>6} {'FAB':>5}"
)
print("-" * (w + 10 * len(figs) + 23))
for m in models:
    cells = " ".join(f"{got[m].get(f, 0):>9}" for f in figs)
    s = sum(got[m].values())
    t = sum(total[f] for f in figs if f in got[m])
    pct = 100.0 * s / t if t else 0
    print(f"{m:<{w}} {cells}{s:>9} {pct:>5.1f}% {len(fabs[m]):>5}")
print("-" * (w + 10 * len(figs) + 23))
print(
    f"{'facts':<{w}} "
    + " ".join(f"{total[f]:>9}" for f in figs)
    + f"{sum(total.values()):>9}"
)

print("\nFABRICATIONS by model (rubric rule 2 — asserted content not in the image):")
for m in models:
    print(f"  {m:<{w}} {len(fabs[m])}/{len(figs)}  {', '.join(fabs[m]) or '-'}")
