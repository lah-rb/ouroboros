#!/usr/bin/env python3
"""Generic counterfactual level-runner: re-run every turn's prompt at the currently-
baked reasoning level, record action + CoT length. Appends to ACTIONS.
Usage: cf_run_level.py <turns.json> <actions.jsonl> <level-name>"""
import json
import sys
import time
import urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cf_extract import extract as _extract

TURNS, ACTIONS, LEVEL = sys.argv[1], sys.argv[2], sys.argv[3]
turns = json.load(open(TURNS))
out = open(ACTIONS, "a")

def rc(prompt, temp=0.3, mt=2500):
    body = json.dumps({
        "query": "query($r: CompletionRequest!){ rawCompletion(request:$r){ rawText tokensGenerated } }",
        "variables": {"r": {"prompt": prompt, "maxTokens": mt, "temperature": temp}},
    }).encode()
    try:
        d = json.load(urllib.request.urlopen(urllib.request.Request(
            "http://localhost:8008/graphql", body, {"Content-Type": "application/json"}), timeout=300))
    except Exception as e:
        return None, str(e)[:90]
    if d.get("errors"):
        return None, str(d["errors"][:1])[:90]
    return d["data"]["rawCompletion"], None

t0 = time.time()
for i, t in enumerate(turns):
    d, err = rc(t["prompt"])
    if err:
        rec = {"id": t["id"], "level": LEVEL, "error": err}
    else:
        raw = d["rawText"]
        # Extraction lives in cf_extract now. The line that used to be here
        # fell back to `raw.strip()[:1200]` when the final-channel regex
        # missed — and it missed on every <|constrain|>json action — so
        # truncated CoT was stored as the agent's action and judged as one.
        # That single line is the root cause of the adaptive_thinking
        # high-class collapse (dev/ADAPTIVE_THINKING_STATUS.md).
        ex = _extract(raw, d.get("finished", True))
        rec = {"id": t["id"], "level": LEVEL,
               "cot_chars": ex["cot_chars"],
               "action": ex["action"],      # None when unusable — NEVER the CoT
               "usable": ex["usable"], "no_final": ex["no_final"],
               "truncated": ex["truncated"],
               "tokens": d["tokensGenerated"]}
    out.write(json.dumps(rec) + "\n"); out.flush()
    if i % 40 == 0:
        print(f"  {LEVEL}: {i}/{len(turns)} ({time.time()-t0:.0f}s)", flush=True)
out.close()
print(f"  {LEVEL}: done {len(turns)} in {time.time()-t0:.0f}s", flush=True)
