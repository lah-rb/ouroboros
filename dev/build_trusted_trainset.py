#!/usr/bin/env python3
"""Materialize the ModernBERT training set from the trusted-label manifest
(dev/trusted_labels_v1.json, JUDGE_STANDARD v1.0 retro-gate) by joining each
record back to its source turn context. Row shape matches the prior
train_dataset.jsonl: {text, label, task, source, uid}.

-> dev/train_dataset_trusted_v1.jsonl
"""
import json

CONTEXT_SOURCES = {
    "phaseB": ("dev/phaseB_labeling.json", "context"),
    "phaseC": ("dev/phaseC_labeling.json", "context"),
    "tb1_cf": ("dev/tb1_cf_labeling.json", "context"),
    "grow": ("dev/grow_panel_input.json", "context"),
    "v3": ("dev/clean_turns_v3.json", "prompt"),
}

manifest = json.load(open("dev/trusted_labels_v1.json"))
records = manifest["records"]
ctx = {}
for src, (path, field) in CONTEXT_SOURCES.items():
    ctx[src] = {r["id"]: r[field] for r in json.load(open(path))}

rows, misses = [], []
for r in records:
    text = ctx.get(r["source"], {}).get(r["orig_id"])
    if not text:
        misses.append(r["uid"])
        continue
    rows.append({"text": text, "label": r["label"], "task": r["task"],
                 "source": r["source"], "uid": r["uid"]})

assert not misses, f"unjoinable uids: {misses[:5]} (+{len(misses)-5} more)"
with open("dev/train_dataset_trusted_v1.jsonl", "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")

from collections import Counter
print(f"wrote dev/train_dataset_trusted_v1.jsonl: {len(rows)} rows "
      f"{dict(Counter(r['label'] for r in rows))} across "
      f"{len(set(r['task'] for r in rows))} tasks")
lens = sorted(len(r["text"]) for r in rows)
print(f"text chars: min {lens[0]} median {lens[len(lens)//2]} max {lens[-1]}")
