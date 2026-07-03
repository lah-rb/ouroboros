#!/usr/bin/env python3
"""SWE-bench run taxonomy — per-task failure class + inference economy.

Usage: python3 dev/swe_taxonomy.py runs/<run-id> [runs/<run-id> ...]

For each TB1 task directory in the run, reports:
  - grade:  is_resolved / failure_mode (harness verdict)
  - mission: status, goal states (from the preserved ouroboros-mission)
  - economy: cycles, inferences, wall clock, inference share, tokens,
    cache hit rate, and the top flows by inference count (trace summary)
  - clobber: whether the agent dispatched writes at repo scaffolding
    (pyproject/setup/setup.cfg) and whether the grader's pytest choked
    on unrecognized args (the cov-addopts clobber signature)

Feeds the Phase A failure taxonomy that prioritizes the repo-scale work
(see the SWE-bench standup plan). Read-only.
"""

from __future__ import annotations

import glob
import json
import os
import sys

SCAFFOLD = ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini")


def _load(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _latest_summary(agent_logs: str):
    paths = sorted(glob.glob(os.path.join(agent_logs, "ouroboros-mission", "traces", "*.summary.json")))
    return _load(paths[-1]) if paths else None


def _clobber_signals(task_dir: str, mission: dict | None) -> dict:
    touched: list[str] = []
    if mission:
        for d in mission.get("dispatch_history", []) or []:
            tf = str(d.get("target_file_path", "") or "")
            if os.path.basename(tf) in SCAFFOLD:
                touched.append(f"{d.get('flow', '?')}:{tf}")
    pytest_choke = ""
    post = os.path.join(task_dir, "panes", "post-test.txt")
    if os.path.isfile(post):
        try:
            tail = open(post, errors="replace").read()[-4000:]
            for line in tail.splitlines():
                if "unrecognized arguments" in line or "usage: pytest" in line:
                    pytest_choke = line.strip()[:120]
                    break
        except Exception:
            pass
    return {"scaffold_writes": touched, "grader_pytest_choke": pytest_choke}


def analyze_task(task_dir: str) -> dict:
    results = _load(os.path.join(task_dir, "results.json")) or {}
    agent_logs = os.path.join(task_dir, "agent-logs")
    mission = _load(os.path.join(agent_logs, "ouroboros-mission", "mission.json"))
    summary = _latest_summary(agent_logs)

    row: dict = {
        "task": os.path.basename(os.path.dirname(task_dir)),
        "is_resolved": results.get("is_resolved"),
        "failure_mode": results.get("failure_mode"),
    }
    if mission:
        goals = mission.get("goals", []) or []
        row["mission_status"] = mission.get("status")
        row["goals"] = {
            "total": len(goals),
            "complete": sum(1 for g in goals if g.get("status") == "complete"),
            "by_type": {},
        }
        for g in goals:
            t = g.get("type", "?")
            row["goals"]["by_type"][t] = row["goals"]["by_type"].get(t, 0) + 1
    if summary:
        s = summary.get("summary", {})
        counts = s.get("counts", {})
        toks = s.get("tokens", {})
        flows = s.get("flows", {}) or {}
        top = sorted(flows.items(), key=lambda kv: -(kv[1].get("inferences", 0)))[:4]
        row["economy"] = {
            "wall_s": round(s.get("total_wall_ms", 0) / 1000, 1),
            "inference_pct": s.get("time_pct", {}).get("inference"),
            "cycles": counts.get("cycles"),
            "steps": counts.get("steps"),
            "inferences": counts.get("inferences"),
            "generated_tokens": toks.get("generated"),
            "fresh_prefill": toks.get("fresh_prefill"),
            "cache_hit_rate": s.get("cache", {}).get("hit_rate"),
            "top_flows_by_inferences": {
                k: v.get("inferences", 0) for k, v in top if v.get("inferences", 0)
            },
        }
    row["clobber"] = _clobber_signals(task_dir, mission)
    return row


def analyze_run(run_dir: str) -> list[dict]:
    rows = []
    for results_path in sorted(glob.glob(os.path.join(run_dir, "*", "*", "results.json"))):
        rows.append(analyze_task(os.path.dirname(results_path)))
    return rows


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for run_dir in sys.argv[1:]:
        print(f"\n=== {run_dir} ===")
        for row in analyze_run(run_dir):
            print(json.dumps(row, indent=2))


if __name__ == "__main__":
    main()
