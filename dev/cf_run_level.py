#!/usr/bin/env python3
"""Generic counterfactual level-runner: re-run every turn's prompt at the currently-
baked reasoning level, record action + CoT length. Appends to ACTIONS.
Usage: cf_run_level.py <turns.json> <actions.jsonl> <level-name>"""
import json, sys, urllib.request, re, time

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
        cm = re.search(r"analysis<\|message\|>(.*?)(?:<\|end\|>|<\|channel\|>final)", raw, re.DOTALL)
        fm = re.search(r"final<\|message\|>(.*?)(?:<\|end\|>|<\|return\|>|$)", raw, re.DOTALL)
        rec = {"id": t["id"], "level": LEVEL,
               "cot_chars": len(cm.group(1)) if cm else 0,
               "action": (fm.group(1).strip() if fm else raw.strip())[:1200],
               "tokens": d["tokensGenerated"]}
    out.write(json.dumps(rec) + "\n"); out.flush()
    if i % 40 == 0:
        print(f"  {LEVEL}: {i}/{len(turns)} ({time.time()-t0:.0f}s)", flush=True)
out.close()
print(f"  {LEVEL}: done {len(turns)} in {time.time()-t0:.0f}s", flush=True)
