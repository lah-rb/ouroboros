#!/usr/bin/env python3
"""Replay the exact prompt that made laguna emit a tool call, under one config.

Server config is the independent variable, so this runs ONE arm per invocation
and the caller restarts the server between arms:

    uv run python dev/laguna/sampling_sweep.py <arm-label> [reps]

Measures the two behaviours in question:
  - tool_call rate  : does the model emit <tool_call> instead of content?
  - runaway         : does it reason to the max_tokens cap without answering?
Results append to dev/laguna/sweep_results.json.
"""
import json, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).parent
PROMPT = Path("/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
              "5061c6f8-87e3-49d2-a506-1ab58e5b7599/scratchpad/trigger_prompt.txt").read_text()
URL = "http://127.0.0.1:8008/graphql"
Q = ("query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m,temperature:1.0})"
     "{text generatedTokens finished}}")
MAX = 8000   # low enough that a runaway is cheap, high enough for a real answer


def ask():
    body = json.dumps({"query": Q, "variables": {"p": PROMPT, "m": MAX}}).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    t0 = time.time()
    r = json.load(urllib.request.urlopen(req, timeout=900))["data"]["completion"]
    return r, time.time() - t0


def main():
    arm = sys.argv[1] if len(sys.argv) > 1 else "unlabelled"
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    h = json.load(urllib.request.urlopen(urllib.request.Request(
        URL, json.dumps({"query": "{health{status}}"}).encode(),
        {"Content-Type": "application/json"})))
    assert h["data"]["health"]["status"] == "ok"

    rows = []
    for i in range(reps):
        r, wall = ask()
        t = r["text"] or ""
        rows.append({
            "tool_call": "<tool_call>" in t,
            "gen": r["generatedTokens"],
            "finished": r["finished"],
            "chars": len(t),
            "capped": r["generatedTokens"] >= MAX - 8,
            "has_fence": "```" in t,
            "wall_s": round(wall, 1),
        })
        print(f"  rep {i+1}/{reps}: gen={r['generatedTokens']:>5} "
              f"tool_call={rows[-1]['tool_call']!s:5} capped={rows[-1]['capped']!s:5} "
              f"fence={rows[-1]['has_fence']!s:5} {wall:5.1f}s")

    n = len(rows)
    summary = {
        "arm": arm,
        "reps": n,
        "tool_call_rate": round(sum(r["tool_call"] for r in rows) / n, 3),
        "runaway_rate": round(sum(r["capped"] for r in rows) / n, 3),
        "usable_rate": round(sum(r["has_fence"] and not r["tool_call"] for r in rows) / n, 3),
        "median_gen": sorted(r["gen"] for r in rows)[n // 2],
        "rows": rows,
    }
    print(f"\n  ARM {arm}: tool_call={summary['tool_call_rate']:.0%}  "
          f"runaway={summary['runaway_rate']:.0%}  usable={summary['usable_rate']:.0%}  "
          f"median_gen={summary['median_gen']}")

    out = HERE / "sweep_results.json"
    prev = json.loads(out.read_text()) if out.exists() else []
    prev.append(summary)
    out.write_text(json.dumps(prev, indent=2))
    print(f"  -> appended to {out}")


if __name__ == "__main__":
    main()
