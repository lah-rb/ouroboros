#!/usr/bin/env python3
"""Canary probe — correlate the ops "no-task" confusion with server health.

Run this ALONGSIDE a live TB2/Harbor run (separate terminal). Every --interval
seconds it fires a fragile-step prompt (a derive_criteria-style verification
planner with a concrete task), checks whether the model BINDS to the task or
falls into the "we haven't been given a task / wait for next step" confusion,
and snapshots the deep-health endpoint (memory residency, KV-cache churn/
fallbacks, throughput drift — the pass-1/2 fields). It appends one JSONL row per
probe and LOUDLY flags the first tick where the canary confuses or throughput
drifts, printing the health state at that exact moment.

That converts "the server degraded somehow over the run" into a named,
timestamped cause: e.g. "confused at +4h12m, when mem_system_available_mb had
dropped 38GB->2GB and flow_fallbacks ticked 0->3" (the Apple-Silicon unified-
memory KV/weight eviction signature) — or rules it out if health stays flat.

Usage:
  python3 dev/canary_probe.py --interval 60 --out /tmp/canary.jsonl
  (Ctrl-C to stop; or --duration SECONDS.)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# A compact, representative derive_criteria-style prompt: a verification planner
# over a concrete task with two named deliverables. A healthy server emits valid
# JSON checks covering both; a degraded one falls into the "no task" confusion.
CANARY_PROMPT = """You define the DONE criteria for the task in the ---YOUR TASK--- block
below — the shell checks that will later confirm it is complete. Your output is
parsed as JSON. Return ONLY a JSON object inside a fenced code block.

---YOUR TASK---
Merge user data from /data/source_a/users.json (primary) and
/data/source_b/users.csv into a unified dataset. Write the merged result to
/app/merged_users.parquet and a conflict report to /app/conflicts.json.
---END TASK---

Working directory: /app

Produce a "checks" array of robust shell checks that inspect the FINAL state
(files exist and are non-empty). Return ONLY the fenced JSON object, e.g.
```json
{"checks": [{"command": "test -s /app/x", "description": "x exists"}]}
```"""

CONFUSED = re.compile(
    r"haven.?t been given|no (specific|concrete|actual) (task|question|request)"
    r"|wait for (the )?next|don.?t know what the|hasn.?t given|unspecified task"
    r"|no further prompt|no action yet|we are stuck|need more info",
    re.I,
)

HEALTH_FIELDS = [
    "status", "memProcessRssMb", "memSystemUsedPercent", "memSystemAvailableMb",
    "memSystemWiredMb", "flowCacheEntries", "residentActive", "flowBuilds",
    "flowHits", "flowEvicts", "flowFallbacks", "runawayCaptures",
    "trendSamples", "decodeTpsRecent", "decodeTpsBaseline", "throughputDrift",
    "ttftRecentS",
]


async def fetch_health(client: httpx.AsyncClient, endpoint: str) -> dict:
    q = "{ health { " + " ".join(HEALTH_FIELDS) + " } }"
    try:
        r = await client.post(endpoint, json={"query": q}, timeout=10)
        return r.json().get("data", {}).get("health", {}) or {}
    except Exception as e:
        return {"_health_error": str(e)}


async def fire_canary(endpoint: str, temp: float) -> dict:
    """Fire the canary prompt, return {confused, valid, covered, error}."""
    from agent.effects.inference import InferenceEffect

    eff = InferenceEffect(endpoint)
    try:
        res = await eff.run_inference(
            prompt=CANARY_PROMPT,
            config_overrides={"max_tokens": 600, "temperature": temp},
        )
    except Exception as e:
        return {"error": f"inference: {e}"}
    try:
        think = await eff.fetch_thinking()
    except Exception:
        think = ""
    text = res.text or ""
    confused = bool(CONFUSED.search(think or ""))
    valid = covered = False
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    raw = m.group(1) if m else text
    try:
        d = json.loads(raw)
        checks = d.get("checks", d if isinstance(d, list) else [])
        valid = True
        blob = json.dumps(checks)
        covered = "merged_users.parquet" in blob and "conflicts.json" in blob
    except Exception:
        pass
    return {"confused": confused, "valid": valid, "covered": covered,
            "tokens_out": getattr(res, "generated_tokens", 0) or 0}


def _alert(reason: str, row: dict) -> None:
    h = row.get("health", {})
    print("\n" + "!" * 70)
    print(f"!! CANARY ALERT @ {row['iso']} (+{row['elapsed_s']:.0f}s): {reason}")
    print(f"!!   mem avail={h.get('memSystemAvailableMb')}MB used%={h.get('memSystemUsedPercent')} "
          f"wired={h.get('memSystemWiredMb')}MB rss={h.get('memProcessRssMb')}MB")
    print(f"!!   flow fallbacks={h.get('flowFallbacks')} evicts={h.get('flowEvicts')} "
          f"runaways={h.get('runawayCaptures')}")
    print(f"!!   throughput_drift={h.get('throughputDrift')} decode_tps={h.get('decodeTpsRecent')}/"
          f"{h.get('decodeTpsBaseline')} ttft={h.get('ttftRecentS')}s")
    print("!" * 70 + "\n")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8008/graphql")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--duration", type=float, default=0.0, help="0 = until Ctrl-C")
    ap.add_argument("--temp", type=float, default=0.0,
                    help="canary temp (0.0 matches the run that broke)")
    ap.add_argument("--out", default="/tmp/canary_probe.jsonl")
    ap.add_argument("--drift-floor", type=float, default=0.7,
                    help="alert when throughput_drift falls below this")
    args = ap.parse_args()

    t0 = time.monotonic()
    f = open(args.out, "a", buffering=1)
    print(f"canary: probing {args.endpoint} every {args.interval}s @ temp {args.temp}; "
          f"log -> {args.out}\n"
          f"{'time':<9} {'conf':<5} {'valid':<5} {'cov':<4} {'availMB':>8} {'used%':>6} "
          f"{'wired':>7} {'fbk':>4} {'drift':>6} {'dtps':>6}")
    prev_fallbacks = None
    confused_flagged = False
    async with httpx.AsyncClient() as client:
        while True:
            elapsed = time.monotonic() - t0
            health = await fetch_health(client, args.endpoint)
            canary = await fire_canary(args.endpoint, args.temp)
            row = {
                "ts": time.time(),
                "iso": datetime.now(timezone.utc).isoformat(),
                "elapsed_s": round(elapsed, 1),
                **canary,
                "health": health,
            }
            f.write(json.dumps(row) + "\n")

            h = health
            conf = canary.get("confused")
            drift = h.get("throughputDrift")
            fbk = h.get("flowFallbacks")
            print(f"{elapsed:>7.0f}s  {str(conf):<5} {str(canary.get('valid')):<5} "
                  f"{str(canary.get('covered')):<4} {str(h.get('memSystemAvailableMb')):>8} "
                  f"{str(h.get('memSystemUsedPercent')):>6} {str(h.get('memSystemWiredMb')):>7} "
                  f"{str(fbk):>4} {str(drift):>6} {str(h.get('decodeTpsRecent')):>6}")

            # Loud alerts on the meaningful transitions.
            if conf and not confused_flagged:
                confused_flagged = True
                _alert("canary CONFUSED — model lost task binding", row)
            if prev_fallbacks is not None and fbk is not None and fbk > prev_fallbacks:
                _alert(f"flow_fallbacks rose {prev_fallbacks}->{fbk} (KV-cache instability)", row)
            if drift is not None and drift < args.drift_floor:
                _alert(f"throughput_drift {drift} < {args.drift_floor} (generation slowdown)", row)
            prev_fallbacks = fbk if fbk is not None else prev_fallbacks

            if args.duration and elapsed >= args.duration:
                break
            await asyncio.sleep(args.interval)
    print(f"\ncanary: done ({round(time.monotonic()-t0)}s). log: {args.out}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ncanary: stopped.")
