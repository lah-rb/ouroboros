#!/usr/bin/env python3
"""Full Raman spectra for the stage-2 species (PROCEDURE §22k; root venv — needs the facts layer).

The XML records keep only the strongest four bands. The representation arm draws its
training spectra from the WHOLE canonical list (positions + relative intensities), so that
intensity variation can re-rank which bands are strongest. Its probe reads the whole held-out
real re-measurement for the same reason. Written once and frozen; the rock-venv builders
read the JSON.

  {"canonical": {species: [[position_cm1, rel], ...]},      # every xml_records species
   "real":      {species: {"sample_id", "source", "laser_nm", "peaks": [[pos, rel], ...]}}}

  ../../.venv/bin/python export_spectra.py --out ~/corpora/rock-olmo-training/v6/stage2k/docs/spectra_full.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

RECORDS = os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2/docs/xml_records.json")
REAL = os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2r/docs/real_remeasurements.json")


def peaks(payload: dict) -> list[list[float]]:
    """[[position, rel]] position-sorted, or [] when positions and intensities do not align."""
    b, r = payload.get("bands_cm1") or [], payload.get("rel") or []
    if len(b) < 4 or len(r) != len(b):
        return []
    return sorted([[float(x), float(y)] for x, y in zip(b, r)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from facts import build_facts

    x = json.load(open(RECORDS))
    names = {r["species"] for r in x["trained"] + x["untouched"]}
    real = json.load(open(REAL))
    canon, cands = {}, {}
    for f in build_facts(set()):
        if f.kind != "raman_bands" or not isinstance(f.species, str) or f.species not in names:
            continue
        pk = peaks(f.payload)
        if not pk:
            continue
        if f.canonical:
            canon[f.species] = pk
        elif f.species in real and f.payload.get("sample_id") == real[f.species]["sample_id"]:
            cands.setdefault(f.species, []).append((str(f.payload.get("laser_nm") or ""), pk))
    # One sample often has several spectra (lasers, orientations). Take the one §22h used:
    # same laser, and its strongest four equal to the four §22h recorded.
    found = {}
    for sp, options in cands.items():
        m = real[sp]
        want = sorted(int(b) for b in m["bands"])

        def score(opt):
            laser, pk = opt
            top = sorted(int(round(p)) for p, _ in sorted(pk, key=lambda x: -x[1])[:4])
            return (laser == str(m["laser_nm"]), top == want, len(set(top) & set(want)))

        best = max(options, key=score)
        found[sp] = {"sample_id": m["sample_id"], "source": m["source"], "laser_nm": m["laser_nm"], "peaks": best[1],
                     "matches_22h_top4": score(best)[1], "same_sample_spectra": len(options)}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump({"canonical": canon, "real": found}, open(args.out, "w"))
    exact = sum(v["matches_22h_top4"] for v in found.values())
    multi = sum(v["same_sample_spectra"] > 1 for v in found.values())
    print(f"canonical {len(canon):,} of {len(names):,} species; real re-measurements {len(found):,} of {len(real):,} "
          f"({multi} samples with several spectra; {exact} reproduce §22h's strongest four exactly) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
