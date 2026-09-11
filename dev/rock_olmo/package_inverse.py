#!/usr/bin/env python3
"""Pack the inverse-identification experiment (rock venv; reuses package.Packer).

Train: identify + predict examples (prompt masked, completion + EOS trained).
Val sets: val-identify_heldout (one RRUFF spectrum per species, in-distribution
instrument) and val-identify_rod (ROD, unseen instrument) — plus, symlinked in,
the stage-1 val sets (paper_markdown, replay, reference) to watch prose
forgetting, and v4/stage2's shapes val under the name shapes_v4 to watch
interference with the OLD fact shapes. train_full.py globs val-*.bin.

    ./.venv/bin/python package_inverse.py --corpus ~/corpora/rock-olmo-training/v4/inverse_exp
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from package import Packer, load_tokenizer, read_jsonl, write_shards, SEQ, _git_commit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--stage1", default=os.path.expanduser("~/corpora/rock-olmo-training/v4/stage1"))
    ap.add_argument("--stage2", default=os.path.expanduser("~/corpora/rock-olmo-training/v4/stage2"))
    args = ap.parse_args()
    t0 = time.time()
    tok = load_tokenizer()
    rng = random.Random(args.seed)
    docs = os.path.join(args.corpus, "docs")

    def tokenize(rows):
        P = tok([r["prompt"] for r in rows], add_special_tokens=False)["input_ids"]
        C = tok([(" " + r["completion"].lstrip()) if not r["completion"].startswith(" ") else r["completion"]
                 for r in rows], add_special_tokens=False)["input_ids"]
        return P, C

    train = read_jsonl([os.path.join(docs, "inverse_train.jsonl")])
    rng.shuffle(train)
    P, C = tokenize(train)
    pk = Packer(SEQ)
    dropped = 0
    for i, r in enumerate(train):
        if not pk.add_example(r["fact_id"], P[i], C[i]):
            dropped += 1
    pk.finish()
    train_tokens = sum(len(P[i]) + len(C[i]) + 1 for i in range(len(train)))
    train_stats = write_shards(args.corpus, "train", pk, rng)
    by_kind = collections.Counter(r["kind"] for r in train)
    print(f"[{time.time()-t0:4.0f}s] train: {len(train):,} examples ({dict(by_kind)}), {train_tokens:,} example tokens, "
          f"{train_stats['blocks']} blocks, pad {train_stats['pad_fraction']:.2%}, dropped {dropped}", flush=True)

    val_stats = {}
    for name in ("inverse_val_heldout", "inverse_val_rod"):
        rows = read_jsonl([os.path.join(docs, name + ".jsonl")])
        Pv, Cv = tokenize(rows)
        vp = Packer(SEQ)
        for i, r in enumerate(rows):
            vp.add_example(r["fact_id"], Pv[i], Cv[i])
        vp.finish()
        tag = "identify_" + name.split("_")[-1]
        val_stats[tag] = write_shards(args.corpus, f"val-{tag}", vp, rng, shuffle=False)
        print(f"[{time.time()-t0:4.0f}s] val-{tag}: {len(rows)} examples, {val_stats[tag]['blocks']} blocks", flush=True)

    # borrowed val sets: stage-1 prose (forgetting) and v4 stage-2 shapes (interference)
    for v in ("paper_markdown", "replay", "reference"):
        for ext in ("bin", "mask.bin", "idx.json"):
            src = os.path.join(args.stage1, f"val-{v}-000.{ext}")
            dst = os.path.join(args.corpus, f"val-{v}-000.{ext}")
            if os.path.exists(src) and not os.path.exists(dst):
                os.symlink(src, dst)
    for ext in ("bin", "mask.bin", "idx.json"):
        src = os.path.join(args.stage2, f"val-shapes-000.{ext}")
        dst = os.path.join(args.corpus, f"val-shapes_v4-000.{ext}")
        if os.path.exists(src) and not os.path.exists(dst):
            os.symlink(src, dst)

    man = {"stage": "inverse_exp", "seq": SEQ, "seed": args.seed, "git": _git_commit(),
           "train_examples": len(train) - dropped, "dropped_oversize": dropped, "by_kind": dict(by_kind),
           "train_example_tokens": train_tokens, "train": train_stats, "val": val_stats,
           "borrowed_val": ["paper_markdown", "replay", "reference", "shapes_v4"],
           "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    json.dump(man, open(os.path.join(args.corpus, "manifest.json"), "w"), indent=1)
    print(f"[{time.time()-t0:4.0f}s] manifest written; val sets: {sorted(os.path.basename(p)[4:-8] for p in __import__('glob').glob(os.path.join(args.corpus, 'val-*-000.bin')))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
