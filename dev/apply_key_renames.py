#!/usr/bin/env python3
"""Rename malformed pack keys: misspellings, stutters, reversed units.

A rename is not an alias. An alias says two names mean one quantity; a
rename says the name itself was written wrong -- `deplorization_factor_formula`,
`them is_band_center_um` (THEMIS with a stray space), `bet_surface_area_g_m2`
whose unit reads backwards, and 58 keys spelling the SHERLOC context camera
WATSON as "watsom". The quantity is unchanged, so values are never touched.

Every proposal is re-checked here: the new name must be well-formed, must not
collide with a key that already exists or with another rename, and the old key
must actually be present. An alias old -> new is recorded too, so a stale
reference in a future pack still canonicalises instead of coining the typo
again.

    python dev/apply_key_renames.py --verdicts <rename_out.json> [--apply]
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import shutil
import time

DS = os.path.expanduser("~/corpora/ouroboros-spectra/databank/dataset")
# Unit suffixes keep their hyphen ("cm-1"), which is the corpus convention.
WELL_FORMED = re.compile(r"^[a-z][a-z0-9]*(?:[_.-][a-z0-9]+)*$")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--ds", default=DS)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    v = json.load(open(a.verdicts))
    reg = json.load(open(f"{a.ds}/key_registry.json"))
    quar = json.load(open(f"{a.ds}/key_registry_quarantine.json"))
    aliases = json.load(open(f"{a.ds}/key_aliases.json"))
    known = set(reg) | set(quar)

    ren: dict[str, str] = {}
    why = collections.Counter()
    for r in v.get("renames") or []:
        old, new = str(r.get("key") or ""), str(r.get("new_key") or "")
        if old not in known:
            why["old key absent"] += 1
        elif not WELL_FORMED.match(new):
            why["new name malformed"] += 1
        elif new in known:
            why["new name already exists (that is an alias, not a rename)"] += 1
        elif new in ren.values():
            why["two renames want the same new name"] += 1
        elif old == new:
            why["no change"] += 1
        else:
            ren[old] = new
    print(
        f"{len(v.get('renames') or [])} proposed | {len(ren)} accepted | refused {dict(why)}"
    )
    files = [
        f
        for f in glob.glob(f"{a.ds}/*.json")
        if not os.path.basename(f).startswith("key_")
    ]
    hits = [
        f
        for f in files
        if any(k in ren for k in (json.load(open(f)).get("data") or {}))
    ]
    print(f"pack files holding a renamed key: {len(hits)}")
    for old, new in list(ren.items())[:6]:
        print(f"   {old[:48]:<50} -> {new}")
    if not a.apply:
        print("\n(dry run: nothing written)")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copytree(a.ds, f"{a.ds}_bak_rename_{stamp}")
    print(f"backed up -> {a.ds}_bak_rename_{stamp}")
    n = 0
    for f in hits:
        env = json.load(open(f))
        data = env.get("data") or {}
        env["data"] = {ren.get(k, k): val for k, val in data.items()}
        json.dump(env, open(f, "w"), indent=1, ensure_ascii=False)
        n += 1
    for old, new in ren.items():
        if old in reg:
            e = reg.pop(old)
            e["renamed_from"] = old
            e["renamed_at"] = time.strftime("%Y-%m-%d")
            reg[new] = e
        if old in quar:
            quar[new] = quar.pop(old)
        # Retarget aliases that pointed at the misspelling. This can produce a
        # SELF-alias when the correctly-spelled name was itself an alias of the
        # typo (live: icp_oes_instrument -> icp_oess_instrument, because the
        # typo had the higher count and won the canonical slot); drop those, and
        # collapse any chain the retarget creates.
        for al, can in list(aliases.items()):
            if can == old:
                aliases[al] = new
        aliases[old] = new
    for al in [k for k, c in aliases.items() if k == c]:
        del aliases[al]
    for _ in range(5):
        chained = {k: c for k, c in aliases.items() if c in aliases and aliases[c] != c}
        if not chained:
            break
        for k, c in chained.items():
            aliases[k] = aliases[c]
    for al in [k for k, c in aliases.items() if k == c]:
        del aliases[al]
    for path, obj in (
        (f"{a.ds}/key_registry.json", reg),
        (f"{a.ds}/key_registry_quarantine.json", quar),
        (f"{a.ds}/key_aliases.json", aliases),
    ):
        tmp = path + ".tmp"
        json.dump(obj, open(tmp, "w"), indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    print(
        f"rewrote {n} packs | registry {len(reg)} | quarantine {len(quar)} | aliases {len(aliases)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
