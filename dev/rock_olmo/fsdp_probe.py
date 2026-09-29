#!/usr/bin/env python3
"""Sharded data-parallel probe for full-parameter OLMo 2 1B on the two NVLinked 3090s (rock venv).

WHY. device_map.py splits the model's LAYERS across the cards. That was the right answer
for a 3090 + 3060, but it runs the cards one after the other: each token's forward and
backward pass through the first card, then the second. With two identical cards joined
by NVLink (NV4, 52.8 GB/s measured), each card can take its own batch while the gradients
and AdamW state are split between them (FSDP; ZeRO-2 = SHARD_GRAD_OP, ZeRO-3 =
FULL_SHARD). Memory per card: fp32 parameters 5.9 GB (whole under SHARD_GRAD_OP, halved
under FULL_SHARD) + half of the fp32 gradients (2.9) + half of the AdamW moments (5.9),
plus activations.

SAME REGIME AS device_map.py --probe, so the numbers compare directly: random 4,096-token
blocks, fp32 master weights under bf16 autocast, gradient checkpointing (non-reentrant),
fused AdamW (lr 4e-5, betas 0.9/0.95), grad-norm clip 1.0, step 0 excluded from timing.
Each rank draws its own blocks, so a step covers world × accum blocks. Gradients are
reduce-scattered on every micro-step (no no_sync): over NVLink this is cheap and keeps
the gradients sharded.

  ./.venv/bin/torchrun --nproc_per_node 2 fsdp_probe.py --strategy shard_grad_op
  ./.venv/bin/torchrun --nproc_per_node 2 fsdp_probe.py --strategy full_shard
"""

from __future__ import annotations

import argparse
import datetime
import functools
import json
import os
import sys
import time

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch  # noqa: E402
import torch.distributed as dist  # noqa: E402
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP  # noqa: E402
from torch.distributed.fsdp import ShardingStrategy  # noqa: E402
from torch.distributed.fsdp.wrap import transformer_auto_wrap_policy  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

MODEL = os.path.expanduser(os.environ.get("ROCK_OLMO_BASE", "~/models/OLMo-2-0425-1B"))
SEQ = 4096
VOCAB = 100352
STRATEGIES = {"shard_grad_op": ShardingStrategy.SHARD_GRAD_OP, "full_shard": ShardingStrategy.FULL_SHARD}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", choices=sorted(STRATEGIES), default="shard_grad_op")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/fsdp_probe.json"))
    args = ap.parse_args()

    local = int(os.environ.get("LOCAL_RANK", "0"))
    torch.cuda.set_device(local)
    # A short collective timeout: a rank that fails must not leave the other waiting on
    # NCCL's 10-minute watchdog (2026-09-28: an OOM on rank 1 did exactly that).
    dist.init_process_group("nccl", device_id=torch.device(f"cuda:{local}"), timeout=datetime.timedelta(seconds=120))
    rank, world = dist.get_rank(), dist.get_world_size()
    from transformers import AutoModelForCausalLM
    from transformers.models.olmo2.modeling_olmo2 import Olmo2DecoderLayer

    t_load = time.monotonic()
    model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch.float32, attn_implementation="sdpa")
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model = FSDP(
        model,
        auto_wrap_policy=functools.partial(transformer_auto_wrap_policy, transformer_layer_cls={Olmo2DecoderLayer}),
        sharding_strategy=STRATEGIES[args.strategy],
        device_id=torch.cuda.current_device(),
        use_orig_params=True,
        limit_all_gathers=True,
    )
    model.train()
    load_s = time.monotonic() - t_load
    opt = torch.optim.AdamW(model.parameters(), lr=4e-5, betas=(0.9, 0.95), eps=1e-8, weight_decay=0.0, fused=True)
    gen = torch.Generator().manual_seed(rank)
    torch.cuda.reset_peak_memory_stats()
    tok_s, losses, ok, err = [], [], True, ""
    try:
        for step in range(args.steps + 1):  # step 0 = warmup, excluded from timing
            torch.cuda.synchronize()
            dist.barrier()
            t0 = time.monotonic()
            for _ in range(args.accum):
                ids = torch.randint(0, VOCAB - 100, (1, SEQ), generator=gen).cuda()
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    out = model(input_ids=ids, labels=ids)
                (out.loss / args.accum).backward()
            model.clip_grad_norm_(1.0)
            opt.step()
            opt.zero_grad(set_to_none=True)
            torch.cuda.synchronize()
            dist.barrier()
            losses.append(round(float(out.loss), 3))
            if step > 0:
                tok_s.append(world * args.accum * SEQ / (time.monotonic() - t0))
    except torch.cuda.OutOfMemoryError as e:
        # Report from THIS rank and stop at once; the other rank is inside a collective and
        # torchrun tears the group down when a child exits.
        print(json.dumps({"strategy": args.strategy, "rank": rank, "ok": False, "error": f"OOM: {str(e)[:200]}",
                          "peak_gib": round(torch.cuda.max_memory_allocated() / 2**30, 2)}), flush=True)
        os._exit(3)
    peak = torch.cuda.max_memory_allocated() / 2**30
    peaks = [None] * world
    dist.all_gather_object(peaks, round(peak, 2))
    if rank == 0:
        res = {
            "strategy": args.strategy, "world": world, "ok": ok, "error": err, "load_s": round(load_s, 1),
            "peak_gib": {f"cuda:{i}": p for i, p in enumerate(peaks)},
            "tokens_per_s": round(sum(tok_s) / len(tok_s), 1) if tok_s else 0.0,
            "tokens_per_h": round(3600 * sum(tok_s) / len(tok_s)) if tok_s else 0,
            "steps_timed": len(tok_s), "accum": args.accum, "blocks_per_step": world * args.accum, "losses": losses,
        }  # fmt: skip
        print(json.dumps(res, indent=1), flush=True)
        prev = json.load(open(args.out)) if os.path.exists(args.out) else []
        json.dump(prev + [res], open(args.out, "w"), indent=1)
    dist.destroy_process_group()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
