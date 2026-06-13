#!/usr/bin/env python3
"""Compare runtime traces across A/B arms — session-mode cost analysis.

Built for the session_full_replay A/B: same mission state + same added
directive, gpt-oss with save/load KV state vs full-replay. The cost of
full-replay is re-prefilling the accumulated session transcript each
turn, so the decisive signal is tokens_in (prefill) on multi-turn
session steps — robust even when the two arms take divergent paths.

Reads an A/B dir with ``arm-<name>/`` subdirs, each holding:
  trace.jsonl   runtime trace (cycle_end + inference_call events)
  run.log       agent stdout (optional — for save_state error counts)
  meta.json     {"start_epoch", "end_epoch", "exit_code", "mode"} (optional)

Usage:
    python dev/ab_trace_compare.py ~/ouroboros-overnight/<date>-ab
    python dev/ab_trace_compare.py --single path/to/trace.jsonl   # validate
"""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

# Inference steps that run INSIDE multi-turn sessions (interact /
# run_session / quality-gate UX). These accumulate token_history, so
# full-replay re-prefills them and tokens_in climbs with turn index;
# save/load keeps tokens_in flat (only the new turn). One-shot steps
# (design, diagnose, single-turn judges) are unaffected by session mode.
SESSION_STEPS = {
    "plan_interaction",
    "evaluate_outcome",
    "evaluate_ux_session",
    "judge_finding",
}


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def load_arm(arm_dir: Path) -> dict:
    trace = arm_dir / "trace.jsonl"
    cyc_ms: list[float] = []
    inf: list[dict] = []
    for line in trace.open():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = d.get("event_type")
        if t == "cycle_end" and d.get("cycle_duration_ms"):
            cyc_ms.append(float(d["cycle_duration_ms"]))
        elif t == "inference_call":
            inf.append(d)

    meta = {}
    mp = arm_dir / "meta.json"
    if mp.is_file():
        meta = json.loads(mp.read_text())

    log = arm_dir / "run.log"
    save_state_errs = 0
    if log.is_file():
        save_state_errs = log.read_text(errors="ignore").count("Negative size passed")

    tin = [float(x.get("tokens_in") or 0) for x in inf]
    tout = [float(x.get("tokens_out") or 0) for x in inf]
    wall = [float(x.get("wall_ms") or 0) for x in inf]

    # Session-step prefill: the full-replay cost concentrator.
    sess = [x for x in inf if x.get("step") in SESSION_STEPS]
    sess_tin = [float(x.get("tokens_in") or 0) for x in sess]
    sess_wall = [float(x.get("wall_ms") or 0) for x in sess]

    total_wall_s = None
    if meta.get("start_epoch") and meta.get("end_epoch"):
        total_wall_s = float(meta["end_epoch"]) - float(meta["start_epoch"])

    return {
        "name": arm_dir.name.replace("arm-", ""),
        "mode": meta.get("mode", "?"),
        "exit_code": meta.get("exit_code"),
        "total_wall_s": total_wall_s,
        "cycles": len(cyc_ms),
        "cycle_ms": cyc_ms,
        "inferences": len(inf),
        "tokens_in": sum(tin),
        "tokens_out": sum(tout),
        "infer_wall_s": sum(wall) / 1000.0,
        "sess_infer": len(sess),
        "sess_tin": sess_tin,
        "sess_wall": sess_wall,
        "save_state_errs": save_state_errs,
    }


def _fmt(v, unit=""):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.1f}{unit}"
    return f"{v:,}{unit}"


def print_report(arms: list[dict]) -> None:
    labels = [a["name"] for a in arms]
    w = max(22, *(len(x) for x in labels)) + 2

    def row(label, key, fn=lambda a: a, unit=""):
        cells = []
        for a in arms:
            try:
                cells.append(_fmt(fn(a), unit))
            except Exception:
                cells.append("n/a")
        print(f"  {label:<34}" + "".join(f"{c:>{w}}" for c in cells))

    print("\n" + "=" * (36 + w * len(arms)))
    print(f"  {'metric':<34}" + "".join(f"{n:>{w}}" for n in labels))
    print("=" * (36 + w * len(arms)))

    row("session mode", None, lambda a: a["mode"])
    row("exit code", None, lambda a: a["exit_code"])
    print("  " + "-" * (34 + w * len(arms)))
    row("total wall (s)", None, lambda a: a["total_wall_s"])
    row("work cycles", None, lambda a: a["cycles"])
    row("  wall / cycle (s)", None,
        lambda a: (a["total_wall_s"] / a["cycles"]) if a["total_wall_s"] and a["cycles"] else None)
    row("  cycle dur mean (ms)", None, lambda a: st.mean(a["cycle_ms"]) if a["cycle_ms"] else None)
    row("  cycle dur median (ms)", None, lambda a: st.median(a["cycle_ms"]) if a["cycle_ms"] else None)
    row("  cycle dur p90 (ms)", None, lambda a: _pct(a["cycle_ms"], 90))
    print("  " + "-" * (34 + w * len(arms)))
    row("inferences", None, lambda a: a["inferences"])
    row("tokens_in (prefill) TOTAL", None, lambda a: a["tokens_in"])
    row("tokens_out TOTAL", None, lambda a: a["tokens_out"])
    row("prefill ratio (in/out)", None,
        lambda a: (a["tokens_in"] / a["tokens_out"]) if a["tokens_out"] else None)
    row("inference wall TOTAL (s)", None, lambda a: a["infer_wall_s"])
    print("  " + "-" * (34 + w * len(arms)))
    print("  session-step inferences (the full-replay cost concentrator):")
    row("  session inferences", None, lambda a: a["sess_infer"])
    row("  tokens_in / sess-infer MEAN", None, lambda a: st.mean(a["sess_tin"]) if a["sess_tin"] else None)
    row("  tokens_in / sess-infer P90", None, lambda a: _pct(a["sess_tin"], 90))
    row("  wall_ms / sess-infer MEAN", None, lambda a: st.mean(a["sess_wall"]) if a["sess_wall"] else None)
    print("  " + "-" * (34 + w * len(arms)))
    row("save_state overflow errors", None, lambda a: a["save_state_errs"])
    print("=" * (36 + w * len(arms)))

    if len(arms) == 2:
        a, b = arms
        print("\n  Δ (B vs A):")
        def delta(label, va, vb, unit=""):
            if va in (None, 0) or vb is None:
                print(f"    {label}: n/a")
                return
            print(f"    {label}: {vb - va:+,.1f}{unit}  ({(vb/va - 1)*100:+.1f}%)")
        delta("total wall (s)", a["total_wall_s"], b["total_wall_s"])
        if a["cycles"] and b["cycles"] and a["total_wall_s"] and b["total_wall_s"]:
            delta("wall / cycle (s)", a["total_wall_s"]/a["cycles"], b["total_wall_s"]/b["cycles"])
        delta("tokens_in TOTAL", a["tokens_in"], b["tokens_in"])
        if a["sess_tin"] and b["sess_tin"]:
            delta("tokens_in / sess-infer MEAN", st.mean(a["sess_tin"]), st.mean(b["sess_tin"]))
        if a["sess_wall"] and b["sess_wall"]:
            delta("wall_ms / sess-infer MEAN", st.mean(a["sess_wall"]), st.mean(b["sess_wall"]))


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--single":
        # Validation mode: treat one trace as a single arm.
        import tempfile
        import shutil

        tmp = Path(tempfile.mkdtemp())
        (tmp / "arm-single").mkdir()
        shutil.copy(sys.argv[2], tmp / "arm-single" / "trace.jsonl")
        print_report([load_arm(tmp / "arm-single")])
        return

    ab_dir = Path(sys.argv[1])
    arm_dirs = sorted(d for d in ab_dir.glob("arm-*") if d.is_dir())
    if not arm_dirs:
        print(f"No arm-*/ subdirs in {ab_dir}")
        sys.exit(1)
    print_report([load_arm(d) for d in arm_dirs])


if __name__ == "__main__":
    main()
