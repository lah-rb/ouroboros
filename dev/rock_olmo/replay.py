#!/usr/bin/env python3
"""Sample the replay stream from OLMo 2's own midtraining mix (rock venv).

WHY REPLAY. §18: 14M tokens of mineral text made the base model's own
knowledge of mineral names WORSE — the classic forgetting signature of
continued pretraining without any of the original distribution. The
standard remedy is to keep a slice of the base model's data in the mix.
dolmino-mix-1124 is the mix OLMo 2 itself annealed on (ODC-BY, ungated).

SCIENCE-BIASED, per the operator (2026-09-07): pes2o (STEM papers) carries
most of the budget, wiki is filtered to science/geoscience articles by a
title-and-lead heuristic, and a small slice of dclm web text keeps general
register. No flan, no stackexchange. Documents are taken in shard order
(dolmino shards are globally shuffled), skipping anything under 200 tokens,
until each subset's token budget is met; 1 % of documents are flagged `val`
so a replay validation loss exists — the forgetting instrument in §19.

  ./.venv/bin/python replay.py                 # -> v4/replay/replay.jsonl + replay_manifest.json
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import time

from transformers import AutoTokenizer

V4 = os.path.expanduser("~/corpora/rock-olmo-training/v4")
RAW = os.path.join(V4, "replay", "raw", "data")
OUT = os.path.join(V4, "replay", "replay.jsonl")
MANIFEST = os.path.join(V4, "replay", "replay_manifest.json")
TOK = os.path.expanduser("~/models/OLMo-2-0425-1B")

#: subset -> (relative path, token budget, science filter?)
PLAN = (
    ("pes2o", "pes2o/pes2o-0025.json.gz", 12_000_000, False),
    ("wiki", "wiki/wiki-0001.json.gz", 5_000_000, True),
    ("dclm", "dclm/0246/dclm-0001.json.zst", 3_000_000, True),
)
MIN_TOKENS = 200
MAX_TOKENS = 6_000  # one document never eats a budget
VAL_FRACTION = 0.01
SCIENCE_RE = re.compile(
    r"\b(mineral|crystal|spectr\w+|chemistr\w+|chemical|physic\w+|geolog\w+|petrolog\w+|volcan\w+|igneous|"
    r"metamorph\w+|sediment\w+|ore\b|oxide|silicate|carbonate|sulfide|sulphide|isotope|wavelength|infrared|"
    r"raman|x-ray|diffraction|laser|plasma|molecule|compound|element|thermodynamic|lattice|electron|photon|"
    r"astronom\w+|planet\w+|meteorite|materials science|metallurg\w+|ceramic|glass|semiconductor|catalys\w+|"
    r"temperature|pressure|reaction|solution|acid|alkal\w+|magnet\w+|optic\w+|microscop\w+|telescope)\b",
    re.I,
)


def _open(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(
            gzip.open(path, "rb"), encoding="utf-8", errors="replace"
        )
    if path.endswith(".zst"):
        import zstandard

        fh = open(path, "rb")
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        return io.TextIOWrapper(reader, encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def science_like(text: str) -> bool:
    head = text[:1500]
    return len(SCIENCE_RE.findall(head)) >= 3


def main() -> int:
    tok = AutoTokenizer.from_pretrained(TOK)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    manifest = {
        "subsets": {},
        "min_tokens": MIN_TOKENS,
        "max_tokens": MAX_TOKENS,
        "val_fraction": VAL_FRACTION,
        "license": "ODC-BY",
        "source": "allenai/dolmino-mix-1124",
    }
    t0 = time.time()
    n_total = 0
    with open(OUT, "w", encoding="utf-8") as out:
        for name, rel, budget, filt in PLAN:
            path = os.path.join(RAW, rel)
            if not os.path.exists(path):
                print(f"missing {path}; skipping {name}", flush=True)
                continue
            used = seen = kept = filtered = val_n = 0
            with _open(path) as fh:
                for line in fh:
                    if used >= budget:
                        break
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    seen += 1
                    text = (rec.get("text") or "").strip()
                    if len(text) < 400:
                        continue
                    if filt and not science_like(text):
                        filtered += 1
                        continue
                    ids = tok(text, add_special_tokens=False)["input_ids"]
                    n = len(ids)
                    if n < MIN_TOKENS:
                        continue
                    if n > MAX_TOKENS:
                        text = tok.decode(ids[:MAX_TOKENS])
                        n = MAX_TOKENS
                    doc_id = f"replay/{name}/{hashlib.sha1(text[:2000].encode()).hexdigest()[:16]}"
                    val = (
                        int(hashlib.sha256(doc_id.encode()).hexdigest()[:8], 16)
                        % 10_000
                        < VAL_FRACTION * 10_000
                    )
                    out.write(
                        json.dumps(
                            {
                                "doc_id": doc_id,
                                "source": f"replay/{name}",
                                "text": text,
                                "tokens": n,
                                "license": "ODC-BY",
                                "provenance": {
                                    "dataset": "allenai/dolmino-mix-1124",
                                    "shard": rel,
                                    "id": rec.get("id", ""),
                                },
                                "val": val,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    used += n
                    kept += 1
                    val_n += val
                    n_total += n
            manifest["subsets"][name] = {
                "shard": rel,
                "budget": budget,
                "tokens": used,
                "docs": kept,
                "docs_seen": seen,
                "filtered_out": filtered,
                "val_docs": val_n,
                "science_filter": filt,
            }
            print(
                f"{name}: {kept} docs, {used:,} tokens (seen {seen}, filtered {filtered}) in {time.time()-t0:.0f}s",
                flush=True,
            )
    manifest["tokens_total"] = n_total
    json.dump(manifest, open(MANIFEST, "w"), indent=1)
    print(f"DONE: {n_total:,} replay tokens -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
