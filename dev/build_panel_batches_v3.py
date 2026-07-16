#!/usr/bin/env python3
"""Build Opus-panel super-batches (500 turns each) from the v3 cleaned turns + their
low/med/high counterfactual.

Each turn carries the decision PROMPT + the ACTION the model took at low / medium /
high reasoning effort (+ cot_chars, tokens = how much it reasoned). The panel judges
the MINIMAL-SUFFICIENT level per turn from this. Only turns with all three levels
present are included.  ->  dev/clean_panel_v3/batch_NN.json  (500 turns/super-batch;
the panel Workflow fans out smaller judge sub-slices x 3 votes within each).

Run AFTER the counterfactual completes (clean_actions_v3.jsonl has 2124x3 records).
"""
import glob
import json
import os
import statistics

BATCH = 500
OUT = "dev/clean_panel_v3"
os.makedirs(OUT, exist_ok=True)

turns = {t["id"]: t for t in json.load(open("dev/clean_turns_v3.json"))}
acts = {}
for line in open("dev/clean_actions_v3.jsonl"):
    line = line.strip()
    if not line:
        continue
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("error"):
        continue
    acts.setdefault(r["id"], {})[r.get("level")] = {
        "action": r.get("action", ""),
        "cot_chars": r.get("cot_chars", 0),
        "tokens": r.get("tokens", 0),
    }

rows, skipped = [], 0
for tid, t in turns.items():
    lv = acts.get(tid, {})
    if not all(k in lv for k in ("low", "medium", "high")):
        skipped += 1
        continue
    rows.append({"id": tid, "task": t["task"], "prompt": t["prompt"], "actions": lv})

# clear any stale batch files, then write fresh 500-turn super-batches
for f in glob.glob(f"{OUT}/batch_*.json"):
    os.remove(f)
n = 0
for i in range(0, len(rows), BATCH):
    json.dump(
        {"batch": n, "turns": rows[i:i + BATCH]},
        open(f"{OUT}/batch_{n:02d}.json", "w"),
        indent=1,
    )
    n += 1

chars = [len(r["prompt"]) + sum(len(r["actions"][k]["action"]) for k in r["actions"]) for r in rows]
avg = statistics.mean(chars) if chars else 0
print(f"complete turns: {len(rows)} (skipped {skipped} missing a level)")
print(f"super-batches: {n} ({BATCH}/batch) -> {OUT}/")
print(f"~per-turn input: {avg:,.0f} chars (~{avg / 4:,.0f} tok)")
