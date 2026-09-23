#!/usr/bin/env python3
"""Is a strip-arm top-1 flip on a non-reproducible model noise or the rollback?

cache_strategy_stress.py --mode rollback holds the strip arm (rollback + replay
of [K + ANS]) to the inline stream [P, K + ANS, NEXT]. On qwen4exp past ~30k
tokens the model does not reproduce ITSELF bit-for-bit (A vs A' max|Δ| ~1
logit), so a lone flip there proves nothing either way. This control computes
each path TWICE and compares every pair:

  r  vs r'  — inline, no rollback, run twice: the model's own noise
  s  vs s'  — rollback strip, run twice
  r  vs s   — the comparison the gate made

If r/s disagreement (flips, KL) sits inside r/r' and s/s', the rollback adds
nothing the model does not already do to itself.

Usage (llmvp venv, seat free):
  python dev/rollback_strip_noise_control.py --model PATH --depth 100000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from llama_cpp import Llama

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "llmvp"))

from cache_strategy_stress import (  # noqa: E402
    _compare,
    _hard_reset,
    _last_logits,
    _teacher_forced,
)
from inference.turn_checkpoint import (  # noqa: E402
    MODE_PARTIAL,
    RestoreOutcome,
    TurnCheckpointStore,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--depth", type=int, default=100000)
    ap.add_argument("--n-ctx", type=int, default=131072)
    ap.add_argument("--gen", type=int, default=32)
    ap.add_argument("--eps", type=float, default=0.05)
    args = ap.parse_args()

    llm = Llama(
        model_path=args.model,
        n_ctx=args.n_ctx,
        n_gpu_layers=-1,
        n_seq_max=1,
        kv_unified=True,
        logits_all=False,
        verbose=False,
        ctx_checkpoints=0,
    )
    tok = lambda b: llm.tokenize(b, add_bos=False)  # noqa: E731
    words = "You are a meticulous operator. " + " ".join(
        f"item{i}" for i in range(args.depth)
    )
    P = llm.tokenize(words.encode(), add_bos=True)[: args.depth]
    K = tok(b"\nQ: What is 2 + 2? A:")
    THINK = tok(b" Let me think step by step about this simple sum.")
    ANS = tok(b" 4.")
    NEXT = tok(b"\nQ: And what is 3 + 3? A:")

    def inline():
        _hard_reset(llm)
        llm.eval(P)
        llm.eval(K + ANS)
        llm.eval(NEXT)

    def strip():
        _hard_reset(llm)
        store = TurnCheckpointStore(MODE_PARTIAL)
        llm.eval(P)
        store.capture(llm)
        llm.eval(K + THINK + ANS)
        assert store.restore(llm, len(P)) is RestoreOutcome.OK
        llm.eval(K + ANS)
        llm.eval(NEXT)

    # Teacher sequence: greedy from the first inline run.
    inline()
    forced, r1 = [], []
    for _ in range(args.gen):
        lg = _last_logits(llm)
        r1.append(lg)
        forced.append(int(np.argmax(lg)))
        llm.eval([forced[-1]])
    inline()
    r2 = _teacher_forced(llm, forced)
    strip()
    s1 = _teacher_forced(llm, forced)
    strip()
    s2 = _teacher_forced(llm, forced)

    print(f"== strip noise control: depth {len(P)}, {args.gen} positions ==")
    for name, a, b in (
        ("r  vs r'  (inline twice — the model's own noise)", r1, r2),
        ("s  vs s'  (rollback strip twice)", s1, s2),
        ("r  vs s   (the gate's comparison)", r1, s1),
        ("r' vs s'", r2, s2),
    ):
        c = _compare(a, b, eps=args.eps)
        c0 = _compare(a, b, eps=0.0)
        print(
            f"  {name:<52} max|Δ|={c['max_abs']:.3g} mean_KL={c['mean_kl']:.3g} "
            f"top1 {c0['agree']}/{c0['compared']} flips at margins {c0['flip_margins']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
