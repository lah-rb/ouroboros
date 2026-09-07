"""Feed spectral peaks to the LoRA and inspect its digit probabilities.

WHY THE DISTRIBUTION AND NOT JUST THE TEXT. A wrong answer produced
CONFIDENTLY and a wrong answer produced by near-uniform guessing are
different failures. If the digits come out at p~1.0 the model is
replaying a memorised sequence and the species token is not conditioning
it; if they are flat it has no opinion at all. The generated string
cannot distinguish those, the distribution can.

Runs on CPU so it never contends with the scraper for the GPUs.
"""

from __future__ import annotations

import argparse
import os

os.environ["CUDA_VISIBLE_DEVICES"] = ""

import torch  # noqa: E402
from peft import PeftModel  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

BASE = os.path.expanduser("~/models/OLMo-2-0425-1B")
ADAPTER = os.path.expanduser("~/models/olmo2-1b-spectra-lora")

PROMPTS = [
    # inverse: real calcite bands -> which mineral?
    "A Raman spectrum with bands at 1085, 712, 282 cm-1 is characteristic of",
    # inverse: real quartz bands
    "A Raman spectrum with bands at 464, 206, 128 cm-1 is characteristic of",
    # inverse: real gypsum bands
    "A Raman spectrum with bands at 1008, 494, 415 cm-1 is characteristic of",
    # forward, for contrast
    "Calcite (CaCO3) shows Raman bands at",
    "Quartz (SiO2) shows Raman bands at",
]


def load(with_adapter: bool):
    tok = AutoTokenizer.from_pretrained(BASE)
    model = AutoModelForCausalLM.from_pretrained(BASE, dtype=torch.float32)
    if with_adapter:
        model = PeftModel.from_pretrained(model, ADAPTER)
    model.eval()
    return tok, model


def probe(tok, model, prompt: str, max_new: int, topk: int) -> None:
    ids = tok(prompt, return_tensors="pt")
    with torch.no_grad():
        out = model.generate(
            **ids,
            max_new_tokens=max_new,
            do_sample=False,
            return_dict_in_generate=True,
            output_scores=True,
            pad_token_id=tok.eos_token_id,
        )
    gen = out.sequences[0][ids["input_ids"].shape[1] :]
    text = tok.decode(gen, skip_special_tokens=True)
    print(f"\n> {prompt}")
    print(f"  -> {text.strip()[:200]}")
    print(f"  {'step':>4s} {'token':>10s} {'p':>7s} {'entropy':>8s}   alternatives")
    for i, (tid, score) in enumerate(zip(gen, out.scores)):
        piece = tok.decode([tid])
        if not any(c.isdigit() for c in piece):
            continue
        probs = torch.softmax(score[0].float(), dim=-1)
        p = probs[tid].item()
        # entropy over the whole vocabulary, in nats
        ent = -(probs * torch.log(probs.clamp_min(1e-12))).sum().item()
        top = torch.topk(probs, topk)
        alts = "  ".join(
            f"{tok.decode([t]).strip()!r}:{v:.3f}"
            for v, t in zip(top.values.tolist(), top.indices.tolist())
        )
        print(f"  {i:4d} {piece.strip()!r:>10} {p:7.3f} {ent:8.3f}   {alts}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=24)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument(
        "--base-too", action="store_true", help="also probe the untuned base"
    )
    args = ap.parse_args()

    print("=" * 72)
    print("LoRA-TUNED")
    print("=" * 72)
    tok, model = load(True)
    for p in PROMPTS:
        probe(tok, model, p, args.max_new, args.topk)

    if args.base_too:
        print("\n" + "=" * 72)
        print("BASE (untuned) — the control")
        print("=" * 72)
        del model
        tok, base = load(False)
        for p in PROMPTS[:3]:
            probe(tok, base, p, args.max_new, args.topk)


if __name__ == "__main__":
    main()
