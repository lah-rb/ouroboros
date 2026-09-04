#!/usr/bin/env python3
"""Review the coinage quarantine: keys a pack coined past the guard's
thresholds, held out of the registry until someone looks.

    python dev/coinage_quarantine.py list                 # by count, then paper
    python dev/coinage_quarantine.py promote-shared       # keys coined by >=2 papers
    python dev/coinage_quarantine.py promote KEY [KEY...]
    python dev/coinage_quarantine.py drop KEY [KEY...]
    python dev/coinage_quarantine.py drop-paper PAPER_KEY # everything a paper coined

Promotion folds the key into databank/dataset/key_registry.json with the
same shape update_key_registry writes (tier from key_family, count from the
quarantine). Both files are backed up beside themselves before a write. The
mission reads the registry per pack, so a promotion is live on the next
pack; nothing here touches papers.jsonl.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.curation_actions import (  # noqa: E402
    KEY_QUARANTINE_PATH,
    KEY_REGISTRY_PATH,
    key_family,
)

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _save(path: str, data: dict) -> None:
    if os.path.exists(path):
        shutil.copy2(path, f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def _promote(reg: dict, q: dict, keys: list[str]) -> int:
    n = 0
    for k in keys:
        e = q.pop(k, None)
        if e is None:
            print(f"  not in quarantine: {k}")
            continue
        if k in reg:
            reg[k]["count"] = int(reg[k].get("count") or 0) + int(e.get("count") or 1)
        else:
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
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("promote-shared")
    p = sub.add_parser("promote")
    p.add_argument("keys", nargs="+")
    d = sub.add_parser("drop")
    d.add_argument("keys", nargs="+")
    dp = sub.add_parser("drop-paper")
    dp.add_argument("paper_key")
    a = ap.parse_args()
    qpath = os.path.join(a.root, KEY_QUARANTINE_PATH)
    rpath = os.path.join(a.root, KEY_REGISTRY_PATH)
    q = _load(qpath)
    if a.cmd == "list":
        by_paper = collections.Counter(e.get("first_paper") for e in q.values())
        print(f"{len(q)} quarantined keys from {len(by_paper)} papers")
        for paper, n in by_paper.most_common():
            print(f"  {n:>4}  {paper}")
        shared = sorted(
            (
                (int(e.get("count") or 1), k)
                for k, e in q.items()
                if int(e.get("count") or 1) >= 2
            ),
            reverse=True,
        )
        print(f"\ncoined by >=2 papers ({len(shared)}):")
        for c, k in shared[:40]:
            print(f"  {c:>3}  {k}  e.g. {q[k].get('exemplar')}")
        return 0
    reg = _load(rpath)
    if a.cmd == "promote-shared":
        keys = [k for k, e in q.items() if int(e.get("count") or 1) >= 2]
    elif a.cmd == "promote":
        keys = a.keys
    elif a.cmd == "drop":
        for k in a.keys:
            q.pop(k, None)
        _save(qpath, q)
        print(f"dropped {len(a.keys)}; {len(q)} remain")
        return 0
    else:
        keys = [k for k, e in q.items() if e.get("first_paper") == a.paper_key]
        for k in keys:
            q.pop(k, None)
        _save(qpath, q)
        print(f"dropped {len(keys)} keys coined by {a.paper_key}; {len(q)} remain")
        return 0
    n = _promote(reg, q, keys)
    _save(rpath, reg)
    _save(qpath, q)
    print(
        f"promoted {n} keys into the registry ({len(reg)} entries); {len(q)} remain quarantined"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
