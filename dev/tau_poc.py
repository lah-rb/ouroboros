#!/usr/bin/env python3
"""τ piece-4 POC: control-inversion episodes on retail.

Runs N retail episodes (boss persona → ops mission → tau tools → user_sim
persona), writes per-episode artifacts, prints a summary. The bar is
layers-exercised, not score (see the plan's success criteria).

Usage: PYTHONPATH=<repo> .venv/bin/python dev/tau_poc.py [n=3] [max_turns=8]
Needs the LLMVP server up on the gpt-oss-120b-a5-tau config (boss + user_sim
personas). Restore the production a5 config afterwards.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    max_turns = int(sys.argv[2]) if len(sys.argv) > 2 else 8

    from tau_adapter.episode import run_tau_episode

    out_dir = os.path.join(_REPO, "runs", "tau_poc",
                           time.strftime("%Y%m%dT%H%M%S"))
    os.makedirs(out_dir, exist_ok=True)

    rows = []
    for idx in range(n):
        t0 = time.time()
        print(f"\n═══ retail task {idx} ═══", flush=True)
        try:
            art = run_tau_episode("retail", idx, max_turns=max_turns)
        except Exception as e:  # noqa: BLE001 — POC must survive one bad episode
            print(f"  EPISODE CRASHED: {e}", flush=True)
            rows.append({"task": idx, "crashed": str(e)})
            continue
        wall = round(time.time() - t0, 1)
        d = dataclasses.asdict(art)
        d["wall_s"] = wall
        with open(os.path.join(out_dir, f"task_{idx}.json"), "w") as f:
            json.dump(d, f, indent=1)
        rows.append({
            "task": idx, "reward": art.reward, "termination": art.termination,
            "turns": art.turns, "tool_calls": art.tool_calls,
            "boss_fallbacks": art.boss_fallbacks,
            "worker_failures": art.worker_failures, "wall_s": wall,
        })
        print(json.dumps(rows[-1]), flush=True)

    print(f"\n═══ POC summary ({out_dir}) ═══", flush=True)
    for r in rows:
        print(json.dumps(r))
    graded = [r for r in rows if "reward" in r]
    if graded:
        solved = sum(1 for r in graded if r["reward"] == 1.0)
        total_fb = sum(r["boss_fallbacks"] for r in graded)
        total_dec = sum(r["turns"] for r in graded)
        print(f"\nreward 1.0: {solved}/{len(graded)} | boss fallback rate: "
              f"{total_fb}/{total_dec} decisions | "
              f"user_stops: {sum(1 for r in graded if r['termination']=='user_stop')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
