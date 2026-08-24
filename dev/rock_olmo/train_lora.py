"""LoRA fine-tune of OLMo 2 1B on the spectral corpus.

WHAT THIS IS FOR. A first representative pass — the point is to see what
the corpus produces, not to reach a tuned optimum. So the knobs are
conservative and the run is instrumented rather than optimised: loss on
the train stream, loss on the held-out stream, and checkpoints often
enough that a bad run can be abandoned without losing everything.

THE HELD-OUT STREAM IS A REAL EVAL, not a slice of the training set. It
contains 30 species stratified on recurrence and compositional
complexity, absent from every train record — paper text included, since
a paper naming a held-out species is dropped whole. Eval loss on it
therefore measures generalisation to unseen minerals rather than
memorisation, which is the whole reason the split cost 27% of the paper
text.

CORPUS SHAPE (v2): 19,369 weighted records / ~13.2M tokens. Paper
markdown is 91% by token; interconnects and reference material 5.4%.
That ratio is the operator's deliberate choice for a representative
first pass and the first thing to revisit if the model knows facts but
cannot use them.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import time

# ONE GPU, PINNED BEFORE TORCH LOADS. Trainer parallelises across every
# visible device, which put half the batch on the 3060 — and OLMo 2's
# 100,352-token vocabulary makes the logits tensor the memory
# bottleneck: batch x seq x vocab x 2 bytes, upcast to fp32 inside
# cross-entropy. That OOM'd an 11.6 GiB card instantly. The 3090 alone
# is both sufficient and simpler than sharding a 1B model.
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import torch  # noqa: E402
from datasets import Dataset  # noqa: E402
from peft import LoraConfig, get_peft_model
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    Trainer,
    TrainingArguments,
)

MODEL = os.path.expanduser("~/models/OLMo-2-0425-1B")
CORPUS = os.path.expanduser("~/corpora/rock-olmo-training/v2")
OUT = os.path.expanduser("~/models/olmo2-1b-spectra-lora")


def load_split(pattern: str) -> list[dict]:
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(CORPUS, pattern))):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                text = (rec.get("text") or "").strip()
                if text:
                    rows.append({"text": text, "source": rec.get("source", "")})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--seq-len", type=int, default=2048)
    # Batch 2 x seq 2048 keeps the logits tensor (batch*seq*100352) inside
    # the 3090 alongside activations; accumulation carries the effective
    # batch back up to 32.
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--accum", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--eval-cap", type=int, default=400)
    ap.add_argument("--smoke", action="store_true", help="20 steps, tiny eval")
    args = ap.parse_args()

    print(f"[{time.strftime('%H:%M:%S')}] loading corpus", flush=True)
    train_rows = load_split("train-*.jsonl")
    eval_rows = load_split("holdout.jsonl")[: args.eval_cap]
    print(f"  train {len(train_rows):,} records | eval {len(eval_rows):,}", flush=True)

    tok = AutoTokenizer.from_pretrained(MODEL)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    def encode(batch):
        return tok(
            batch["text"],
            truncation=True,
            max_length=args.seq_len,
            padding=False,
        )

    train_ds = Dataset.from_list(train_rows).map(
        encode, batched=True, remove_columns=["text", "source"], desc="tokenising train"
    )
    eval_ds = Dataset.from_list(eval_rows).map(
        encode, batched=True, remove_columns=["text", "source"], desc="tokenising eval"
    )
    tokens = sum(len(x) for x in train_ds["input_ids"])
    print(f"  tokenised: {tokens:,} train tokens", flush=True)

    print(f"[{time.strftime('%H:%M:%S')}] loading {MODEL}", flush=True)
    model = AutoModelForCausalLM.from_pretrained(
        MODEL, dtype=torch.bfloat16, device_map={"": 0}
    )
    model.config.use_cache = False
    model.enable_input_require_grads()

    # Attention + MLP projections. Targeting attention alone underfits a
    # domain shift this large; the MLP is where factual association
    # lives, and at 1B the extra parameters are cheap.
    lora = LoraConfig(
        r=args.rank,
        lora_alpha=args.rank * 2,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    steps_per_epoch = max(1, len(train_ds) // (args.batch * args.accum))
    total = int(steps_per_epoch * args.epochs) if not args.smoke else 20
    print(f"  ~{steps_per_epoch} steps/epoch, running {total}", flush=True)

    targs = TrainingArguments(
        output_dir=OUT,
        num_train_epochs=args.epochs if not args.smoke else 1,
        max_steps=total,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        # transformers 5 dropped warmup_ratio in favour of warmup_steps.
        warmup_steps=max(5, total // 30),
        bf16=True,
        logging_steps=5,
        eval_strategy="steps",
        eval_steps=max(10, total // 8),
        per_device_eval_batch_size=args.batch,
        save_strategy="steps",
        save_steps=max(20, total // 4),
        save_total_limit=3,
        gradient_checkpointing=True,
        report_to=[],
        seed=20260824,
        dataloader_num_workers=2,
    )

    trainer = Trainer(
        model=model,
        args=targs,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=DataCollatorForLanguageModeling(tok, mlm=False),
    )

    base = trainer.evaluate()
    print(f"[{time.strftime('%H:%M:%S')}] BASELINE eval_loss={base['eval_loss']:.4f} "
          f"ppl={math.exp(min(20, base['eval_loss'])):.1f}", flush=True)

    trainer.train()

    final = trainer.evaluate()
    print(f"[{time.strftime('%H:%M:%S')}] FINAL eval_loss={final['eval_loss']:.4f} "
          f"ppl={math.exp(min(20, final['eval_loss'])):.1f}", flush=True)
    trainer.save_model(OUT)
    tok.save_pretrained(OUT)
    with open(os.path.join(OUT, "run.json"), "w") as fh:
        json.dump(
            {
                "corpus": CORPUS,
                "train_records": len(train_rows),
                "train_tokens": tokens,
                "eval_records": len(eval_rows),
                "baseline_eval_loss": base["eval_loss"],
                "final_eval_loss": final["eval_loss"],
                "args": vars(args),
            },
            fh,
            indent=1,
        )
    print(f"saved to {OUT}", flush=True)


if __name__ == "__main__":
    main()
