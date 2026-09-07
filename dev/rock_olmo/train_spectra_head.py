"""Species -> Raman spectrum via a regression head. The AtomGPT shape.

WHAT CHANGED FROM THE TOKEN RUN. That run asked a 1B model to emit band
positions as digits out of a 100k vocabulary and it could not: entropy
4.5 nats across plausible three-digit numbers, identical output for
Quartz and Hematite. AtomGPT does not do that — for numeric properties
it attaches a regression head to the transformer. This is that, with a
128-bin spectrum in place of a scalar.

WHY THE FIRST ATTEMPT AT *THIS* ALSO FAILED, AND WHAT IT COST TO SEE IT.
Run 1 used L1, copying AtomGPT directly. It reported val 0.0747 against
a "trivial" 0.1013 and looked like a 26% win. It was not. The head had
converged to ALL ZEROS: pairwise L1 between its predictions for Quartz,
Calcite, Gypsum, Hematite, Pyrite and Diamond was 0.00000, and every
output bin was 0.000.

The cause is the target, not the model. A peak-normalised Raman spectrum
is SPARSE — a flat baseline near zero with a few narrow bands. Under L1
the optimal constant is the per-bin MEDIAN, and for sparse bins that
median is ~0. So "predict nothing" is a genuine L1 minimum, and it even
beat the per-bin mean (0.0802 median vs 0.1013 mean) which is what made
the number look like progress. AtomGPT regresses DENSE scalars
(formation energy, band gap); L1 is fine there and misleading here.

TWO LESSONS WORTH KEEPING:
  * the L1-optimal constant is the median, so quote the MEDIAN as the
    trivial baseline whenever L1 is the metric
  * a metric a degenerate predictor can win is not a metric — check that
    outputs VARY WITH INPUT before believing any headline number

THE OBJECTIVE NOW is cosine distance (equivalently spectral angle, SAM),
which is what spectroscopy uses to compare spectra and what mineral
matching libraries score on. It is scale-invariant and UNDEFINED for the
zero vector, so the collapse that ate run 1 is not reachable. Reported
in both cos and radians.

MEASURED BOUNDS, computed before training so the result can be judged:
  different species     cos 0.2316  SAM 1.3313   <- floor; unrelated pairs
  predict-the-mean      cos 0.4600  SAM 1.0856   <- trivial, must beat
  same species x2       cos 0.8874  SAM 0.4054   <- ceiling; measurement noise
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "1")  # 3060; 3090 runs the scraper

import torch  # noqa: E402
import torch.nn as nn  # noqa: E402

_TORCH_THREADS = int(os.environ.get("ROCK_OLMO_THREADS", "12"))
import torch.nn.functional as F  # noqa: E402
from transformers import AutoModel, AutoTokenizer  # noqa: E402

BASE = os.path.expanduser("~/models/OLMo-2-0425-1B")
EMB_CACHE = os.path.expanduser("~/corpora/rock-olmo-training/emb_cache")
DATA = os.path.expanduser("~/corpora/rock-olmo-training/spectra_binned.json")
OUT = os.path.expanduser("~/models/olmo2-1b-spectra-head")
N_BINS = 128

# Cosine similarity, higher is better. See the module docstring for how
# these were measured (dev/rock_olmo/PROCEDURE.md §12).
TRIVIAL_COS = 0.4600  # predict the corpus mean spectrum
NOISE_CEIL = 0.8874  # two measurements of the SAME mineral
UNRELATED_COS = 0.2316  # two different minerals


class SpectrumHead(nn.Module):
    """Pooled LLM state -> binned spectrum.

    Softplus, not sigmoid. The target is peak-normalised to [0, 1] so
    sigmoid looks right, but under a scale-invariant loss the output
    scale is free and sigmoid's saturation only costs gradient near the
    band peaks — exactly the bins that carry the information.
    """

    def __init__(self, hidden: int, bins: int = N_BINS):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(hidden, 1024),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(1024, 1024),
            nn.GELU(),
            nn.Linear(1024, bins),
            nn.Softplus(),
        )

    def forward(self, x):
        # +eps: a genuinely zero output has no cosine, so keep the vector
        # off the origin rather than letting the loss go undefined.
        return self.net(x) + 1e-6


def spectral_loss(pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    """1 - cos. Scale-invariant, and the all-zeros collapse cannot win it."""
    return (1.0 - F.cosine_similarity(pred, target, dim=-1)).mean()


PROMPT_MODES = ("both", "name", "formula", "none")


def prompt_for(species: str, formula: str, mode: str = "both") -> str:
    """Which half of the prompt carries the signal?

    This is the ablation that separates the two stories. `formula` gives
    the head exactly what the retrieval control has, so beating retrieval
    in formula mode would mean the LLM reads composition better than a
    element-count cosine does. `name` gives it only the mineral name,
    where any advantage has to come from what OLMo already knew before we
    touched it. `none` is the negative control: a constant prompt carries
    zero information, so a head that still scores above trivial there is
    reading something other than its input and the run is void."""
    if mode == "name":
        return f"Raman spectrum of the mineral {species}."
    if mode == "formula":
        return f"Raman spectrum of a mineral. Composition: {formula}."
    if mode == "none":
        return "Raman spectrum of a mineral."
    return f"Raman spectrum of the mineral {species}. Composition: {formula}."


def build_split(data: dict, holdout: set[str], seed: int = 20260824):
    """Split BY SPECIES. Two spectra of one mineral in different splits
    would leak the answer — the eval must be minerals never seen."""
    rng = random.Random(seed)
    names = sorted(k for k in data if k not in holdout)
    rng.shuffle(names)
    n_val = max(20, len(names) // 10)
    val, train = names[:n_val], names[n_val:]
    held = sorted(k for k in data if k in holdout)
    return train, val, held


def make_rows(data: dict, names: list[str]) -> list[tuple[str, list[float]]]:
    """(species, spectrum) pairs. Species is the cache key, not the text,
    so the embedder can memoise the frozen backbone's one answer per
    mineral instead of recomputing it for each of its spectra."""
    return [(n, s) for n in names for s in data[n]["spectra"]]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--max-len", type=int, default=48)
    ap.add_argument(
        "--lora",
        action="store_true",
        help="unfreeze the backbone with LoRA; tests whether the "
        "frozen representation, not the head, is the limit",
    )
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument(
        "--lm-adapter",
        default="",
        help="path to a CAUSAL-LM LoRA (e.g. the run-2 corpus adapter) to "
        "merge into the frozen backbone before extracting features. "
        "Tests whether domain-adapting the LM improves its "
        "REPRESENTATION for the spectrum task, which is a different "
        "question from whether it can generate the answer.",
    )
    ap.add_argument(
        "--seed",
        type=int,
        default=20260824,
        help="species-split seed; vary it to size run-to-run noise, "
        "which is the same order as the head-vs-retrieval gap",
    )
    ap.add_argument(
        "--grad-ckpt",
        action="store_true",
        help="trade compute for VRAM; the 3060 shares 12 GiB with paddle",
    )
    ap.add_argument(
        "--tag",
        default="",
        help="suffix for the output dir; pass as "
        "--tag=-s7, since a leading dash reads as a flag",
    )
    ap.add_argument(
        "--prompt-mode",
        default="both",
        choices=PROMPT_MODES,
        help="ablation: see prompt_for(). 'none' is the negative control",
    )
    ap.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda"),
        help="the frozen arm is cheap on CPU (1,952 unique prompts embed "
        "once, then it is an MLP), which frees the contended 3060",
    )
    args = ap.parse_args()
    out_dir = OUT + (args.tag or ("-lora" if args.lora else ""))

    data = json.load(open(DATA))
    holdout_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
    )
    holdout = (
        set(json.load(open(holdout_path))) if os.path.exists(holdout_path) else set()
    )
    tr_names, va_names, he_names = build_split(data, holdout, seed=args.seed)
    train, val, held = (make_rows(data, n) for n in (tr_names, va_names, he_names))
    print(f"species train/val/holdout: {len(tr_names)}/{len(va_names)}/{len(he_names)}")
    print(f"spectra train/val/holdout: {len(train)}/{len(val)}/{len(held)}", flush=True)

    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    if _TORCH_THREADS:
        torch.set_num_threads(_TORCH_THREADS)
    dev = (
        ("cuda" if torch.cuda.is_available() else "cpu")
        if args.device == "auto"
        else args.device
    )
    _dtype = torch.float32 if dev == "cpu" else torch.bfloat16
    if args.lm_adapter:
        # The adapter was trained on AutoModelForCausalLM, whose module
        # names carry a `model.` prefix the bare AutoModel does not have.
        # Load the causal LM, merge the LoRA into the weights, then take
        # its inner transformer as the encoder — merging avoids carrying
        # the peft wrapper (and its name mangling) into feature extraction.
        from peft import PeftModel
        from transformers import AutoModelForCausalLM

        _lm = AutoModelForCausalLM.from_pretrained(BASE, dtype=_dtype)
        _lm = PeftModel.from_pretrained(_lm, os.path.expanduser(args.lm_adapter))
        _lm = _lm.merge_and_unload()
        backbone = _lm.model.to(dev)
        print(f"backbone: base + merged LM adapter {args.lm_adapter}", flush=True)
    else:
        backbone = AutoModel.from_pretrained(BASE, dtype=_dtype).to(dev)

    if args.lora:
        from peft import LoraConfig, get_peft_model

        backbone = get_peft_model(
            backbone,
            LoraConfig(
                r=args.lora_r,
                lora_alpha=args.lora_r * 2,
                lora_dropout=0.05,
                target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
                bias="none",
                task_type="FEATURE_EXTRACTION",
            ),
        )
        backbone.print_trainable_parameters()
        if args.grad_ckpt:
            backbone.gradient_checkpointing_enable()
            backbone.enable_input_require_grads()
        backbone.train()
    else:
        backbone.eval()
        for p in backbone.parameters():
            p.requires_grad = False  # frozen: this tests the REPRESENTATION

    head = SpectrumHead(backbone.config.hidden_size).to(dev).float()
    params = list(head.parameters()) + (
        [p for p in backbone.parameters() if p.requires_grad] if args.lora else []
    )
    opt = torch.optim.AdamW(params, lr=args.lr, weight_decay=0.01)

    prompts = {n: prompt_for(n, data[n]["formula"], args.prompt_mode) for n in data}
    cache: dict[str, torch.Tensor] = {}

    # DISK CACHE of the frozen backbone's pooled state, keyed by prompt mode.
    # The seed changes only the SPLIT, never the embeddings, so an ablation of
    # M modes over S seeds needs M backbone passes, not M*S. Skipped under
    # --lora, where the backbone weights move and yesterday's vectors are wrong.
    _bk = os.path.basename(BASE)
    if args.lm_adapter:
        _bk += "+" + os.path.basename(
            os.path.normpath(os.path.expanduser(args.lm_adapter))
        )
    cache_path = f"{EMB_CACHE}_{args.prompt_mode}_{_bk}.pt"
    if not args.lora and os.path.exists(cache_path):
        cache = {
            k: v.to(dev) for k, v in torch.load(cache_path, map_location="cpu").items()
        }
        print(
            f"embedding cache HIT: {len(cache)} species from {cache_path}", flush=True
        )
    if args.prompt_mode == "none":
        print(
            "NEGATIVE CONTROL: every prompt is identical; anything above "
            f"trivial {TRIVIAL_COS:.4f} means a leak, not learning.",
            flush=True,
        )

    def embed(names: list[str], grad: bool) -> torch.Tensor:
        """Frozen backbone -> memoise per species. 1,952 unique prompts
        serve 9,655 spectra, so without this ~80% of every epoch's
        forward passes recompute an answer that cannot have changed."""
        if not grad and all(n in cache for n in names):
            return torch.stack([cache[n] for n in names])
        enc = tok(
            [prompts[n] for n in names],
            return_tensors="pt",
            padding=True,
            truncation=True,
            max_length=args.max_len,
        ).to(dev)
        ctx = torch.enable_grad() if grad else torch.no_grad()
        with ctx:
            out = backbone(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).to(out.dtype)
            # Mean-pool over real tokens. Last-token pooling puts everything
            # on the final full stop, which carries little about the mineral.
            pooled = ((out * mask).sum(1) / mask.sum(1).clamp_min(1)).float()
        if not grad:
            for n, v in zip(names, pooled):
                cache[n] = v.detach()
        return pooled

    def evaluate(rows) -> tuple[float, float]:
        """Returns (mean cos, mean pairwise cos BETWEEN PREDICTIONS).

        The second number is the collapse detector that run 1 lacked. If
        the head ignores its input, every prediction is the same vector
        and self-similarity pins at 1.0 — visible from epoch 0, no matter
        how respectable the headline score looks."""
        head.eval()
        if args.lora:
            backbone.eval()
        sims, preds = [], []
        with torch.no_grad():
            for i in range(0, len(rows), args.batch):
                chunk = rows[i : i + args.batch]
                y = torch.tensor([c[1] for c in chunk], device=dev)
                p = head(embed([c[0] for c in chunk], grad=False))
                sims.append(F.cosine_similarity(p, y, dim=-1))
                preds.append(p[:: max(1, len(chunk) // 4)])
        head.train()
        if args.lora:
            backbone.train()
        cos = torch.cat(sims).mean().item()
        P = F.normalize(torch.cat(preds), dim=-1)
        M = P @ P.T
        n = M.shape[0]
        self_sim = ((M.sum() - M.diagonal().sum()) / max(1, n * (n - 1))).item()
        return cos, self_sim

    if not args.lora and not cache:
        todo = sorted(prompts)
        t0 = time.time()
        for i in range(0, len(todo), 64):
            embed(todo[i : i + 64], grad=False)
        torch.save({k: v.cpu() for k, v in cache.items()}, cache_path)
        print(
            f"embedded {len(cache)} species in {time.time()-t0:.0f}s -> {cache_path}",
            flush=True,
        )

    print(
        f"\nbounds (cosine, higher better): unrelated {UNRELATED_COS:.4f} | "
        f"trivial {TRIVIAL_COS:.4f} | noise ceiling {NOISE_CEIL:.4f}\n",
        flush=True,
    )
    best, best_ep = -1e9, -1
    history: list[tuple[float, float]] = []
    rng = random.Random(1)
    for ep in range(args.epochs):
        rng.shuffle(train)
        head.train()
        run, nb = 0.0, 0
        for i in range(0, len(train), args.batch):
            chunk = train[i : i + args.batch]
            y = torch.tensor([c[1] for c in chunk], device=dev)
            loss = spectral_loss(head(embed([c[0] for c in chunk], grad=args.lora)), y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            run += loss.item()
            nb += 1
        v, v_self = evaluate(val)
        h, _ = evaluate(held) if held else (float("nan"), 0.0)
        history.append((v, h))
        flag = ""
        if v > best:
            best, best_ep = v, ep
            os.makedirs(out_dir, exist_ok=True)
            torch.save(head.state_dict(), os.path.join(out_dir, "head.pt"))
            if args.lora:
                backbone.save_pretrained(os.path.join(out_dir, "lora"))
            flag = " *"
        print(
            f"[{time.strftime('%H:%M:%S')}] epoch {ep:3d} train_loss {run/max(1,nb):.4f} "
            f"val_cos {v:.4f} holdout_cos {h:.4f} self_sim {v_self:.3f}{flag}",
            flush=True,
        )

    tail_v = sum(t[0] for t in history[-10:]) / max(1, len(history[-10:]))
    tail_h = sum(t[1] for t in history[-10:]) / max(1, len(history[-10:]))
    print(f"\nbest val cos {best:.4f} at epoch {best_ep}")
    print(
        f"last-10-epoch mean: val {tail_v:.4f}  holdout {tail_h:.4f}   <- quote THIS, not the argmax"
    )
    span = NOISE_CEIL - TRIVIAL_COS
    print(
        f"  trivial {TRIVIAL_COS:.4f} -> ceiling {NOISE_CEIL:.4f}; "
        f"closed {100*(best-TRIVIAL_COS)/span:+.1f}% of the gap"
    )
    os.makedirs(out_dir, exist_ok=True)
    json.dump(
        {
            "best_val_cos": best,
            "best_epoch": best_ep,
            "tail10_val_cos": tail_v,
            "tail10_holdout_cos": tail_h,
            "seed": args.seed,
            "trivial": TRIVIAL_COS,
            "noise_ceiling": NOISE_CEIL,
            "unrelated": UNRELATED_COS,
            "lora": args.lora,
            "prompt_mode": args.prompt_mode,
            "species_train": len(tr_names),
        },
        open(os.path.join(out_dir, "run.json"), "w"),
        indent=1,
    )


if __name__ == "__main__":
    main()
