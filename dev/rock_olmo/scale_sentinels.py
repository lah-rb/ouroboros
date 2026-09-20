#!/usr/bin/env python3
"""Give OLMo-2's three FIM sentinels an audible input embedding (§22b; rock venv).

WHAT WAS MEASURED (2026-09-20). OLMo-2-0425-1B's input embeddings are untied
(`tie_word_embeddings: false`). Trained rows have norm median 10.82 (p5 6.66,
p95 14.18). The three reserved FIM sentinels — <|fim_prefix|> 100258,
<|fim_middle|> 100259, <|fim_suffix|> 100260 — sit at 0.88–0.90, random
directions (cosine to the mean embedding ≈ 0), the same as the 272 other
never-trained ids. Their lm_head rows are 1.78 against a median of 1.81, i.e.
ordinary. After 877 steps at 2e-5 and 1,500 steps at 3e-5 the input rows had
moved by 0.003: Adam bounds a coordinate's drift by lr × steps, so a fine-tune
cannot grow them.

WHY IT MATTERS. A sentinel's token-identity signal enters the residual stream
at one twelfth of a normal token's. The behaviour FIM needs most — on
<|fim_middle|> after a long suffix, STOP continuing the suffix and resume the
prefix (the PSM order) — is a switch keyed on that near-silent token. The
stage-0 readings fit this exactly: SPM (no switch; the middle follows the
adjacent prefix) 0.50–0.55, PSM 0.15–0.23, both flat from 16 M to 49 M tokens
at two rates from two inits, and the smoke from the v2 endpoint reached the
same ceiling.

THE SURGERY. Keep each sentinel row's direction (already mutually near-
orthogonal and orthogonal to every trained token, as any random direction in
2,048 dimensions is) and rescale it to the trained-row median norm. No new
information is injected; the token merely becomes as loud as the others.
lm_head rows are left alone. Written as a new model dir so the base stays
byte-identical.

  ./.venv/bin/python scale_sentinels.py --src ~/models/OLMo-2-0425-1B --dst ~/models/OLMo-2-0425-1B-sentinels
"""

from __future__ import annotations

import argparse
import json
import os

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

SENTINELS = {"<|fim_prefix|>": 100258, "<|fim_middle|>": 100259, "<|fim_suffix|>": 100260}
TRAINED_ROWS = 100_000  # the real vocabulary; ids above are special / unused


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.expanduser("~/models/OLMo-2-0425-1B"))
    ap.add_argument("--dst", default=os.path.expanduser("~/models/OLMo-2-0425-1B-sentinels"))
    ap.add_argument("--target", type=float, default=0.0, help="row norm to scale to (default: median of trained rows)")
    args = ap.parse_args()
    model = AutoModelForCausalLM.from_pretrained(args.src, dtype=torch.float32)
    tok = AutoTokenizer.from_pretrained(args.src)
    for name, i in SENTINELS.items():
        ids = tok(name, add_special_tokens=False)["input_ids"]
        assert ids == [i], (name, ids)
    W = model.model.embed_tokens.weight.data
    norms = W[:TRAINED_ROWS].norm(dim=1)
    target = args.target or float(norms.median())
    before, after, cos = {}, {}, {}
    with torch.no_grad():
        rows = torch.stack([W[i] for i in SENTINELS.values()])
        for name, i in SENTINELS.items():
            before[name] = round(float(W[i].norm()), 4)
            W[i] *= target / W[i].norm()
            after[name] = round(float(W[i].norm()), 4)
        c = torch.nn.functional.cosine_similarity(rows[:, None, :], rows[None, :, :], dim=-1)
        cos["pairwise_max_abs_offdiag"] = round(float((c - torch.eye(3)).abs().max()), 4)
        cos["max_abs_to_trained_rows"] = round(
            float(torch.nn.functional.cosine_similarity(rows[:, None, :], W[None, :TRAINED_ROWS, :], dim=-1).abs().max()), 4
        )
    os.makedirs(args.dst, exist_ok=True)
    model.save_pretrained(args.dst, safe_serialization=True)
    tok.save_pretrained(args.dst)
    report = {
        "src": args.src,
        "target_norm": round(target, 4),
        "trained_rows": {"median": round(float(norms.median()), 4), "p5": round(float(norms.quantile(0.05)), 4), "p95": round(float(norms.quantile(0.95)), 4)},
        "before": before,
        "after": after,
        "direction_check": cos,
        "lm_head_untouched": True,
    }
    json.dump(report, open(os.path.join(args.dst, "sentinel_scaling.json"), "w"), indent=1)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
