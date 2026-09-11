"""Split a packed validation set's loss by paragraph class (CPU only).

Runs under dev/rock_olmo/.venv. Answers one question the aggregate
`eval_paper_markdown_loss` cannot: how much of the papers improvement sits in
the inlined VLM figure readings (a house style, ~24 % of paper characters) and
how much in the papers' own text. Reads `val-<source>-000.{bin,mask.bin,
idx.json}` exactly as the trainer does; a token is attributed to the span
(paragraph) that contains it, and a span is `vlm` when its decoded text opens
with the `> [FIGURE` label or is the unanchored-figures header.

    CUDA_VISIBLE_DEVICES= ./.venv/bin/python val_split_figtext.py \
        --model ~/models/OLMo-2-0425-1B --val-dir ~/corpora/rock-olmo-training/v4/stage1 \
        --every 2 --threads 12 --out ~/tmp/analysis/figtext_split_base.json

CUDA is disabled at import so this can never touch the GPUs of a live run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

os.environ["CUDA_VISIBLE_DEVICES"] = ""

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

SEQ = 4096


def classify(text: str) -> str:
    t = text.lstrip()
    if t.startswith("> [FIGURE") or t.startswith("## Unanchored figures"):
        return "vlm"
    return "text"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--tokenizer", default="", help="tokenizer dir; trainer checkpoints carry none, so default to --model")
    ap.add_argument("--val-dir", required=True)
    ap.add_argument("--source", default="paper_markdown")
    ap.add_argument("--every", type=int, default=1, help="use every Nth block")
    ap.add_argument("--threads", type=int, default=12)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    base = os.path.join(args.val_dir, f"val-{args.source}-000")
    ids = np.memmap(base + ".bin", dtype="<u4", mode="r").reshape(-1, SEQ)
    mask = np.memmap(base + ".mask.bin", dtype="u1", mode="r").reshape(-1, SEQ)
    idx = json.load(open(base + ".idx.json"))
    blocks = list(range(0, len(ids), args.every))

    tok = AutoTokenizer.from_pretrained(args.tokenizer or args.model)
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.float32)
    model.eval()

    sums = {"vlm": 0.0, "text": 0.0}
    counts = {"vlm": 0, "text": 0}
    spans = {"vlm": 0, "text": 0}
    per_doc: dict[str, dict[str, list[float]]] = {}
    t0 = time.time()
    for n, b in enumerate(blocks):
        x = torch.from_numpy(ids[b].astype(np.int64))[None]
        with torch.no_grad():
            logits = model(input_ids=x).logits[0].float()
        nll = F.cross_entropy(logits[:-1], x[0, 1:], reduction="none").numpy()
        m = mask[b]
        cls = np.empty(SEQ, dtype=object)
        cls[:] = "text"
        doc_of = np.empty(SEQ, dtype=object)
        for doc_id, s, e in idx[b]:
            c = classify(tok.decode(ids[b][s:e].tolist()))
            cls[s:e] = c
            doc_of[s:e] = doc_id
            spans[c] += 1
        for pos in range(1, SEQ):
            if m[pos] == 0:
                continue
            c = cls[pos]
            sums[c] += float(nll[pos - 1])
            counts[c] += 1
            d = per_doc.setdefault(doc_of[pos] or "?", {"vlm": [0.0, 0], "text": [0.0, 0]})
            d[c][0] += float(nll[pos - 1])
            d[c][1] += 1
        el = time.time() - t0
        print(
            f"[{el:6.0f}s] block {n+1}/{len(blocks)}  running vlm {sums['vlm']/max(1,counts['vlm']):.4f} "
            f"text {sums['text']/max(1,counts['text']):.4f}  eta {el/(n+1)*(len(blocks)-n-1)/60:.0f} min",
            flush=True,
        )

    total = sums["vlm"] + sums["text"]
    ntot = counts["vlm"] + counts["text"]
    out = {
        "model": args.model,
        "source": args.source,
        "blocks_used": len(blocks),
        "blocks_total": int(len(ids)),
        "tokens": counts,
        "token_share": {k: counts[k] / max(1, ntot) for k in counts},
        "spans": spans,
        "mean_nll": {k: sums[k] / max(1, counts[k]) for k in counts},
        "mean_nll_all": total / max(1, ntot),
        "per_doc": {
            d: {k: (v[0] / v[1] if v[1] else None) for k, v in parts.items()}
            for d, parts in per_doc.items()
        },
        "seconds": time.time() - t0,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    json.dump(out, open(args.out, "w"), indent=1)
    print("DONE", json.dumps({k: out[k] for k in ("tokens", "mean_nll", "mean_nll_all")}), flush=True)


if __name__ == "__main__":
    sys.exit(main())
