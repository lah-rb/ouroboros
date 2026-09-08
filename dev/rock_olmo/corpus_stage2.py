#!/usr/bin/env python3
"""Render the stage-2 (shaped anneal) examples for corpus v4 (root venv).

WHAT STAGE 2 IS. prompt -> completion pairs over the structured facts, loss
on the completion only, in the frames of templates.py. Every fact is
rendered through N_FRAMES distinct frames of which at least one is a
question; canonical Raman facts additionally render in the inverse
direction (bands -> species), which is the spectroscopist's actual task.
Pack facts (one packed key/value per prompt) teach that a paper's numbers
are askable. Targets are single-valued by construction (facts.py).

Nothing is excluded (operator ruling: everything trains; the probe species
are an evaluation list ranked by exposure). The probe FRAMES are never
rendered here (templates.PROBE_FRAMES).

  ../../.venv/bin/python corpus_stage2.py           # -> v4/stage2/docs/shapes.jsonl
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import time

import emit
from facts import build_facts, census
from templates import FRAMES, pick_frames, render, render_kind

V4 = os.path.expanduser("~/corpora/rock-olmo-training/v4")
OUT = os.path.join(V4, "stage2", "docs", "shapes.jsonl")
MANIFEST = os.path.join(V4, "stage2", "docs_manifest.json")
SEED = 20260824
VAL_FRACTION = 0.01
N_FRAMES = 3
N_FRAMES_PACK = 1
N_INVERSE = 2


def is_val(fact_id: str) -> bool:
    return (
        int(hashlib.sha256(f"{SEED}|s2|{fact_id}".encode()).hexdigest()[:8], 16)
        % 10_000
        < VAL_FRACTION * 10_000
    )


def _with_a_question(fact_id: str, kind: str, k: int, salt: str) -> list:
    picks = pick_frames(fact_id, kind, k, questions=True, salt=salt)
    qs = FRAMES[kind]["questions"]
    if not any(fr in qs for _, fr in picks):
        # swap the last pick for the fact's first-ranked question frame
        qpick = pick_frames(
            fact_id,
            kind,
            len(FRAMES[kind]["statements"]) + 1,
            questions=True,
            salt=salt,
        )
        q = next(((i, fr) for i, fr in qpick if fr in qs), None)
        if q:
            picks[-1] = q
    return picks


def shapes(exclude: set[str]) -> tuple[list[dict], dict]:
    facts = build_facts(exclude)
    out: list[dict] = []
    for f in facts:
        k = N_FRAMES_PACK if f.kind == "pack_fact" else N_FRAMES
        val = is_val(f.fact_id)
        for i, frame in _with_a_question(f.fact_id, f.kind, k, "s2"):
            r = render(f, frame)
            comp, _ = emit.normalize_decimals(r["completion"], commas=False)
            out.append(
                {
                    "fact_id": f.fact_id,
                    "kind": f.kind,
                    "frame": i,
                    "prompt": r["prompt"],
                    "completion": comp,
                    "species": f.species,
                    "source": f.source,
                    "canonical": f.canonical,
                    "val": val,
                }
            )
        if f.kind == "raman_bands" and f.canonical:
            for i, frame in _with_a_question(f.fact_id, "inverse", N_INVERSE, "s2inv"):
                r = render_kind(f, "inverse", frame)
                out.append(
                    {
                        "fact_id": f.fact_id,
                        "kind": "inverse",
                        "frame": i,
                        "prompt": r["prompt"],
                        "completion": r["completion"],
                        "species": f.species,
                        "source": f.source,
                        "canonical": True,
                        "val": val,
                    }
                )
    return out, census(facts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()
    t0 = time.time()
    exclude: set[str] = set()  # everything trains; the probe set is an eval list
    rows, fcensus = shapes(exclude)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    by = collections.Counter(r["kind"] for r in rows)
    chars = sum(len(r["prompt"]) + len(r["completion"]) for r in rows)
    man = {
        "seed": SEED,
        "val_fraction": VAL_FRACTION,
        "n_frames": N_FRAMES,
        "n_inverse": N_INVERSE,
        "examples": len(rows),
        "by_kind": dict(by),
        "val_examples": sum(r["val"] for r in rows),
        "chars": chars,
        "tokens_est": int(chars / 3.5),
        "facts": fcensus,
        "probe_species_excluded": len(exclude),
    }
    json.dump(man, open(MANIFEST, "w"), indent=1)
    print(
        f"STAGE 2 SHAPES: {len(rows)} examples (~{man['tokens_est']:,} tokens) in {time.time()-t0:.0f}s -> {args.out}"
    )
    for k, n in by.most_common():
        print(f"  {k:16s} {n:7d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
