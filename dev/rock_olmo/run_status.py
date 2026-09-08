#!/usr/bin/env python3
"""Status of a train_full.py run, computed correctly (rock or root venv).

TWO ARITHMETIC TRAPS this exists to stop repeating (both hit on 2026-09-08):

  * `num_input_tokens_seen` is CUMULATIVE ACROSS A RESUME — it counts the
    batches Trainer skips to reach the resume step. Dividing it by this
    segment's `train_runtime` reported 11-13M tok/h against a real 7M. The
    rate must come from the DELTA within the segment.
  * `train_runtime` is seconds and the token budget is per epoch; mixing a
    per-hour rate with a seconds conversion printed "remaining 0.0h" twice.

Reads one or more logs in order (the pre-resume log first) so a trajectory
spans a restart.

  ../../.venv/bin/python run_status.py ~/tmp/train_stage1_lr4e-5.log ~/tmp/train_stage1_lr2e-5.log
"""

from __future__ import annotations

import argparse
import re
import time

TOKENS_PER_EPOCH = 140_333_056  # stage 1, from v4/stage1/manifest.json
#: base-model losses on the v4 stage-1 val sets (PROCEDURE.md §19)
BASE = {
    "replay": 2.118,
    "paper_markdown": 1.626,
    "reference": 1.719,
    "binder_markdown": 1.852,
    "hom": 2.485,
    "webmineral": 2.357,
    "pack_prose": 2.264,
    "mindat_prose": 2.728,
}
REPLAY_BOUND = round(BASE["replay"] * 1.03, 4)


def series(text: str, key: str) -> list[tuple[float, float]]:
    pat = r"\{[^{}]*'eval_" + key + r"_loss': '([0-9.]+)'[^{}]*'epoch': '?([0-9.]+)'?"
    return [(float(m.group(2)), float(m.group(1))) for m in re.finditer(pat, text)]


def rate_and_eta(text: str, total_epochs: float) -> dict:
    tok = [int(x) for x in re.findall(r"'num_input_tokens_seen': (\d+)", text)]
    rt = [float(x) for x in re.findall(r"'train_runtime': '([0-9.e+]+)'", text)]
    ep = [float(x) for x in re.findall(r"'epoch': '([0-9.]+)'", text)]
    if not (tok and rt and ep):
        return {}
    seg_tokens = tok[-1] - tok[0]  # delta, not the cumulative counter
    seg_seconds = rt[-1]
    rate_h = seg_tokens / seg_seconds * 3600 if seg_seconds else 0.0
    epoch = ep[-1]
    remaining_h = (total_epochs - epoch) * TOKENS_PER_EPOCH / rate_h if rate_h else 0.0
    return {
        "epoch": epoch,
        "tokens_seen": tok[-1],
        "segment_tokens": seg_tokens,
        "segment_hours": seg_seconds / 3600,
        "rate_M_per_h": rate_h / 1e6,
        "remaining_h": remaining_h,
        "eta": time.strftime(
            "%a %d %b %H:%M", time.localtime(time.time() + remaining_h * 3600)
        ),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--tail", type=int, default=8)
    args = ap.parse_args()
    texts = [open(p, errors="ignore").read() for p in args.logs]
    joined = "\n".join(texts)

    print(
        f"{'source':16s} {'base':>7s} {'now':>8s} {'change':>8s}   last {args.tail} evals"
    )
    for key, base in BASE.items():
        s = series(joined, key)
        if not s:
            continue
        now = s[-1][1]
        mark = "  <-- OVER BOUND" if key == "replay" and now > REPLAY_BOUND else ""
        print(
            f"{key:16s} {base:7.3f} {now:8.4f} {100*(now/base-1):+7.1f}%   "
            + " ".join(f"{v:.4f}" for _, v in s[-args.tail :])
            + mark
        )
    print(f"\nreplay bound (+3 %) = {REPLAY_BOUND}")
    st = rate_and_eta(texts[-1], args.epochs)
    if st:
        print(
            f"epoch {st['epoch']:.3f}/{args.epochs:g} | tokens seen {st['tokens_seen']:,} "
            f"(this segment {st['segment_tokens']/1e6:.1f}M in {st['segment_hours']:.2f}h)"
        )
        print(
            f"rate {st['rate_M_per_h']:.2f}M tok/h | remaining {st['remaining_h']:.1f}h | ETA {st['eta']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
