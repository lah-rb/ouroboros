#!/usr/bin/env python3
"""Fetch the replay shards for corpus v4 (rock venv; network).

Replay = a sample of OLMo 2's OWN midtraining mix (allenai/dolmino-mix-1124,
ODC-BY, ungated), biased toward scientific text per the operator: pes2o
(STEM papers) first, then wiki, then a little dclm web text. Only the
smallest shard of each subset is fetched — a few GB is thousands of times
the ~20M tokens replay.py samples from them.

  ./.venv/bin/python replay_fetch.py            # downloads into v4/replay/raw
"""

import os
from huggingface_hub import hf_hub_download

RAW = os.path.expanduser("~/corpora/rock-olmo-training/v4/replay/raw")
FILES = (
    "data/pes2o/pes2o-0025.json.gz",  # 479 MB — the small tail shard
    "data/dclm/0246/dclm-0001.json.zst",  # 597 MB — smallest dclm shard
    "data/wiki/wiki-0001.json.gz",  # 2.2 GB — smaller of the two wiki shards
)
for f in FILES:
    print("fetching", f, flush=True)
    p = hf_hub_download(
        "allenai/dolmino-mix-1124", f, repo_type="dataset", local_dir=RAW
    )
    print("  ->", p, round(os.path.getsize(p) / 1e6), "MB", flush=True)
print("DONE")
