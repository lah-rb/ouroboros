#!/usr/bin/env python3
"""Per-run yield + souring signal for the mission marathon.

Extracted from an inline heredoc: bash brace-expanded the Python dict literal
`{"slug":SLUG,...}` (a `{a,b,c}` list) when the heredoc was nested inside a
`< <(...)` process substitution, splitting it into per-key SyntaxErrors that killed
the marathon loop after run #1. As a standalone arg-driven file there is no heredoc
and no brace-expansion surface — and it's independently testable.

  argv: <working_dir> <stub_threshold> <slug>
  stdout: "<goals>,<gate>,<complete>,<highcand> <stub_rate>"   (two fields: stats, stub)
  side effect: writes <W>/.agent/.health  {slug, stub_rate, rewrites, state}
"""
import json, glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.trace_health import classify  # single source of truth for the stub shape

W, THR, SLUG = sys.argv[1], float(sys.argv[2]), sys.argv[3]
m = json.load(open(os.path.join(W, ".agent", "mission.json")))
g = m.get("goals", [])

hc = rw = st = 0
fs = glob.glob(os.path.join(W, ".agent", "traces", "*.jsonl"))
if fs:
    T = sorted(fs, key=os.path.getmtime)[-1]
    for line in open(T, errors="ignore"):
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("event_type") != "inference_call":
            continue
        if (e.get("generated_tokens") or 0) >= 674:
            hc += 1
        if e.get("step") == "generate_rewrite":
            rw += 1
            resp = e.get("response_content") or ""
            is_stub = (classify(resp) == "stub") if resp else ((e.get("generated_tokens") or 999) < 250)
            if is_stub:
                st += 1

sr = round(st / rw, 3) if rw else 0.0
gate = sum(1 for x in g if x.get("origin") == "quality_gate")
comp = sum(1 for x in g if x.get("status") == "complete")
state = "soured" if (rw >= 4 and sr >= THR) else "clean"

health = {"slug": SLUG, "stub_rate": sr, "rewrites": rw, "state": state}
open(os.path.join(W, ".agent", ".health"), "w").write(json.dumps(health))
print("%d,%d,%d,%d %s" % (len(g), gate, comp, hc, sr))
