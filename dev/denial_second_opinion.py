#!/usr/bin/env python3
"""Fold the second-opinion pass back into the first-pass denial verdicts.

WHY. The first pass over jobs 00-62 ran against a brief that wrongly listed
XPS and Mossbauer as corpus families (see dev/DENIAL_REVIEW_2026-09-05.md).
Every `recover` those batches produced was re-judged against the charter
verbatim, voting `hold` or `overturn`. This applies the overturns.

An overturn rewrites the first-pass verdict to `clean` and records BOTH
opinions in `why`, so the disagreement stays visible in the artifact rather
than being silently resolved. A `hold` leaves the recovery untouched.

    python dev/denial_second_opinion.py --first ~/tmp/denial_review \
        --second ~/tmp/denial_second            # report
    python dev/denial_second_opinion.py ... --apply
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", required=True)
    ap.add_argument("--second", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    second: dict[str, dict] = {}
    for f in sorted(glob.glob(f"{a.second}/out_*.json")):
        try:
            doc = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            print(f"  UNREADABLE {os.path.basename(f)}: {e}")
            continue
        for v in doc.get("verdicts") or []:
            second[str(v.get("key"))] = v
    print(f"second opinions read: {len(second)}")
    if not second:
        print("nothing to fold")
        return 0

    tally: collections.Counter = collections.Counter()
    for f in sorted(glob.glob(f"{a.first}/out_*.json")):
        try:
            doc = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            print(f"  UNREADABLE {os.path.basename(f)}: {e}")
            continue
        changed = False
        for v in doc.get("verdicts") or []:
            if str(v.get("verdict")) != "recover":
                continue
            s = second.get(str(v.get("key")))
            if s is None:
                tally["not_rejudged"] += 1
                continue
            verdict = str(s.get("verdict"))
            tally[verdict] += 1
            if verdict != "overturn":
                continue
            v["verdict"] = "clean"
            v["why"] = (
                f"OVERTURNED on review: {str(s.get('why') or '')[:90]} "
                f"| first pass had said: {str(v.get('why') or '')[:90]}"
            )[:250]
            changed = True
        if changed and a.apply:
            json.dump(doc, open(f, "w"), indent=1, ensure_ascii=False)

    print(
        f"held {tally['hold']}, overturned {tally['overturn']}"
        + (
            f", {tally['not_rejudged']} recoveries had no second opinion "
            "(these came from the corrected-brief batches)"
            if tally["not_rejudged"]
            else ""
        )
        + ("" if a.apply else "   (report only: pass --apply to rewrite)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
