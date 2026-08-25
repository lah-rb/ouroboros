"""The last control: no language model at all.

THE ABLATION FORCED THIS. Prompt-mode results (PROCEDURE.md §13):
formula-only 0.6308 val, name+formula 0.5851, name-only 0.4101 against a
constant-prompt control of 0.4599. The mineral NAME is worse than
useless -- below the negative control -- and adding it to the formula
costs 0.046. So the entire signal is the chemical formula STRING, and
OLMo's pretrained knowledge of mineral names contributes nothing.

That raises the obvious question this script answers: if the only useful
input is a formula, does running it through a 1B-parameter language
model beat just parsing it? Same element-count vector the retrieval
control already builds, same head architecture, same loss, same splits,
same seeds -- but a 100-dim composition vector in place of a 2048-dim
pooled transformer state.

Three outcomes and what each would mean:
  MLP ~= formula-only   the LLM is decoration; ship the MLP
  MLP <  formula-only   the LLM's tokenisation of a formula carries
                        something an element count does not (ratios,
                        hydration, oxidation-state notation)
  MLP >  formula-only   the LLM is actively LOSING information that is
                        present in the formula, which would be the most
                        interesting result of the three
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from retrieval_baseline import composition  # noqa: E402
from train_spectra_head import (  # noqa: E402
    DATA,
    N_BINS,
    SpectrumHead,
    build_split,
    spectral_loss,
    TRIVIAL_COS,
    NOISE_CEIL,
)

# Everything through the actinides. Fixed order so the vector is stable
# across runs; an unknown symbol is dropped rather than hashed, because a
# collision would silently merge two elements.
ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni "
    "Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe "
    "Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg "
    "Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U"
).split()
IDX = {e: i for i, e in enumerate(ELEMENTS)}


def comp_vector(formula: str) -> list[float]:
    """Element counts, L2-normalised. Normalised because Raman band
    POSITIONS depend on which bonds are present, not on how the formula
    happens to be scaled -- Fe2O3 and Fe4O6 are the same mineral."""
    v = [0.0] * len(ELEMENTS)
    for sym, n in composition(formula).items():
        if sym in IDX:
            v[IDX[sym]] = n
    norm = sum(x * x for x in v) ** 0.5
    return [x / norm for x in v] if norm else v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=20260824)
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    data = json.load(open(DATA))
    hp = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
    )
    holdout = set(json.load(open(hp))) if os.path.exists(hp) else set()
    tr, va, he = build_split(data, holdout, seed=args.seed)
    vecs = {n: comp_vector(data[n]["formula"]) for n in data}

    def rows(names):
        return [(n, s) for n in names if data[n]["spectra"] for s in data[n]["spectra"]]

    train, val, held = rows(tr), rows(va), rows(he)
    head = SpectrumHead(len(ELEMENTS), N_BINS).float()
    opt = torch.optim.AdamW(head.parameters(), lr=args.lr, weight_decay=0.01)

    def evaluate(rs):
        head.eval()
        sims = []
        with torch.no_grad():
            for i in range(0, len(rs), args.batch):
                c = rs[i : i + args.batch]
                x = torch.tensor([vecs[a[0]] for a in c])
                y = torch.tensor([a[1] for a in c])
                sims.append(F.cosine_similarity(head(x), y, dim=-1))
        head.train()
        return torch.cat(sims).mean().item()

    rng = random.Random(1)
    hist = []
    for ep in range(args.epochs):
        rng.shuffle(train)
        for i in range(0, len(train), args.batch):
            c = train[i : i + args.batch]
            x = torch.tensor([vecs[a[0]] for a in c])
            y = torch.tensor([a[1] for a in c])
            loss = spectral_loss(head(x), y)
            opt.zero_grad()
            loss.backward()
            opt.step()
        hist.append((evaluate(val), evaluate(held)))

    tv = sum(h[0] for h in hist[-10:]) / len(hist[-10:])
    th = sum(h[1] for h in hist[-10:]) / len(hist[-10:])
    print(f"seed {args.seed}  NO-LLM composition MLP")
    print(
        f"  last-10-epoch mean: val {tv:.4f}  holdout {th:.4f}   "
        f"(trivial {TRIVIAL_COS:.4f}, ceiling {NOISE_CEIL:.4f})"
    )


if __name__ == "__main__":
    main()
