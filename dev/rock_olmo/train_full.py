#!/usr/bin/env python3
"""Full-parameter two-stage training of OLMo 2 1B on corpus v4 (rock venv).

WHAT CHANGED FROM train_lora.py, and why (PROCEDURE.md §19):
  * FULL weights in fp32 with bf16 autocast. LoRA r32 (1.6 % of parameters)
    was measured a null for representation, and bf16 master weights round
    away updates at lr 4e-5.
  * BOTH GPUs through an explicit device map (device_map.py) — pooled memory
    for fp32 params + grads + AdamW moments (~24 GB static). Trainer runs its
    model-parallel path (hf_device_map spans two devices -> _n_gpu = 1).
    CUDA_VISIBLE_DEVICES is pinned to "0,1" before torch loads; unset, Trainer
    would DataParallel a single-device model and OOM the 3060 on the logits.
  * PACKED 4,096-token blocks from package.py (no truncation, no padding
    waste); labels come from the block's mask, so stage 2 trains on
    completions only.
  * WARMUP-STABLE-DECAY across stages: stage 1 constant after warmup (so a
    further epoch is a plain resume), stage 2 linear to zero — the shape of
    OLMo 2's own midtraining anneal.
  * Weight decay exempts embeddings (28 % of parameters), OLMo practice.
  * Per-source validation losses (papers, reference, replay, shapes): the
    replay loss is the forgetting instrument.
  * No load_best_model_at_end: the stage endpoint is the artefact, saved in
    bf16 beside the resumable fp32 checkpoints.

  ./.venv/bin/python train_full.py --stage 1 --corpus ~/corpora/rock-olmo-training/v4/stage1 \\
        --init ~/models/OLMo-2-0425-1B --out ~/models/olmo2-1b-spectra-full/stage1 --epochs 2
  ./.venv/bin/python train_full.py --stage 2 --corpus .../v4/stage2 --init .../stage1/final --out .../stage2 --epochs 2
  ./.venv/bin/python train_full.py --eval-only --corpus .../v4/stage1 --init <model dir> --out /tmp/x
  ./.venv/bin/python train_full.py --smoke ...
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import time

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0,1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch  # noqa: E402
from transformers import (  # noqa: E402
    AutoModelForCausalLM,
    AutoTokenizer,
    Trainer,
    TrainerCallback,
    TrainingArguments,
)

from device_map import BIG, assert_two_gpus, build_device_map  # noqa: E402
from packed_dataset import PackedDataset, collate, manifest  # noqa: E402

DEFAULT_SMALL_LAYERS = 4  # measured 2026-09-07 by device_map.py --probe


def _git() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def _sha(path: str) -> str:
    import hashlib

    try:
        return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    except OSError:
        return ""


class ParamGroupTrainer(Trainer):
    """AdamW with weight decay on matrices only — embeddings, norms and
    biases exempt (OLMo 2 practice; the embedding table is 28 % of params)."""

    def create_optimizer(self):
        if self.optimizer is None:
            decay, no_decay = [], []
            for n, p in self.model.named_parameters():
                if not p.requires_grad:
                    continue
                (
                    no_decay
                    if (p.ndim < 2 or "embed_tokens" in n or "norm" in n)
                    else decay
                ).append(p)
            groups = [
                {"params": decay, "weight_decay": self.args.weight_decay},
                {"params": no_decay, "weight_decay": 0.0},
            ]
            self.optimizer = torch.optim.AdamW(
                groups,
                lr=self.args.learning_rate,
                betas=(self.args.adam_beta1, self.args.adam_beta2),
                eps=self.args.adam_epsilon,
                fused=True,
            )
        return self.optimizer


class Telemetry(TrainerCallback):
    def __init__(self):
        self.t0 = time.monotonic()
        self.history: list[dict] = []
        self.peak: dict[str, float] = {}

    def on_log(self, args, state, control, logs=None, **kw):
        row = {
            "step": state.global_step,
            "epoch": round(state.epoch or 0, 4),
            "elapsed_s": round(time.monotonic() - self.t0),
            "tokens_seen": getattr(state, "num_input_tokens_seen", 0),
        }
        row.update(
            {k: v for k, v in (logs or {}).items() if isinstance(v, (int, float))}
        )
        self.history.append(row)
        for d in range(torch.cuda.device_count()):
            self.peak[f"cuda:{d}"] = max(
                self.peak.get(f"cuda:{d}", 0),
                round(torch.cuda.max_memory_allocated(d) / 2**30, 2),
            )


def load_model(init: str, small_layers: int):
    dm = build_device_map(small_layers)
    model = AutoModelForCausalLM.from_pretrained(
        init, dtype=torch.float32, device_map=dm, attn_implementation="sdpa"
    )
    model.config.use_cache = False
    return model, dm


def val_sets(corpus: str, seq: int) -> dict:
    import glob

    out = {}
    for p in sorted(glob.glob(os.path.join(corpus, "val-*.bin"))):
        if p.endswith(".mask.bin"):
            continue
        name = os.path.basename(p)[len("val-") : -len(".bin")].rsplit("-", 1)[0]
        out.setdefault(name, PackedDataset(corpus, prefix=f"val-{name}", seq=seq))
    return out


def evaluate_only(model, evals: dict, batch: int = 1) -> dict:
    from torch.utils.data import DataLoader

    model.eval()
    out = {}
    with torch.no_grad():
        for name, ds in evals.items():
            tot, n = 0.0, 0
            for b in DataLoader(ds, batch_size=batch, collate_fn=collate):
                labels = b["labels"].to(BIG)
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    logits = model(
                        input_ids=b["input_ids"].to(
                            next(model.model.embed_tokens.parameters()).device
                        ),
                        attention_mask=b["attention_mask"].to(
                            next(model.model.embed_tokens.parameters()).device
                        ),
                    ).logits
                shift_logits = logits[:, :-1, :].float()
                shift_labels = labels[:, 1:]
                loss = torch.nn.functional.cross_entropy(
                    shift_logits.reshape(-1, shift_logits.size(-1)),
                    shift_labels.reshape(-1),
                    ignore_index=-100,
                    reduction="sum",
                )
                k = int((shift_labels != -100).sum())
                tot += float(loss)
                n += k
            out[name] = {
                "loss": round(tot / max(1, n), 4),
                "ppl": round(math.exp(min(20, tot / max(1, n))), 2),
                "tokens": n,
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, choices=(1, 2), default=1)
    ap.add_argument(
        "--corpus",
        required=True,
        help="packed stage dir (train-*.bin, val-*.bin, manifest.json)",
    )
    ap.add_argument(
        "--init",
        required=True,
        help="model dir to start from (base, or the stage-1 endpoint)",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=4e-5)
    ap.add_argument("--warmup-frac", type=float, default=0.05)
    ap.add_argument("--accum", type=int, default=16)
    ap.add_argument("--small-layers", type=int, default=DEFAULT_SMALL_LAYERS)
    ap.add_argument("--eval-steps", type=int, default=100)
    ap.add_argument("--save-steps", type=int, default=200)
    ap.add_argument("--save-total-limit", type=int, default=2)
    ap.add_argument("--resume", default=None)
    ap.add_argument(
        "--smoke", action="store_true", help="20 steps on the first 200 blocks"
    )
    ap.add_argument(
        "--eval-only",
        action="store_true",
        help="per-source val loss of --init on --corpus, then stop",
    )
    args = ap.parse_args()

    assert_two_gpus()
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    man = manifest(args.corpus)
    seq = int(man["seq"])
    t_start = time.time()

    print(
        f"[{time.strftime('%H:%M:%S')}] loading {args.init} across 2 GPUs (3060 layers={args.small_layers})",
        flush=True,
    )
    model, dm = load_model(args.init, args.small_layers)
    evals = val_sets(args.corpus, seq)
    os.makedirs(args.out, exist_ok=True)

    if args.eval_only:
        res = evaluate_only(model, evals)
        print(json.dumps(res, indent=1))
        json.dump(
            {"init": args.init, "corpus": args.corpus, "eval": res, "git": _git()},
            open(
                os.path.join(
                    args.out, f"eval_{os.path.basename(args.init.rstrip('/'))}.json"
                ),
                "w",
            ),
            indent=1,
        )
        return 0

    train_ds = PackedDataset(args.corpus, prefix="train", seq=seq)
    if args.smoke:
        train_ds = torch.utils.data.Subset(
            train_ds, list(range(min(200, len(train_ds))))
        )
        evals = {
            k: torch.utils.data.Subset(v, list(range(min(8, len(v)))))
            for k, v in evals.items()
        }
    steps_per_epoch = max(1, len(train_ds) // args.accum)
    total = 20 if args.smoke else int(steps_per_epoch * args.epochs)
    warm = max(5, int(total * args.warmup_frac)) if args.stage == 1 else 20
    print(
        f"  blocks {len(train_ds):,} = {len(train_ds)*seq:,} tokens/epoch | {steps_per_epoch} steps/epoch x {args.epochs} = {total} steps | warmup {warm} | eval sets {list(evals)}",
        flush=True,
    )

    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    tel = Telemetry()
    targs = TrainingArguments(
        output_dir=args.out,
        max_steps=total,
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="constant_with_warmup" if args.stage == 1 else "linear",
        warmup_steps=warm,
        weight_decay=0.1,
        adam_beta1=0.9,
        adam_beta2=0.95,
        adam_epsilon=1e-8,
        max_grad_norm=1.0,
        bf16=True,
        tf32=True,
        logging_steps=10,
        eval_strategy="steps",
        eval_steps=max(10, args.eval_steps) if not args.smoke else 10,
        eval_on_start=True,
        save_strategy="steps",
        save_steps=max(10, args.save_steps) if not args.smoke else 10,
        save_total_limit=args.save_total_limit,
        load_best_model_at_end=False,
        report_to=[],
        seed=20260824,
        dataloader_num_workers=2,
        remove_unused_columns=False,
        include_num_input_tokens_seen=True,
        gradient_checkpointing=False,  # enabled on the model directly (non-reentrant, cross-device hooks)
        disable_tqdm=True,
    )
    trainer = ParamGroupTrainer(
        model=model,
        args=targs,
        train_dataset=train_ds,
        eval_dataset=evals,
        data_collator=collate,
        callbacks=[tel],
    )
    trainer.train(resume_from_checkpoint=args.resume)
    final_eval = trainer.evaluate()
    # bf16 endpoint beside the fp32 checkpoints
    final_dir = os.path.join(args.out, "final")
    model.to(torch.bfloat16).save_pretrained(final_dir, safe_serialization=True)
    AutoTokenizer.from_pretrained(args.init).save_pretrained(final_dir)
    tokens_seen = getattr(trainer.state, "num_input_tokens_seen", 0)
    hours = (time.time() - t_start) / 3600
    run = {
        "stage": args.stage,
        "init": args.init,
        "corpus": args.corpus,
        "corpus_manifest_sha": _sha(os.path.join(args.corpus, "manifest.json")),
        "device_map": dm,
        "args": vars(args),
        "steps": trainer.state.global_step,
        "epochs": trainer.state.epoch,
        "tokens_seen": tokens_seen,
        "tokens_per_hour": round(tokens_seen / max(hours, 1e-6)),
        "wall_hours": round(hours, 3),
        "peak_gib": tel.peak,
        "final_eval": {
            k: v for k, v in final_eval.items() if isinstance(v, (int, float))
        },
        "history": tel.history,
        "git": _git(),
        "final_dir": final_dir,
    }
    json.dump(run, open(os.path.join(args.out, "run.json"), "w"), indent=1)
    print(
        f"[{time.strftime('%H:%M:%S')}] DONE stage {args.stage}: {trainer.state.global_step} steps, {tokens_seen:,} tokens, {run['tokens_per_hour']/1e6:.2f}M tok/h, peak {tel.peak}; endpoint {final_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
