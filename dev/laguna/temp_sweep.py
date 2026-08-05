#!/usr/bin/env python3
"""Does a COLD sampler cause the runaway? One prompt, many temperatures.

    uv run python dev/laguna/temp_sweep.py <model-label> [reps]

HYPOTHESIS (Luke, 2026-07-27): the model has a hard time breaking out of a loop
when cold, so a higher temperature floor would rescue it.

WHY THE EXISTING DATA CANNOT ANSWER IT. Every arm of the sampling sweep ran at
temperature 1.0 and never once produced the pathological form. The runaways all
happened inside agent flows, which set temperature MULTIPLICATIVELY —
design_and_plan carries a step at `t*0.1`, replan's decompose steps at `t*0.2`,
defaults `t*0.4`-`t*0.6` — so with a base of 1.0 those steps ran at 0.1-0.6.
And `session_temp_floor` did NOT protect them: it is session-scoped AND
depth-gated ("plain completions keep the requested temperature untouched",
session_manager.py:190), while all four >100k runaways were `mode='non-stream'`.

So cold correlates with runaway and hot does not — across two different prompt
sets. That is suggestive, not causal. This sweep removes the confound by holding
the prompt fixed and moving ONLY temperature.

Temperature is a per-request GraphQL field, so the whole sweep runs against one
loaded model with no restarts.

WHAT COUNTS AS THE FAILURE. Under `thinking: true` the runaway is pure CoT with
zero code fences; under `thinking: false` it becomes content looping (one
observed response had 212 fenced blocks). Both hit the cap. So `capped` is the
comparable signal across modes, and `fences` distinguishes which shape it took.
"""

import json
import statistics as st
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
PROMPT = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "5061c6f8-87e3-49d2-a506-1ab58e5b7599/scratchpad/trigger_prompt.txt"
).read_text()
URL = "http://127.0.0.1:8008/graphql"
Q = (
    "query($p:String!,$m:Int!,$t:Float!){completion(request:{prompt:$p,maxTokens:$m,temperature:$t})"
    "{text generatedTokens finished}}"
)
MAX = 8000
# 0.1 and 0.2 are the real step temperatures; 0.35 is session_temp_floor (what a
# global floor would give); 1.0 is where every prior arm ran.
TEMPS = [0.1, 0.2, 0.35, 0.6, 1.0]


def ask(temp):
    body = json.dumps(
        {"query": Q, "variables": {"p": PROMPT, "m": MAX, "t": temp}}
    ).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=900))["data"]["completion"]
    return r, time.time() - t0


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "unlabelled"
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    out = HERE / "temp_sweep_results.json"
    prev = json.loads(out.read_text()) if out.exists() else []

    print(f"model={label}  reps={reps}  max_tokens={MAX}\n")
    print(
        f"{'temp':>6} {'capped':>8} {'median gen':>11} {'median fences':>14} {'tool_call':>10}"
    )
    print("-" * 54)
    for temp in TEMPS:
        rows = []
        for _ in range(reps):
            r, wall = ask(temp)
            t = r["text"] or ""
            rows.append(
                {
                    "temp": temp,
                    "gen": r["generatedTokens"],
                    "capped": r["generatedTokens"] >= MAX - 8,
                    "fences": t.count("```") // 2,
                    "tool_call": "<tool_call>" in t,
                    "chars": len(t),
                    "wall_s": round(wall, 1),
                }
            )
        capped = sum(r["capped"] for r in rows) / len(rows)
        print(
            f"{temp:>6.2f} {capped:>7.0%} {st.median(r['gen'] for r in rows):>11.0f} "
            f"{st.median(r['fences'] for r in rows):>14.0f} "
            f"{sum(r['tool_call'] for r in rows)/len(rows):>9.0%}"
        )
        prev.append(
            {
                "model": label,
                "temp": temp,
                "reps": reps,
                "capped_rate": round(capped, 3),
                "rows": rows,
            }
        )
        out.write_text(json.dumps(prev, indent=2))

    print(f"\n-> {out}")
    print(
        "READ: if cold locks the sampler, `capped` falls monotonically as temp rises."
    )
    print("     A flat row across temperatures REFUTES the floor as a lever.")


if __name__ == "__main__":
    main()
