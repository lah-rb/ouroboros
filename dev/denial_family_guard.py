#!/usr/bin/env python3
"""Downgrade re-judged recoveries that rest only on an out-of-family technique.

WHY THIS EXISTS. The sub-agent brief used to re-judge the denial backlog
(dev/review_denials_via_agents.py) named XPS and Mossbauer as corpus families.
They are NOT: the mission objective lists laser/spark atomic emission, Raman
and its infrared counterparts, X-ray diffraction, UV-Vis-NIR and reflectance,
and the close neighbours XRF, EDS/EPMA, photoluminescence and
cathodoluminescence -- and nothing else. Agents working from the wrong brief
recovered papers whose only in-scope-looking technique was out of family, and
those denials were correct as written.

The guard is deterministic and reads the curator's own summary, not the
agent's reasoning: a `recover` survives only if that summary names a technique
the charter actually covers. It is written as a separate pass rather than
folded into `book` because it must be auditable -- every downgrade is printed
with the sentence that decided it.

    python dev/denial_family_guard.py --in ~/tmp/denial_review          # report
    python dev/denial_family_guard.py --in ~/tmp/denial_review --apply  # rewrite
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re

# Straight from the mission objective. Keep these two lists in the same order
# as the charter sentence so a future edit can be diffed against it.
IN_FAMILY = (
    r"\bLIBS\b|spark[- ]induced|\bSIBS\b|optical emission|\bOES\b"
    r"|\bRaman\b|\bSERS\b|\bFTIR\b|\bATR\b|\bDRIFTS?\b|infrared|\bIR spectr"
    r"|\bXRD\b|diffractogram|diffraction pattern|X-ray diffraction|Rietveld"
    r"|d-spacing|\bhkl\b|lattice parameter"
    r"|UV-?\s?Vis|diffuse reflectance|band ?gap|\bVNIR\b|\bSWIR\b|hyperspectral"
    r"|reflectance spectr"
    r"|\bXRF\b|\bpXRF\b|\bEDS\b|\bEDX\b|\bEDAX\b|\bEPMA\b|electron microprobe"
    r"|photoluminescen|cathodoluminescen"
)
# Named only so the report can say WHY a paper looked in-scope but is not.
OUT_FAMILY = (
    r"\bXPS\b|photoelectron|M[oö]ssbauer|neutron diffraction|\bEXAFS\b"
    r"|\bXANES\b|\bXMCD\b|\bNMR\b|atomic absorption|\bAAS\b|\bICP-MS\b"
    r"|isotope[- ]ratio|gamma spectrometry|\bDLS\b"
)
_IN = re.compile(IN_FAMILY, re.I)
_OUT = re.compile(OUT_FAMILY, re.I)


def _evidence(job: dict) -> str:
    return f"{job.get('curator_summary') or ''} " + " ".join(
        str(i) for i in (job.get("curator_issues") or [])
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    jobs: dict[str, dict] = {}
    for f in glob.glob(f"{a.inp}/job_*.json"):
        for r in json.load(open(f)):
            jobs[r["key"]] = r

    tally: collections.Counter = collections.Counter()
    for f in sorted(glob.glob(f"{a.inp}/out_*.json")):
        try:
            doc = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            print(f"  UNREADABLE {os.path.basename(f)}: {e}")
            continue
        changed = False
        for v in doc.get("verdicts") or []:
            if str(v.get("verdict")) != "recover":
                continue
            job = jobs.get(str(v.get("key")))
            if job is None:
                continue
            ev = _evidence(job)
            tally["recover_seen"] += 1
            if _IN.search(ev):
                continue
            v["verdict"] = "clean"
            v["why"] = (
                "GUARD: summary names no charter technique"
                + (" (out-of-family only)" if _OUT.search(ev) else "")
                + f" | agent said: {str(v.get('why') or '')[:80]}"
            )[:200]
            changed = True
            tally["downgraded"] += 1
            print(f"  downgraded {str(v['key'])[:46]:<48} {ev[:100]}")
        if changed and a.apply:
            json.dump(doc, open(f, "w"), indent=1, ensure_ascii=False)
    print(
        f"\n{tally['recover_seen']} recoveries checked, "
        f"{tally['downgraded']} downgraded to clean"
        + ("" if a.apply else "   (report only: pass --apply to rewrite)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
