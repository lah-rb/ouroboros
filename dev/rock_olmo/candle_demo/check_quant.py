#!/usr/bin/env python3
"""Score quantized candle generations against the bf16 references (rock venv).

  ./.venv/bin/python candle_demo/check_quant.py --probes candle_demo/validation.json \\
      --run ~/tmp/analysis/candle_demo/validation_v3l_q6k.json --model v3l

Per pair: HITs for bf16 and for the quantized run (the probes' own rules, build_probes.judge),
how many verdicts agree, and how many generations are identical text.
With --record, the run's generations are stored in the probes file as `recorded[<model>]`
(the page shows them when no server is reachable).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import probe_chains as pc  # noqa: E402
from build_probes import judge  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probes", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--record", action="store_true")
    args = ap.parse_args()
    doc = json.load(open(args.probes))
    run = json.load(open(os.path.expanduser(args.run)))
    gens = {r["id"]: r["gen"] for r in run["rows"]}
    tally = collections.defaultdict(lambda: collections.Counter())
    changed = []
    for p in doc["probes"]:
        g = pc.load_gold(p["species"])
        ref = p["reference"][args.model]
        text = gens[p["id"]]["text"]
        ans, status = judge(p["pair"], text, g)
        t = tally[p["pair"]]
        t["n"] += 1
        t["hit_bf16"] += ref["status"] == "HIT"
        t["hit_quant"] += status == "HIT"
        t["same_verdict"] += status == ref["status"]
        t["same_text"] += text == ref["text"]
        if status != ref["status"]:
            changed.append((p["id"], ref["status"], ref["answer"][:50], status, ans[:50]))
        if args.record:
            p.setdefault("recorded", {})[args.model] = {"text": text, "answer": ans, "status": status,
                                                         "quant": run["quant"], "device": run["device"]}
    print(f"{run['model']} ({run['quant']}, {run['device']}) vs bf16 reference")
    for pair, t in tally.items():
        print(f"  {pair:18s} bf16 {t['hit_bf16']}/{t['n']}  quant {t['hit_quant']}/{t['n']}  "
              f"same verdict {t['same_verdict']}/{t['n']}  identical text {t['same_text']}/{t['n']}")
    for c in changed:
        print("  changed:", c)
    if args.record:
        json.dump(doc, open(args.probes, "w"), ensure_ascii=False, indent=1)
        print(f"recorded {args.model} generations into {args.probes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
