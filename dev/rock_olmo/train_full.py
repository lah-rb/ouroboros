#!/usr/bin/env python3
"""Full-parameter two-stage training of OLMo 2 1B on corpus v4 (rock venv).

WHAT CHANGED FROM train_lora.py, and why (PROCEDURE.md §19):
  * FULL weights in fp32 with bf16 autocast. LoRA r32 (1.6 % of parameters)
    was measured a null for representation, and bf16 master weights round
    away updates at lr 4e-5.
  * BOTH GPUs. Two ways, chosen by how the script is launched:
      - torchrun --nproc_per_node 2 (the default since the 2x 3090 + NVLink
        rebuild, 2026-09-28): ZeRO-3 via FSDP (FULL_SHARD; FSDP1, see
        FSDP_VERSION for why not FSDP2), one shard per decoder layer. Each card takes its own blocks, and parameters, gradients and
        AdamW moments are sharded across the pair. Probe: 23.1 M tok/h against
        15.0 M for the layer split on the same cards (fsdp_probe.py).
      - plain python: the explicit layer device map (device_map.py), pooled
        memory with the cards running in turn. Kept for --eval-only and
        single-process use. Trainer runs its model-parallel path
        (hf_device_map spans two devices -> _n_gpu = 1).
        CUDA_VISIBLE_DEVICES is pinned to "0,1" before torch loads; unset,
        Trainer would DataParallel a single-device model.
    THE PRECISION REGIME IS THE SAME IN BOTH: fp32 parameters, gradients,
    reductions and AdamW, with only the forward/backward math in bf16
    autocast. Under FSDP, Trainer's bf16 flag would make accelerate reduce
    gradients in bf16, so it stays off there and compute_loss autocasts
    instead.
  * --accum is the GLOBAL number of blocks per optimizer step in both modes
    (ZeRO-3 runs accum / world per rank), so the batch, the step count and the
    LR schedule are identical whichever way the run is launched.
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

  PYTHONUNBUFFERED=1 ./.venv/bin/torchrun --nproc_per_node 2 train_full.py --stage 1 \\
        --corpus ~/corpora/rock-olmo-training/v4/stage1 --init ~/models/OLMo-2-0425-1B \\
        --out ~/models/olmo2-1b-spectra-full/stage1 --epochs 2          # ZeRO-3
  ./.venv/bin/python train_full.py --stage 1 ...                        # layer split
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

# Layer-split path only. 4 was measured for the 3090 + 3060 (2026-09-07); on 2x 3090
# the split point does not change speed (the cards run in turn: 14.93 / 14.95 / 14.97
# M tok/h at 4 / 6 / 8), and 8 balances memory (16.6 / 13.9 GB). Probe 2026-09-28.
DEFAULT_SMALL_LAYERS = 8
WORLD = int(os.environ.get("WORLD_SIZE", "1"))
RANK = int(os.environ.get("RANK", "0"))
DISTRIBUTED = WORLD > 1
MEMDEBUG = os.environ.get("TRAIN_FULL_MEMDEBUG") == "1"
# FSDP1, not FSDP2. Under Trainer / accelerate, FSDP2 REPORTED AND CLIPPED A WRONG
# GRADIENT NORM: 0.73 / 0.56 / 0.77 / 0.68 on smoke steps 1-4, where the single-process
# layer path and FSDP1 both give 1.00 / 0.72 / 1.03 / 0.87 (2026-09-28). That is about
# 1/sqrt(2), the norm of one rank's shard, so max_grad_norm=1.0 would have clipped less
# than the recipe says. FSDP1 also peaks lower (20.95 vs 22.22 GiB). It is deprecated from
# transformers 5.20; this venv pins 5.15. Re-check the FSDP2 norm before moving.
FSDP_VERSION = int(os.environ.get("TRAIN_FULL_FSDP_VERSION", "1"))


def log(msg: str) -> None:
    if RANK == 0:
        print(msg, flush=True)


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
    biases exempt (OLMo 2 practice; the embedding table is 28 % of params).

    Under FSDP it also owns the bf16 autocast (see the module docstring): the
    forward runs in bf16 while every parameter, gradient and reduction stays
    fp32."""

    fsdp_autocast = False

    def compute_loss(self, model, inputs, *args, **kwargs):
        if self.fsdp_autocast:
            with torch.autocast("cuda", dtype=torch.bfloat16):
                return super().compute_loss(model, inputs, *args, **kwargs)
        return super().compute_loss(model, inputs, *args, **kwargs)

    def evaluate(self, eval_dataset=None, ignore_keys=None, metric_key_prefix="eval"):
        """Under FSDP: the SAME per-source losses the layer-split path logs.

        Trainer's distributed eval weights blocks by their loss-token counts
        across ranks, so masked sets (fim, replay, xml_*) come out different
        from the single-process series every earlier stage was measured on
        (2026-09-28 smoke: xml_plain 0.552 against 0.617). Here every rank runs
        every block, which a sharded forward needs anyway, and the loss is the
        mean of per-block losses, exactly as before. The blocks are split
        across the ranks: each rank runs every world-th block, padding with
        block 0 so all ranks take the same number of sharded forwards, and the
        real blocks' losses are gathered and averaged. That is the same mean at
        half the time. Log keys are unchanged (eval_<set>_loss), so
        run_status.py and the queue scripts still read them."""
        if not self.fsdp_autocast:
            return super().evaluate(eval_dataset=eval_dataset, ignore_keys=ignore_keys, metric_key_prefix=metric_key_prefix)
        from transformers.trainer_utils import speed_metrics

        ds = eval_dataset if eval_dataset is not None else self.eval_dataset
        if isinstance(ds, dict):
            metrics = {}
            for name, d in ds.items():
                metrics.update(self.evaluate(eval_dataset=d, metric_key_prefix=f"{metric_key_prefix}_{name}"))
            return metrics
        if isinstance(ds, str):
            ds = self.eval_dataset[ds]
        start = time.time()
        dev = torch.device("cuda", torch.cuda.current_device())
        was_training = self.model.training
        self.model.eval()
        n = len(ds)
        local = []
        with torch.no_grad():
            for s in range(math.ceil(n / WORLD)):
                i = RANK + s * WORLD
                b = collate([ds[i if i < n else 0]])
                with torch.autocast("cuda", dtype=torch.bfloat16):
                    out = self.model(
                        input_ids=b["input_ids"].to(dev),
                        attention_mask=b["attention_mask"].to(dev),
                        labels=b["labels"].to(dev),
                    )
                if i < n:  # the padding forward keeps the ranks in step; its loss is dropped
                    local.append(float(out.loss))
        gathered = [None] * WORLD
        torch.distributed.all_gather_object(gathered, local)
        losses = [x for part in gathered for x in part]
        if was_training:
            self.model.train()
        del out
        torch.cuda.empty_cache()  # hand the eval's logits buffers back before training resumes
        metrics = {f"{metric_key_prefix}_loss": sum(losses) / max(1, len(losses))}
        metrics.update(speed_metrics(metric_key_prefix, start, num_samples=len(losses), num_steps=len(losses)))
        self.log(metrics)
        self.control = self.callback_handler.on_evaluate(self.args, self.state, self.control, metrics)
        return metrics

    def create_optimizer(self, model=None):
        if self.optimizer is None:
            decay, no_decay = [], []
            # BY NAME, not p.ndim: under FSDP1 a sharded parameter is a flattened
            # view, so a matrix would read as 1-D. For OLMo 2 the two tests pick
            # the same set: every 1-D parameter is a norm weight (checked 2026-09-28).
            for n, p in (model if model is not None else self.model).named_parameters():
                if not p.requires_grad:
                    continue
                (
                    no_decay
                    if ("embed_tokens" in n or "norm" in n or n.endswith(".bias"))
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


class OverrideLR(TrainerCallback):
    """Apply a NEW learning rate to a RESUMED run.

    `--lr` alone does not survive a resume: Trainer builds the optimizer and
    scheduler, then `_load_optimizer_and_scheduler` overwrites both from the
    checkpoint (trainer.py:1668), restoring the old rate -- and
    `on_train_begin` fires BEFORE that load (:1524), so the obvious hook is
    too early. The first hook after the load is `on_step_begin`, so this
    patches once, there: the param groups AND `base_lrs`, because a LambdaLR
    recomputes `lr = base_lr * lambda(step)` every step and would undo a
    param-group-only change immediately.

    Used when the pre-registered forgetting bound fires (PROCEDURE.md 19).
    """

    def __init__(self, lr: float):
        self.lr = lr
        self.done = False

    def on_step_begin(
        self, args, state, control, optimizer=None, lr_scheduler=None, **kw
    ):
        if self.done or optimizer is None:
            return
        self.done = True
        for g in optimizer.param_groups:
            g["lr"] = self.lr
            g["initial_lr"] = self.lr
        if lr_scheduler is not None and hasattr(lr_scheduler, "base_lrs"):
            lr_scheduler.base_lrs = [self.lr] * len(lr_scheduler.base_lrs)
        print(
            f"[LR OVERRIDE] resumed at step {state.global_step}; base_lrs -> {self.lr}",
            flush=True,
        )


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
        # Under torchrun each rank reads ONLY its own card: touching the other one
        # opens a CUDA context there (~0.3 GB) inside a sharded run's thin margin.
        devs = [torch.cuda.current_device()] if DISTRIBUTED else range(torch.cuda.device_count())
        for d in devs:
            self.peak[f"cuda:{d}"] = max(
                self.peak.get(f"cuda:{d}", 0),
                round(torch.cuda.max_memory_allocated(d) / 2**30, 2),
            )
        if MEMDEBUG:
            d = torch.cuda.current_device()
            print(f"[mem rank{RANK}] step {state.global_step}: now {torch.cuda.memory_allocated(d) / 2**30:.2f} GiB, "
                  f"peak since last log {torch.cuda.max_memory_allocated(d) / 2**30:.2f} GiB, "
                  f"reserved {torch.cuda.memory_reserved(d) / 2**30:.2f} GiB", flush=True)
            torch.cuda.reset_peak_memory_stats(d)


def load_model(init: str, small_layers: int):
    """(model, device map). Under torchrun the model loads whole on the CPU in
    fp32 and Trainer / accelerate shards it (FSDP2); otherwise the explicit
    layer map places it across both cards."""
    if DISTRIBUTED:
        model = AutoModelForCausalLM.from_pretrained(
            init, dtype=torch.float32, attn_implementation="sdpa"
        )
        model.config.use_cache = False
        return model, {"fsdp": f"FSDP{FSDP_VERSION} full_shard over {WORLD} ranks, one shard per Olmo2DecoderLayer"}
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
                shift_labels = labels[:, 1:].to(shift_logits.device)  # lm_head may sit on either card
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
    ap.add_argument(
        "--stage",
        type=int,
        choices=(0, 1, 2),
        default=1,
        help="0 = FIM primer (stage-1 schedule, recorded as 0), 1 = constant "
        "after warmup, 2 = linear to zero",
    )
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
        "--override-lr",
        action="store_true",
        help="force --lr onto a RESUMED run (the checkpoint's optimizer and "
        "scheduler would otherwise restore the old rate)",
    )
    ap.add_argument(
        "--smoke", action="store_true", help="20 steps on the first 200 blocks"
    )
    ap.add_argument(
        "--eval-only",
        action="store_true",
        help="per-source val loss of --init on --corpus, then stop",
    )
    ap.add_argument(
        "--save-final-fp32",
        action="store_true",
        help="also save the fp32 endpoint as <out>/final_fp32 (the init for "
        "the next stage; chaining from the bf16 `final` rounds the weights)",
    )
    args = ap.parse_args()

    assert_two_gpus()
    if DISTRIBUTED and args.eval_only:
        raise SystemExit("--eval-only runs as one process: launch it with python, not torchrun")
    if DISTRIBUTED and args.accum % WORLD:
        raise SystemExit(f"--accum {args.accum} (global blocks per step) must divide by the {WORLD} ranks")
    if DISTRIBUTED:
        local = int(os.environ.get("LOCAL_RANK", "0"))
        torch.cuda.set_device(local)
        # gloo beside NCCL: loading a sharded optimizer checkpoint on resume runs CPU
        # collectives, and accelerate alone opens an NCCL-only group ("No backend type
        # associated with device type cpu", 2026-09-28 smoke).
        torch.distributed.init_process_group(backend="cpu:gloo,cuda:nccl", device_id=torch.device(f"cuda:{local}"))
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
    man = manifest(args.corpus)
    seq = int(man["seq"])
    t_start = time.time()

    how = f"ZeRO-3 (FSDP{FSDP_VERSION} full_shard) over {WORLD} ranks" if DISTRIBUTED else f"a layer split ({args.small_layers} layers on cuda:1)"
    log(f"[{time.strftime('%H:%M:%S')}] loading {args.init} across 2 GPUs as {how}")
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
    constant = args.stage in (0, 1)
    warm = max(5, int(total * args.warmup_frac)) if constant else 20
    log(
        f"  blocks {len(train_ds):,} = {len(train_ds)*seq:,} tokens/epoch | {steps_per_epoch} steps/epoch x {args.epochs} = {total} steps | warmup {warm} | eval sets {list(evals)}"
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
        gradient_accumulation_steps=args.accum // WORLD,
        learning_rate=args.lr,
        lr_scheduler_type="constant_with_warmup" if constant else "linear",
        warmup_steps=warm,
        weight_decay=0.1,
        adam_beta1=0.9,
        adam_beta2=0.95,
        adam_epsilon=1e-8,
        max_grad_norm=1.0,
        # Under FSDP bf16 stays off (it would reduce gradients in bf16) and
        # compute_loss autocasts instead; see the module docstring.
        bf16=not DISTRIBUTED,
        tf32=True,
        **(
            {
                "fsdp": "full_shard auto_wrap",
                "fsdp_config": (
                    {
                        "version": 1,
                        "transformer_layer_cls_to_wrap": ["Olmo2DecoderLayer"],
                        "reshard_after_forward": "full_shard",
                        "use_orig_params": "true",
                        "sync_module_states": "true",
                        "limit_all_gathers": "true",
                        "state_dict_type": "FULL_STATE_DICT",
                    }
                    if FSDP_VERSION == 1
                    else {
                        "version": 2,
                        "transformer_layer_cls_to_wrap": ["Olmo2DecoderLayer"],
                        "reshard_after_forward": True,
                        "state_dict_type": "FULL_STATE_DICT",
                    }
                ),
            }
            if DISTRIBUTED
            else {}
        ),
        logging_steps=1 if MEMDEBUG else 10,
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
    cbs = [tel]
    if args.override_lr:
        cbs.append(OverrideLR(args.lr))
    trainer = ParamGroupTrainer(
        model=model,
        args=targs,
        train_dataset=train_ds,
        eval_dataset=evals,
        data_collator=collate,
        callbacks=cbs,
    )
    trainer.fsdp_autocast = DISTRIBUTED
    trainer.train(resume_from_checkpoint=args.resume)
    final_eval = trainer.evaluate()
    try:  # a Trainer checkpoint dir (the §22a anneal's init) carries no tokenizer
        tokenizer = AutoTokenizer.from_pretrained(args.init)
    except (OSError, ValueError):
        tokenizer = AutoTokenizer.from_pretrained(os.path.expanduser("~/models/OLMo-2-0425-1B"))
    fp32_dir = os.path.join(args.out, "final_fp32") if args.save_final_fp32 else ""
    final_dir = os.path.join(args.out, "final")
    if DISTRIBUTED:
        # The weights are sharded: Trainer gathers the full fp32 state dict to rank 0
        # (FULL_STATE_DICT). The bf16 endpoint is cast from that file on rank 0.
        gathered = fp32_dir or os.path.join(args.out, "_final_fp32_gathered")
        trainer.save_model(gathered)
        if RANK == 0:
            tokenizer.save_pretrained(gathered)
            AutoModelForCausalLM.from_pretrained(gathered, dtype=torch.bfloat16).save_pretrained(
                final_dir, safe_serialization=True
            )
            tokenizer.save_pretrained(final_dir)
            if not fp32_dir:
                import shutil

                shutil.rmtree(gathered)  # this run's own scratch copy
        torch.distributed.barrier()
    else:
        if fp32_dir:
            model.save_pretrained(fp32_dir, safe_serialization=True)
            tokenizer.save_pretrained(fp32_dir)
        # bf16 endpoint beside the fp32 checkpoints
        model.to(torch.bfloat16).save_pretrained(final_dir, safe_serialization=True)
        tokenizer.save_pretrained(final_dir)
    peak = dict(tel.peak)
    if DISTRIBUTED:  # each rank measured its own card
        mine = {f"cuda:{RANK}": round(torch.cuda.max_memory_allocated() / 2**30, 2)}
        allp = [None] * WORLD
        torch.distributed.all_gather_object(allp, mine)
        peak = {k: v for d in allp for k, v in d.items()}
    if RANK != 0:
        return 0
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
        "peak_gib": peak,
        "parallel": f"fsdp{FSDP_VERSION}-full_shard" if DISTRIBUTED else "layer-split",
        "world": WORLD,
        "final_eval": {
            k: v for k, v in final_eval.items() if isinstance(v, (int, float))
        },
        "history": tel.history,
        "git": _git(),
        "final_dir": final_dir,
        "final_fp32_dir": fp32_dir,
    }
    json.dump(run, open(os.path.join(args.out, "run.json"), "w"), indent=1)
    log(
        f"[{time.strftime('%H:%M:%S')}] DONE stage {args.stage}: {trainer.state.global_step} steps, {tokens_seen:,} tokens, {run['tokens_per_hour']/1e6:.2f}M tok/h, peak {peak}; endpoint {final_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
