#!/usr/bin/env python3
"""Swap the values of a _min/_max key pair where a reviewer confirmed they
were written to the wrong keys.

The packer keyed "_min" to a LABEL in the paper ("min. working distance")
rather than to the numerically lower value, so a camera's field of view came
out with min 41 deg against max 39 deg. Only rows a reviewer marked "swap"
with a quotation from the paper are applied; "keep" rows are the resolution
trap (a paper ordering coarse-to-fine legitimately puts the larger number
under _min) and are left alone.

Values are exchanged BETWEEN THE TWO KEYS. Nothing is invented, nothing is
dropped, and the pack's leaf count cannot change.

    python dev/apply_suffix_swaps.py --verdicts <suffix_out.json> [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time

DS = os.path.expanduser("~/corpora/ouroboros-spectra/databank/dataset")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--ds", default=DS)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    rows = [
        r for r in json.load(open(a.verdicts))["verdicts"] if r.get("verdict") == "swap"
    ]
    print(f"{len(rows)} rows marked swap")
    edits: dict[str, dict] = {}
    for r in rows:
        path = f"{a.ds}/{r['paper']}.json"
        if not os.path.exists(path):
            print(f"  MISSING pack {r['paper']}")
            continue
        env = edits.get(path) or json.load(open(path))
        data = env.get("data") or {}
        lo, hi = r["min_key"], r["max_key"]
        if lo not in data or hi not in data:
            print(f"  SKIP {r['paper']}: {lo}/{hi} no longer both present")
            continue
        if not (
            isinstance(data[lo], (int, float)) and isinstance(data[hi], (int, float))
        ):
            print(f"  SKIP {r['paper']}: {lo}/{hi} are not both plain numbers")
            continue
        if data[lo] <= data[hi]:
            print(f"  SKIP {r['paper']}: {lo} no longer exceeds {hi}; already correct")
            continue
        print(f"  {r['paper'][:34]:<36} {lo[:40]:<42} {data[lo]} <-> {data[hi]}")
        data[lo], data[hi] = data[hi], data[lo]
        env["data"] = data
        edits[path] = env
    if not a.apply:
        print(f"\n(dry run: {len(edits)} pack files would change)")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for path, env in edits.items():
        shutil.copy2(path, f"{path}.bak-suffix-{stamp}")
        json.dump(env, open(path, "w"), indent=1, ensure_ascii=False)
    print(f"rewrote {len(edits)} pack files (each backed up beside itself)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
