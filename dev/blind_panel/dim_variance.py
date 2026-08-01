"""Which rubric dimensions actually SEPARATE models, and which are constant?

Reads every recorded dimension vector out of llmvp/configs/*.yaml (doc-only
`tier:` blocks) and reports, per dimension, the spread across arms.

The question this answers: the 2026-07-31 sweep's scored artifacts cluster in a
narrow band (46-60). Is that because the models are genuinely close, or because
most of the 100 points are pinned at the same value for everyone -- i.e. the
instrument is measuring the BRIEF rather than the model?
"""
import sys
import statistics as st
from pathlib import Path

import yaml

CONFIGS = Path("llmvp/configs")


def find_dimension_blocks(node, path=()):
    """Yield (path, block, dims_dict) for every `dimensions:` mapping under tier:.

    `block` is the enclosing mapping, so the sibling `run:`/`total:` keys are
    available -- a config can legitimately carry MORE THAN ONE judged block
    (the same config scored in two different sweeps against two rubric
    versions), and those must not be collapsed into one arm.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "dimensions" and isinstance(v, dict):
                yield path, node, v
            else:
                yield from find_dimension_blocks(v, path + (str(k),))


def sweep_of(run: str) -> str:
    """Which sweep a run belongs to, from its run id."""
    for tag in ("20260731", "20260729", "20260730"):
        if tag in run:
            return tag
    return "unknown"


def collect():
    """-> list of (label, sweep, total, {dim: (score, max)})"""
    out = []
    for f in sorted(CONFIGS.rglob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text())
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        tier = doc.get("tier")
        if not isinstance(tier, dict):
            continue
        for _path, block, dims in find_dimension_blocks(tier):
            run = str(block.get("run", ""))
            vec = {n: (float(d["score"]), float(d.get("max", 0)))
                   for n, d in dims.items()
                   if isinstance(d, dict) and "score" in d}
            if not vec:
                continue
            # single-judge blocks record `total:`; three-judge blocks record
            # `score:` (the MEDIAN of the votes, per rubric §5)
            score = block.get("total")
            if score is None:
                score = block.get("score")
            if score is None:
                score = block.get("median")
            out.append((f.stem, sweep_of(run), score, vec))
    return out


def report(pop, title):
    """Per-dimension spread for one population of vectors."""
    print(f"\n{'=' * 78}\n{title}  (n={len(pop)})\n{'=' * 78}")
    for label, _sw, total, vec in sorted(pop, key=lambda r: -(r[2] or 0)):
        print(f"  {label:36s} total={total}")
    if len(pop) < 2:
        return
    dims = []
    for _l, _s, _t, vec in pop:
        for n, (_sc, mx) in vec.items():
            if n not in [d for d, _ in dims]:
                dims.append((n, mx))
    print()
    print(f"{'dimension':22s} {'max':>4s} {'min':>4s} {'max':>4s} {'rng':>4s} "
          f"{'sd':>5s} {'rng/max':>8s}  values")
    print("-" * 92)
    summary = []
    for dname, dmax in dims:
        vals = [vec[dname][0] for _l, _s, _t, vec in pop if dname in vec]
        if len(vals) < 2:
            continue
        rng = max(vals) - min(vals)
        sd = st.pstdev(vals)
        frac = rng / dmax if dmax else 0.0
        summary.append((frac, rng, sd, dname, dmax))
        print(f"{dname:22s} {dmax:4g} {min(vals):4g} {max(vals):4g} {rng:4g} "
              f"{sd:5.2f} {frac:8.2f}  {sorted(vals)}")
    summary.sort(reverse=True)
    print("\nRANKED BY SEPARATING POWER (range as a fraction of the max):")
    for frac, rng, sd, dname, dmax in summary:
        verdict = ("SEPARATES" if frac >= 0.4 else
                   "weak" if frac >= 0.2 else "PINNED")
        print(f"  {frac:5.2f}  {dname:22s} ({rng:g}/{dmax:g}, sd {sd:.2f})  {verdict}")
    live = sum(m for fr, _, _, _, m in summary if fr >= 0.4)
    weak = sum(m for fr, _, _, _, m in summary if 0.2 <= fr < 0.4)
    pin = sum(m for fr, _, _, _, m in summary if fr < 0.2)
    print(f"\nPOINT BUDGET:  separating={live:g}  weak={weak:g}  pinned={pin:g}"
          f"  (of {live + weak + pin:g})")
    totals = [t for _l, _s, t, _v in pop if t is not None]
    if len(totals) > 1:
        print(f"TOTALS: min={min(totals)} max={max(totals)} "
              f"range={max(totals) - min(totals)} sd={st.pstdev(totals):.2f}")


def pedestal_and_rerank(pop, pinned):
    """Does removing the near-constant dimensions change the RANKING?

    If the order survives, the tightness is an instrument property (a floor
    every artifact clears) and the ranking still means something. If the order
    scrambles, the ranking was riding on dimensions that do not discriminate.
    """
    print(f"\n{'=' * 78}\nPEDESTAL TEST -- drop the pinned dimensions {sorted(pinned)}"
          f"\n{'=' * 78}")
    rows = []
    for label, _sw, total, vec in pop:
        ped = sum(sc for n, (sc, _m) in vec.items() if n in pinned)
        live = sum(sc for n, (sc, _m) in vec.items() if n not in pinned)
        live_max = sum(m for n, (_s, m) in vec.items() if n not in pinned)
        rows.append((label, total, ped, live, live_max))

    by_total = [r[0] for r in sorted(rows, key=lambda r: -(r[1] or 0))]
    by_live = [r[0] for r in sorted(rows, key=lambda r: -r[3])]
    # NOTE: several arms TIE on the total, so their order in `by_total` is
    # insertion order, not a result. Comparing the two orderings directly would
    # report a spurious "ranking changed". Only pairs that DIFFER on the total
    # can invert, so those are the only ones counted below.

    print(f"{'arm':36s} {'total':>6s} {'pedestal':>9s} {'live':>6s} {'live/max':>9s}")
    for label, total, ped, live, live_max in sorted(rows, key=lambda r: -(r[1] or 0)):
        print(f"{label:36s} {str(total):>6s} {ped:9g} {live:6g} "
              f"{live / live_max * 100:8.1f}%")

    peds = [r[2] for r in rows]
    print(f"\nPEDESTAL: every artifact banks {min(peds):g}-{max(peds):g} points "
          f"from the pinned dimensions (mean {st.mean(peds):.1f}) "
          f"regardless of quality.")
    print(f"LIVE RANGE: {min(r[3] for r in rows):g}-{max(r[3] for r in rows):g} "
          f"of {rows[0][4]:g} possible.")
    print(f"\nrank by TOTAL: {' > '.join(by_total)}")
    print(f"rank by LIVE : {' > '.join(by_live)}")

    inversions, agree, ties_split = [], 0, []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            ta, tb = (a[1] or 0), (b[1] or 0)
            if ta == tb:
                if a[3] != b[3]:
                    hi, lo = (a, b) if a[3] > b[3] else (b, a)
                    ties_split.append(f"{hi[0]} ({hi[3]}) > {lo[0]} ({lo[3]})")
                continue
            hi_t, lo_t = (a, b) if ta > tb else (b, a)
            if hi_t[3] < lo_t[3]:
                inversions.append(f"total says {hi_t[0]}>{lo_t[0]}, "
                                  f"live says {lo_t[0]}>{hi_t[0]}")
            else:
                agree += 1
    print(f"\nORDERED PAIRS (distinct totals only): {agree} agree, "
          f"{len(inversions)} invert")
    for s in inversions:
        print(f"  INVERSION: {s}")
    if ties_split:
        print(f"\nTIES ON THE TOTAL THAT LIVE SCORE RESOLVES ({len(ties_split)}):")
        for s in ties_split:
            print(f"  {s}")


def main():
    pop = collect()
    if not pop:
        print("no dimension vectors recorded")
        return 1
    cur = [r for r in pop if r[1] == "20260731"]
    report(cur, "THE 2026-07-31 SWEEP (v1.2) -- the population under doubt")
    pedestal_and_rerank(cur, {"conformance", "creativity", "documentation"})
    report(pop, "ALL RECORDED VECTORS (both sweeps, both rubric versions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
