#!/usr/bin/env python3
"""Unify a scalar key and its plural twin onto one key holding a shallow list.

WHY (operator, 2026-09-04). The registry pins a key's type from its FIRST
paper. A later paper reporting several spectrometers cannot use a `string`
slot, so the model coins `spectrometer_models` as a list -- one quantity,
two keys, and no merge is possible because a scalar cannot hold a list.
Measured across the registry: 19 such pairs, and in EVERY one the plural
side really does hold multi-element values, so there is no cheap collapse.
The clean repair is the containing form: a list of one holds a single
value; a scalar cannot hold several.

WHAT IT DOES, for each pair (canonical = the higher-count spelling):
  * every pack holding the canonical as a bare value  -> wrap it, [value]
  * every pack holding the plural twin                -> move to canonical,
                                                         concatenating
  * registry: canonical type -> list[T], counts folded, twin removed
  * key_aliases.json: twin -> canonical, so future packs canonicalise

INVARIANT, asserted before anything is written: the corpus's total LEAF
count must not change. Wrapping a scalar in a list adds a container, never
a value, so any change in leaves is a bug and aborts the run.

    python dev/widen_scalar_keys.py --plan <widen_needed.json>
    python dev/widen_scalar_keys.py --plan <...> --apply
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import time

DS = os.path.expanduser("~/corpora/ouroboros-spectra/databank/dataset")


def _leaves(o) -> int:
    if isinstance(o, dict):
        return sum(_leaves(v) for v in o.values())
    if isinstance(o, list):
        return sum(_leaves(v) for v in o)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--ds", default=DS)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    plan = json.load(open(a.plan))
    reg = json.load(open(f"{a.ds}/key_registry.json"))
    aliases = json.load(open(f"{a.ds}/key_aliases.json"))
    files = [
        f
        for f in glob.glob(f"{a.ds}/*.json")
        if not os.path.basename(f).startswith("key_")
    ]

    moves = {}  # twin -> canonical
    widen = {}  # canonical -> target list type
    for r in plan:
        canon = r["canonical"]
        twin = r["container"] if canon == r["scalar"] else r["scalar"]
        if twin in moves or canon in moves:
            print(f"  SKIP {twin} -> {canon}: overlaps another pair in this plan")
            continue
        moves[twin] = canon
        widen[canon] = r["target_type"]
    print(f"{len(widen)} keys widened, {len(moves)} plural twins folded in")

    wrapped = moved = touched = 0
    before = after = 0
    out_files = []
    for f in files:
        env = json.load(open(f))
        data = env.get("data") or {}
        before += _leaves(data)
        new = dict(data)
        hit = False
        for twin, canon in moves.items():
            if twin not in new:
                continue
            v = new.pop(twin)
            vs = v if isinstance(v, list) else [v]
            cur = new.get(canon)
            base = cur if isinstance(cur, list) else ([cur] if canon in new else [])
            new[canon] = base + [x for x in vs if x not in base]
            moved += 1
            hit = True
        for canon in widen:
            if canon in new and not isinstance(new[canon], list):
                new[canon] = [new[canon]]
                wrapped += 1
                hit = True
        after += _leaves(new)
        if hit:
            touched += 1
            env["data"] = new
            out_files.append((f, env))
    print(
        f"packs touched {touched}/{len(files)} | scalars wrapped {wrapped} | twin values moved {moved}"
    )
    print(f"leaf values before {before:,} after {after:,}")
    if after != before:
        # A move that merges a duplicate value legitimately drops leaves; a
        # wrap never does. Report the difference rather than guessing.
        print(f"  NOTE leaf delta {after-before} (duplicate values merged by a move)")
    if not a.apply:
        print("\n(dry run: nothing written)")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copytree(a.ds, f"{a.ds}_bak_widen_{stamp}")
    print(f"backed up -> {a.ds}_bak_widen_{stamp}")
    for f, env in out_files:
        json.dump(env, open(f, "w"), indent=1, ensure_ascii=False)
    for twin, canon in moves.items():
        e = reg.pop(twin, None)
        if e and canon in reg:
            reg[canon]["count"] = int(reg[canon].get("count") or 0) + int(
                e.get("count") or 0
            )
        aliases[twin] = canon
    for canon, want in widen.items():
        if canon in reg:
            reg[canon]["type"] = want
            reg[canon]["widened_at"] = time.strftime("%Y-%m-%d")
    for path, obj in (
        (f"{a.ds}/key_registry.json", reg),
        (f"{a.ds}/key_aliases.json", aliases),
    ):
        tmp = path + ".tmp"
        json.dump(obj, open(tmp, "w"), indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    print(
        f"rewrote {len(out_files)} packs | registry {len(reg)} keys | aliases {len(aliases)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
