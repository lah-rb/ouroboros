#!/usr/bin/env python3
"""tau-bench adapter live smoke: gold-action replay through the REAL env.

Per task: construct the env (fires the user simulator's opening message on
the LLMVP shim — validates the litellm→shim wiring), execute the task's
gold tool actions against the episode DB, synthesize the required-outputs
message, grade with the official calculate_reward. Expected reward 1.0 per
task; anything less = adapter plumbing bug, not model failure.

Usage: .venv/bin/python dev/tau_gold_replay.py [domain=retail] [n_tasks=3]
Needs the LLMVP server up (shim on :8008).
"""

from __future__ import annotations

import json
import sys
import time


def main() -> int:
    domain = sys.argv[1] if len(sys.argv) > 1 else "retail"
    n_tasks = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    from adapters.tau.env import make_env
    from adapters.tau.runner import replay_gold

    results = []
    for idx in range(n_tasks):
        t0 = time.time()
        env = make_env(domain, task_index=idx)
        opening = env.reset(task_index=idx).observation
        res = replay_gold(env, idx)
        results.append(
            {
                "task": idx,
                "reward": res.reward,
                "gold_tool_calls": res.steps,
                "user_opening": opening[:110],
                "wall_s": round(time.time() - t0, 1),
            }
        )
        print(json.dumps(results[-1]), flush=True)

    passed = sum(1 for r in results if r["reward"] == 1.0)
    print(f"\nGOLD REPLAY: {passed}/{len(results)} tasks at reward 1.0 [{domain}]")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
