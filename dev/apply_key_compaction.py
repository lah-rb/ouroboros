#!/usr/bin/env python3
"""Validate and apply an agent-proposed key-registry compaction.

The proposals come from `dev/` sub-agent jobs (aliases, quarantine
promotions, drops). NOTHING is trusted: every alias is re-checked here
against the rules the operator reform set in 2026-08-22 and restated in the
agents' policy, because a wrong merge silently pools values that are not
comparable and there is no way to tell afterwards.

Rejections (each counted and reported, never silently dropped):
  missing        either side is not a key we hold
  self           alias == canonical
  chain/cycle    the canonical is itself aliased
  type           registry types differ
  unit           the trailing unit tokens differ (nm vs um, ppm vs pct)
  digit          either side is digit-parameterised (_2, ph3) -- the digit
                 carries a condition, so the values are not interchangeable
  aggregate      one side is a min/max/avg/mean variant and the other is not
  direction      a registry key would be aliased onto a quarantined key

    python dev/apply_key_compaction.py --in <dir>            # report only
    python dev/apply_key_compaction.py --in <dir> --apply
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra/databank")
DS = f"{ROOT}/dataset"
REG, QUAR, ALIAS = (
    f"{DS}/key_registry.json",
    f"{DS}/key_registry_quarantine.json",
    f"{DS}/key_aliases.json",
)
_UNITS = (
    "nm|um|mm|cm|m|k|c|kv|ma|mj|j|w|mw|ev|hz|ghz|mhz|thz|ppm|ppb|pct|percent|wt|mol|deg|"
    "s|sec|ms|ns|ps|fs|us|min|minutes|h|hr|hours|gpa|mpa|pa|bar|atm|g|mg|kg|ml|l|"
    "cm2|cm3|m2|a|v|hv|rpm|cm-1|degrees|micron|microns|mass|weight|lines|grooves|"
    "h-1|microsecond|microseconds"
)
# "range" is NOT here: it is a filler word ("xrd_measurement_range_2theta_max_deg"
# is the same quantity as "xrd_2theta_max_deg"). avg/average are one aggregate.
_AGG = re.compile(r"(?:^|_)(min|max|avg|average|mean|median|std|total)(?:$|_)")
_AGG_SYNONYM = {"average": "avg"}
_DIGIT = re.compile(r"(?:^|_)\d+$|[a-z]\d(?:$|_)")


# Spellings of the SAME unit, which may merge. Deliberately tiny: "cm" and
# "cm-1" are NOT here (length vs wavenumber), nor "wt" and "percent" (weight
# percent vs percent). A pair that needs domain judgement belongs in the
# agents' notes, not in an automatic equivalence.
_UNIT_SPELLING = {
    "degrees": "deg",
    "micron": "um",
    "microns": "um",
    "sec": "s",
    "hr": "h",
    "hours": "h",
    "minutes": "min",
    "pct": "percent",
    # Unambiguous same-unit spellings, each confirmed against exemplars by the
    # reviewing agents: 1 cm3 IS 1 mL; mass% IS wt%; a grating's g/mm, l/mm,
    # lines/mm and grooves/mm are one groove density; a stirrer's min-1 is rpm.
    "cm3": "ml",
    "mass": "wt",
    "grooves": "lines",
    "g": "lines",
    "l": "lines",
    "min-1": "rpm",
    "weight": "wt",
    "microseconds": "us",
    "microsecond": "us",
}
# ...but "g" and "l" are grams and litres everywhere EXCEPT beside "per_mm".
_GRATING = re.compile(r"(?:^|_)(?:g|l|lines|grooves)_(?:per_)?mm(?:$|_)")
# Technique abbreviations whose trailing "ms" is mass spectrometry, not
# milliseconds: gc_ms, icp_ms, lc_ms, tof_ms.
_TECHNIQUE_MS = re.compile(r"(?:^|_)(?:gc|lc|icp|tof|sims|tims|py|hs)_ms(?:$|_)")


def _agg(key: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            _AGG_SYNONYM.get(m.group(1), m.group(1)) for m in _AGG.finditer(key.lower())
        )
    )


def _is_parameterised(key: str) -> bool:
    """A trailing counter or a pH-style condition digit.

    Deliberately NARROW. An earlier version flagged any letters+digits token
    and refused "tio2_concentrations_mg_per_l" and "calibration_curve_r2":
    chemical formulas (tio2, al2o3, c1s) and statistic names (r2) carry
    digits without being parameterised. What must never merge is a SECOND
    INSTANCE ("sample_preparation_2", "laser_wavelength_nm_2") or a stated
    condition ("ph3"), so only those two shapes are refused.
    """
    low = key.lower()
    return bool(re.search(r"_\d+$", low) or re.search(r"(?:^|_)ph\d+(?:$|_)", low))


def _units(key: str) -> tuple[str, ...]:
    """Unit tokens in a key, order-insensitive, aggregate words removed first.

    "min" is both a unit (minutes) and an aggregate (minimum); the aggregate
    test owns it, so strip aggregate words before reading units or every
    *_min key looks like it carries a time unit.
    """
    low = _AGG.sub("_", key.lower())
    # "gc_ms" is mass spectrometry, not milliseconds.
    low = _TECHNIQUE_MS.sub("_msdetector_", low)
    grating = bool(_GRATING.search(low))
    out = []
    for t in re.split(r"[^a-z0-9-]+", low):
        if not re.fullmatch(_UNITS, t):
            continue
        if t in ("g", "l", "lines", "grooves") and not grating:
            out.append(t)  # grams / litres, not a groove density
        else:
            out.append(_UNIT_SPELLING.get(t, t))
    return tuple(sorted(out))


def validate(
    props: list[dict], reg: dict, quar: dict
) -> tuple[dict, collections.Counter, list]:
    """(accepted alias map, rejection counts, rejection records)."""
    known = set(reg) | set(quar)
    ok: dict[str, str] = {}
    why = collections.Counter()
    rejected = []

    def reject(p, reason):
        why[reason] += 1
        rejected.append({**p, "rejected": reason})

    for p in props:
        a, c = str(p.get("alias") or ""), str(p.get("canonical") or "")
        if not a or not c or a not in known or c not in known:
            reject(p, "missing")
        elif a == c:
            reject(p, "self")
        elif _agg(a) != _agg(c):
            reject(p, "aggregate")
        elif _units(a) != _units(c):
            reject(p, "unit")
        elif _is_parameterised(a) or _is_parameterised(c):
            reject(p, "digit")
        elif str((reg.get(a) or quar.get(a) or {}).get("type")) != str(
            (reg.get(c) or quar.get(c) or {}).get("type")
        ):
            reject(p, "type")
        elif a in reg and c not in reg:
            reject(p, "direction")
        else:
            ok[a] = c
    # chains and cycles: a canonical must not itself be an alias
    changed = True
    while changed:
        changed = False
        for a, c in list(ok.items()):
            if c in ok:
                if ok[c] == a or ok[c] in (a, c):
                    del ok[a]
                    why["chain/cycle"] += 1
                    changed = True
                    break
                ok[a] = ok[c]
                changed = True
    return ok, why, rejected


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    reg, quar = json.load(open(REG)), json.load(open(QUAR))
    aliases = json.load(open(ALIAS))
    props, promos, drops, notes = [], [], [], []
    files = sorted(glob.glob(f"{a.inp}/out_*.json"))
    for f in files:
        try:
            d = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            print(f"  UNREADABLE {os.path.basename(f)}: {e}")
            continue
        props += d.get("aliases") or []
        promos += d.get("promotions") or []
        drops += d.get("drops") or []
        notes += d.get("notes") or []
    print(
        f"{len(files)} agent verdicts: {len(props)} alias proposals, {len(promos)} promotions, {len(drops)} drops"
    )
    ok, why, rejected = validate(props, reg, quar)
    print(f"aliases accepted {len(ok)} | rejected {sum(why.values())}: {dict(why)}")
    promos = [
        p for p in promos if str(p.get("key")) in quar and str(p.get("key")) not in ok
    ]
    drops = [
        p for p in drops if str(p.get("key")) in quar and str(p.get("key")) not in ok
    ]
    both = {p["key"] for p in promos} & {p["key"] for p in drops}
    if both:
        print(f"  {len(both)} keys proposed BOTH promote and drop -> left quarantined")
        promos = [p for p in promos if p["key"] not in both]
        drops = [p for p in drops if p["key"] not in both]
    print(
        f"promotions {len(promos)} | drops {len(drops)} | quarantine left {len(quar)-len(promos)-len(drops)-sum(1 for k in ok if k in quar)}"
    )
    ds = glob.glob(f"{DS}/*.json")
    ds = [
        f
        for f in ds
        if os.path.basename(f)
        not in ("key_registry.json", "key_registry_quarantine.json", "key_aliases.json")
    ]
    touched = sum(
        1 for f in ds if any(k in ok for k in (json.load(open(f)).get("data") or {}))
    )
    print(f"dataset files that would be rewritten: {touched} of {len(ds)}")
    for n in notes[:12]:
        print(f"  note: {str(n)[:150]}")
    if not a.apply:
        print("\n(dry run: nothing written)")
        json.dump(
            {"aliases": ok, "rejected": rejected},
            open(f"{a.inp}/validated.json", "w"),
            indent=1,
        )
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copytree(DS, f"{DS}_bak_compaction_{stamp}")
    print(f"backed up {DS} -> {DS}_bak_compaction_{stamp}")
    # 1. data files
    n_files = n_keys = 0
    for f in ds:
        env = json.load(open(f))
        data = env.get("data") or {}
        if not any(k in ok for k in data):
            continue
        out: dict = {}
        for k, v in data.items():
            ck = ok.get(k, k)
            if ck in out:
                n_keys += 1
                if isinstance(out[ck], list) and isinstance(v, list):
                    out[ck] = out[ck] + [x for x in v if x not in out[ck]]
            else:
                out[ck] = v
        env["data"] = out
        json.dump(env, open(f, "w"), indent=1, ensure_ascii=False)
        n_files += 1
    print(f"rewrote {n_files} dataset files ({n_keys} key collisions merged)")
    # 2. PROMOTIONS FIRST. A promoted key can be the canonical side of an
    # alias (live: binder_concentration_percent_w_w -> the promoted
    # binder_concentration_w_w_pct); folding aliases first would drop the
    # variant's count on the floor, its canonical not yet being in the
    # registry. Sub-agent job 06 caught this.
    from agent.actions.curation_actions import key_family  # noqa: E402

    for p in promos:
        k = p["key"]
        e = quar.pop(k)
        fam = key_family(k)
        reg[k] = {
            "type": e.get("type") or "string",
            "description": "",
            "exemplar": e.get("exemplar") or "",
            "count": int(e.get("count") or 1),
            "first_paper": e.get("first_paper") or "",
            "similar_to": [],
            "tier": "family" if fam else "bespoke-pool",
            **({"family": fam} if fam else {}),
            "promoted_from_quarantine": time.strftime("%Y-%m-%d"),
        }
    # 3. registry: fold counts, drop the aliased spellings
    for al, can in ok.items():
        e = reg.pop(al, None)
        if e and can in reg:
            reg[can]["count"] = int(reg[can].get("count") or 0) + int(
                e.get("count") or 0
            )
        elif e:
            print(f"  WARNING alias {al} -> {can}: canonical absent from the registry")
    for p in drops:
        quar.pop(p["key"], None)
    for k in list(ok):
        quar.pop(k, None)
    aliases.update(ok)
    for path, obj in ((REG, reg), (QUAR, quar), (ALIAS, aliases)):
        tmp = path + ".tmp"
        json.dump(obj, open(tmp, "w"), indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    print(f"registry {len(reg)} keys | quarantine {len(quar)} | aliases {len(aliases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
