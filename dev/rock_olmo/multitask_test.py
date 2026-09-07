"""Does predicting a measured PROPERTY help predict a SPECTRUM?

THE QUESTION. Operator's proposal: synthesise properties (hardness,
plasma temperature) from spectra to create more links across the corpus.
Before building any synthesis, this measures whether property-linking
causes transfer AT ALL — using only MEASURED targets, so a negative
result is about the mechanism rather than about the quality of some
estimator we wrote.

THE TRAP THIS AVOIDS. mindat hardness and density are already INPUT
features in `struct_vector` (cell[3], cell[4]). Predicting them as
auxiliary targets while feeding them in is an identity map: it would
train to ~zero loss, look like a working auxiliary task, and measure
nothing. Every arm here therefore ZEROES those two inputs. This is the
same vacuous-verification failure as a rate gate that passes on zero
checkable items — it has to be able to fail.

THE ARMS.
  base   no property inputs, no auxiliary task      <- the floor
  multi  no property inputs, + auxiliary property head
  given  property INPUTS restored, no auxiliary     <- the ceiling:
         what the property is worth when simply handed over

`multi` minus `base` is the transfer effect: what predicting a property
buys when the property is not given. `given` minus `base` bounds it —
if auxiliary supervision recovered the whole gap it would match `given`,
which would be a strong result and is not expected.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from augmented_mlp import cif_vector, load_cifs, load_mindat, struct_vector, _f
from composition_mlp import comp_vector
from train_spectra_head import (  # noqa: E402
    DATA,
    N_BINS,
    build_split,
    spectral_loss,
    TRIVIAL_COS,
    NOISE_CEIL,
)

# struct_vector tail: [... log a, c/a, b/a, log dcalc, hardness/10, sg, sg_flag, present]
PROP_IDX = (-5, -4)  # log density, hardness inside struct_vector
CIF_DENSITY_IDX = 4  # cif_vector: [logbond, meanCN, maxCN, logvol, LOGDENSITY, ...]


class MultiHead(nn.Module):
    """Shared trunk, one head per task.

    The trunk is where transfer would happen if it happens at all: the
    property head can only influence the spectrum head through the
    representation they share.
    """

    def __init__(self, dim: int, bins: int = N_BINS, n_props: int = 2):
        super().__init__()
        self.trunk = nn.Sequential(
            nn.Linear(dim, 1024),
            nn.GELU(),
            nn.Dropout(0.1),
            nn.Linear(1024, 1024),
            nn.GELU(),
        )
        self.spectrum = nn.Sequential(nn.Linear(1024, bins), nn.Softplus())
        self.props = nn.Linear(1024, n_props)

    def forward(self, x):
        h = self.trunk(x)
        return self.spectrum(h) + 1e-6, self.props(h)


def property_targets(
    data: dict, mind: dict
) -> dict[str, tuple[list[float], list[float]]]:
    """species -> (targets, mask). log density and Mohs hardness.

    Both are MEASURED values from mindat, not estimates. Mask marks the
    ~16% of species where one is absent, so a missing value contributes
    no gradient rather than training the head toward zero.
    """
    out = {}
    for n in data:
        rec = mind.get(n.lower()) or {}
        # RAW mindat keys: augmented_mlp.load_mindat does not rename fields.
        d, h = _f(rec.get("dcalc")), _f(rec.get("hmax"))
        out[n] = (
            [math.log(d) if d else 0.0, h / 10.0 if h else 0.0],
            [1.0 if d else 0.0, 1.0 if h else 0.0],
        )
    return out


def run(seed: int, arm: str, epochs: int, lam: float) -> tuple[float, float, float]:
    data = json.load(open(DATA))
    hp = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
    )
    holdout = set(json.load(open(hp))) if os.path.exists(hp) else set()
    tr, va, he = build_split(data, holdout, seed=seed)
    mind, cifs = load_mindat(), load_cifs()
    props = property_targets(data, mind)

    vecs = {}
    for n in data:
        sv = struct_vector(mind.get(n.lower()))
        gv = cif_vector(cifs.get(n.lower()))
        if arm != "given":
            for i in PROP_IDX:  # strip density + hardness from the INPUT
                sv[i] = 0.0
            # AND from the CIF block, which carries its own log(density).
            # Zeroing only the mindat copy left density readable for the 44%
            # of species with an AMCSD entry, making that auxiliary target
            # partly an identity map — the same vacuous-test failure this
            # experiment is built to avoid.
            gv[CIF_DENSITY_IDX] = 0.0
        vecs[n] = comp_vector(data[n]["formula"]) + sv + gv

    # Standardise property targets on TRAIN species only. Fitting the
    # scaler on everything would leak holdout statistics into training.
    tr_t = [[props[n][0][j] for n in tr if props[n][1][j]] for j in (0, 1)]
    # Guard rather than crash: an empty column means the property never
    # resolved, which is a wiring bug (wrong key name) and should say so.
    for j, col in enumerate(tr_t):
        if not col:
            raise RuntimeError(
                f"property column {j} is empty across {len(tr)} train species — "
                "the mindat field name is probably wrong"
            )
    mu = [statistics.mean(c) for c in tr_t]
    prop_sd = [statistics.pstdev(c) or 1.0 for c in tr_t]

    def rows(names):
        return [
            (n, sp) for n in names if data[n]["spectra"] for sp in data[n]["spectra"]
        ]

    train, val, held = rows(tr), rows(va), rows(he)
    torch.manual_seed(seed)
    net = MultiHead(len(next(iter(vecs.values())))).float()
    opt = torch.optim.AdamW(net.parameters(), lr=3e-4, weight_decay=0.01)

    def batch(chunk):
        x = torch.tensor([vecs[a[0]] for a in chunk])
        y = torch.tensor([a[1] for a in chunk])
        pt = torch.tensor(
            [[(props[a[0]][0][j] - mu[j]) / prop_sd[j] for j in (0, 1)] for a in chunk]
        )
        pm = torch.tensor([props[a[0]][1] for a in chunk])
        return x, y, pt, pm

    def evaluate(rs):
        net.eval()
        sims = []
        with torch.no_grad():
            for i in range(0, len(rs), 32):
                x, y, _, _ = batch(rs[i : i + 32])
                sp, _ = net(x)
                sims.append(F.cosine_similarity(sp, y, dim=-1))
        net.train()
        return torch.cat(sims).mean().item()

    rng = random.Random(1)
    hist = []
    for _ in range(epochs):
        rng.shuffle(train)
        for i in range(0, len(train), 32):
            x, y, pt, pm = batch(train[i : i + 32])
            sp, pp = net(x)
            loss = spectral_loss(sp, y)
            if arm == "multi" and lam:
                # Masked L1 so absent measurements contribute nothing.
                per = (pp - pt).abs() * pm
                denom = pm.sum().clamp_min(1.0)
                loss = loss + lam * (per.sum() / denom)
            opt.zero_grad()
            loss.backward()
            opt.step()
        hist.append((evaluate(val), evaluate(held)))
    tv = sum(h[0] for h in hist[-10:]) / len(hist[-10:])
    th = sum(h[1] for h in hist[-10:]) / len(hist[-10:])
    return tv, th, 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument(
        "--seeds", type=int, nargs="*", default=[20260824, 7, 1337, 42, 2024]
    )
    ap.add_argument("--lambdas", type=float, nargs="*", default=[0.3])
    args = ap.parse_args()

    print(f"trivial {TRIVIAL_COS:.4f} | noise ceiling {NOISE_CEIL:.4f}")
    print(
        "(hardness+density REMOVED from the input in base/multi; "
        "restored only in 'given')\n"
    )
    results = {}
    arms = [("base", 0.0), ("given", 0.0)] + [("multi", l) for l in args.lambdas]
    for arm, lam in arms:
        vs, hs = [], []
        for sd in args.seeds:
            v, h, _ = run(sd, arm, args.epochs, lam)
            vs.append(v)
            hs.append(h)
        label = f"{arm}" + (f" (lam={lam})" if arm == "multi" else "")
        results[label] = (vs, hs)
        print(
            f"  {label:18s} val {statistics.mean(vs):.4f}±{statistics.stdev(vs):.4f}   "
            f"holdout {statistics.mean(hs):.4f}±{statistics.stdev(hs):.4f}   "
            f"{100*(statistics.mean(hs)-TRIVIAL_COS)/(NOISE_CEIL-TRIVIAL_COS):5.1f}% of gap"
        )

    base_h = results["base"][1]
    print("\npaired against base, on holdout:")
    for label, (_, hs) in results.items():
        if label == "base":
            continue
        d = [a - b for a, b in zip(hs, base_h)]
        m, s = statistics.mean(d), statistics.stdev(d)
        t = m / (s / len(d) ** 0.5) if s else float("inf")
        print(
            f"  {label:18s} {m:+.4f} ± {s:.4f}  t={t:5.2f}  "
            f"{'SIGNIFICANT' if abs(t) > 2.8 else 'not significant'}"
        )


if __name__ == "__main__":
    main()
