#!/usr/bin/env python3
"""Extract + clean a panel Workflow's labels from its task .output file.

Usage: extract_panel_result.py <task_output_file> <batch_dir>

The .output file is a wrapper {summary, agentCount, logs, result, ...} where
"result" is the workflow return — possibly a JSON *string* needing a second parse.
We pull result.labels, filter to the batch's manifest ids (drop any hallucinated
id), dedup, and write <batch_dir>/result.json = {"labels":[...]}. Prints coverage.
"""

import json
import os
import sys

out_file, batch_dir = sys.argv[1], sys.argv[2]
raw = open(out_file).read()
d = json.loads(raw)
res = d.get("result", d)
if isinstance(res, str):
    res = json.loads(res)
labels = res["labels"]

manifest = json.load(open(os.path.join(batch_dir, "manifest.json")))
batch_ids = set(i for m in manifest for i in m["ids"])

# dedup: on a duplicate id (a neighbor judge hallucinating another sub's id),
# keep the entry with more votes — that's the one from the id's real sub-file.
best, spurious = {}, []
for l in labels:
    if l.get("id") not in batch_ids:
        spurious.append(l.get("id"))
        continue
    prev = best.get(l["id"])
    if prev is None or l.get("votes", 0) > prev.get("votes", 0):
        best[l["id"]] = l
clean = list(best.values())
missing = sorted(batch_ids - set(best), key=lambda x: int(x.split("_")[1]))

json.dump({"labels": clean}, open(os.path.join(batch_dir, "result.json"), "w"))
print(
    f"clean={len(clean)}/{len(batch_ids)} | spurious={spurious} | "
    f"missing={missing[:10]}{'...' if len(missing) > 10 else ''}"
)
