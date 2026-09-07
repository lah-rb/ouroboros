#!/usr/bin/env python3
"""Device map for OLMo 2 1B across a 3090 (cuda:0) and a 3060 (cuda:1) — and
the probe that measures whether a split fits and how fast it runs.

WHY A HAND-WRITTEN MAP. The two cards are heterogeneous (23.6 vs 11.6 GiB,
~3x compute apart). DataParallel would put a full copy of the 100,352-vocab
logits on the 3060 and OOM it (train_lora.py's L38 comment records exactly
that). `device_map="auto"` balances by MEMORY and would give the 3060 ~a third
of the layers, which makes the slow card the bottleneck. So the map is
explicit: the embeddings (28 % of parameters, almost no compute) plus a few
early layers live on the 3060; the rest, the final norm and `lm_head` live on
the 3090 so logits and labels share a device.

FULL-PARAMETER TRAINING NEEDS FP32 MASTER WEIGHTS. At lr 4e-5 an update is
~1e-6 relative; bf16 carries ~3 significant digits and rounds it away.
Parameters are fp32 (5.9 GB) + fp32 grads (5.9) + fp32 AdamW moments (11.8)
= 23.7 GB static, which is why the pooled 35 GB is the design and not a
single card.

THE PROBE, not the estimate, chooses the split (§19 rule: measured before
frozen): for each candidate count of 3060 layers it loads the model, runs a
few fused-AdamW steps on random 4,096-token blocks under bf16 autocast with
gradient checkpointing, and reports peak memory per device and tokens/hour.

  ./.venv/bin/python device_map.py --probe                 # counts 2 3 4 5
  ./.venv/bin/python device_map.py --probe --small-layers 3 --steps 6
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import time

# Both cards visible, BEFORE torch loads. Never unset with a single-device
# model: Trainer would wrap it in DataParallel.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0,1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch  # noqa: E402

MODEL = os.path.expanduser(os.environ.get("ROCK_OLMO_BASE", "~/models/OLMo-2-0425-1B"))
N_LAYERS = 16
BIG, SMALL = "cuda:0", "cuda:1"
SEQ = 4096
VOCAB = 100352


def build_device_map(small_layers: int, *, big: str = BIG, small: str = SMALL) -> dict:
    """Embeddings + the first `small_layers` decoder layers on the small card;
    everything else, the final norm and lm_head on the big card."""
    if not 0 <= small_layers <= N_LAYERS:
        raise ValueError(f"small_layers must be in [0, {N_LAYERS}]")
    dm = {"model.embed_tokens": small, "model.rotary_emb": small}
    for i in range(N_LAYERS):
        dm[f"model.layers.{i}"] = small if i < small_layers else big
    dm["model.norm"] = big
    dm["lm_head"] = big
    return dm


def assert_two_gpus() -> None:
    n = torch.cuda.device_count()
    if n != 2:
        raise SystemExit(
            f"expected 2 visible GPUs, found {n} (CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES')})"
        )
    m0 = torch.cuda.get_device_properties(0).total_memory
    m1 = torch.cuda.get_device_properties(1).total_memory
    if m0 < m1:
        raise SystemExit(
            "device 0 must be the larger card (the map puts lm_head there)"
        )


def load_model(small_layers: int, *, grad_ckpt: bool = True):
    from transformers import AutoModelForCausalLM

    dm = build_device_map(small_layers)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, dtype=torch.float32, device_map=dm, attn_implementation="sdpa"
    )
    model.config.use_cache = False
    if grad_ckpt:
        model.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
    model.train()
    return model, dm


def probe_one(small_layers: int, steps: int, accum: int) -> dict:
    """Load, run `steps` optimizer steps of `accum` micro-batches each, report."""
    for d in (0, 1):
        torch.cuda.reset_peak_memory_stats(d)
    t_load = time.monotonic()
    model, dm = load_model(small_layers)
    load_s = time.monotonic() - t_load
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(
        params, lr=4e-5, betas=(0.9, 0.95), eps=1e-8, weight_decay=0.0, fused=True
    )
    gen = torch.Generator().manual_seed(0)
    embed_dev = next(model.model.embed_tokens.parameters()).device
    tok_s = []
    ok = True
    err = ""
    try:
        for step in range(steps + 1):  # step 0 = warmup, excluded from timing
            torch.cuda.synchronize(0)
            torch.cuda.synchronize(1)
            t0 = time.monotonic()
            for _ in range(accum):
                ids = torch.randint(0, VOCAB - 100, (1, SEQ), generator=gen).to(
                    embed_dev
                )
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    out = model(input_ids=ids, labels=ids.to(BIG))
                (out.loss / accum).backward()
            torch.nn.utils.clip_grad_norm_(params, 1.0)
            opt.step()
            opt.zero_grad(set_to_none=True)
            torch.cuda.synchronize(0)
            torch.cuda.synchronize(1)
            if step > 0:
                tok_s.append(accum * SEQ / (time.monotonic() - t0))
    except torch.cuda.OutOfMemoryError as e:  # noqa: PERF203
        ok, err = False, f"OOM: {str(e)[:160]}"
    peak = {
        f"cuda:{d}": round(torch.cuda.max_memory_allocated(d) / 2**30, 2)
        for d in (0, 1)
    }
    total = {
        f"cuda:{d}": round(torch.cuda.get_device_properties(d).total_memory / 2**30, 2)
        for d in (0, 1)
    }
    res = {
        "small_layers": small_layers,
        "device_map": dm,
        "ok": ok,
        "error": err,
        "load_s": round(load_s, 1),
        "peak_gib": peak,
        "total_gib": total,
        "headroom_gib": {k: round(total[k] - peak[k], 2) for k in peak},
        "tokens_per_s": round(sum(tok_s) / len(tok_s), 1) if tok_s else 0.0,
        "tokens_per_h": round(3600 * sum(tok_s) / len(tok_s)) if tok_s else 0,
        "steps_timed": len(tok_s),
        "accum": accum,
    }
    del opt, model, params
    gc.collect()
    torch.cuda.empty_cache()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--small-layers", type=int, nargs="*", default=[2, 3, 4, 5])
    ap.add_argument(
        "--steps", type=int, default=4, help="timed optimizer steps (plus one warmup)"
    )
    ap.add_argument(
        "--accum", type=int, default=2, help="micro-batches per step in the probe"
    )
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/device_map_probe.json"))
    args = ap.parse_args()
    if not args.probe:
        print(json.dumps(build_device_map(3), indent=1))
        return 0
    assert_two_gpus()
    torch.backends.cuda.matmul.allow_tf32 = True
    results = []
    for n in args.small_layers:
        print(f"[{time.strftime('%H:%M:%S')}] probing small_layers={n} …", flush=True)
        r = probe_one(n, args.steps, args.accum)
        results.append(r)
        print(
            f"  ok={r['ok']} peak {r['peak_gib']} headroom {r['headroom_gib']} "
            f"tok/s={r['tokens_per_s']} (~{r['tokens_per_h']/1e6:.2f}M tok/h) {r['error']}",
            flush=True,
        )
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=1)
    fits = [r for r in results if r["ok"] and min(r["headroom_gib"].values()) >= 1.5]
    if fits:
        best = max(fits, key=lambda r: r["tokens_per_s"])
        print(
            f"\nRECOMMENDED small_layers={best['small_layers']}: {best['tokens_per_h']/1e6:.2f}M tok/h, headroom {best['headroom_gib']}"
        )
    else:
        print(
            "\nNO SPLIT fits with >=1.5 GiB headroom on both cards — see fallbacks in the plan (freeze embeddings / adafactor)"
        )
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
