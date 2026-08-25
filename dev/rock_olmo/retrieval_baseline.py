"""The control the head has to beat: retrieval, with no language model.

WHY THIS EXISTS. A head that maps "Raman spectrum of the mineral X,
composition Y" to a spectrum can succeed two ways. It can have learned
something about how composition and bonding produce vibrational bands —
the result worth having — or it can have learned "minerals whose formulas
look alike have spectra that look alike", which is true, useful, and
requires no model at all. Only the second is measured by val_cos on its
own, so the honest comparison is against a retrieval baseline that does
exactly and only that second thing.

METHOD. Parse each formula to an element->count vector, normalise it,
and for an unseen mineral copy the mean spectrum of the k nearest
training minerals by composition cosine. Deterministic, CPU, no
training. If the LLM head cannot beat this, the LLM is decoration.

Reported on the SAME val and holdout species splits as the head, from
the same seed, so the numbers are directly comparable.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from train_spectra_head import DATA, build_split, TRIVIAL_COS, NOISE_CEIL  # noqa: E402

_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*\.?\d*)")


def composition(formula: str) -> dict[str, float]:
    """Element -> count. Subscript groups and hydration dots are flattened
    rather than parsed properly: this baseline should be strong but it is
    not the deliverable, and a full formula parser would be its own bug
    surface. Parentheses multiply nothing here, which understates a few
    complex silicates and so if anything makes the baseline HARDER to
    beat only where it is already weak."""
    out: dict[str, float] = {}
    for sym, num in _TOKEN.findall(formula or ""):
        try:
            n = float(num) if num else 1.0
        except ValueError:
            n = 1.0
        out[sym] = out.get(sym, 0.0) + n
    return out


def cos_dict(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if not na or not nb:
        return 0.0
    return sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys) / (na * nb)


def cos_vec(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


def mean_spectrum(spectra: list[list[float]]) -> list[float]:
    n = len(spectra)
    return [sum(s[i] for s in spectra) / n for i in range(len(spectra[0]))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--seed",
        type=int,
        default=20260824,
        help="MUST match the head run's --seed; a different seed is a "
        "different species split and the two are then not comparable",
    )
    args = ap.parse_args()
    data = json.load(open(DATA))
    hp = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
    )
    holdout = set(json.load(open(hp))) if os.path.exists(hp) else set()
    tr, va, he = build_split(data, holdout, seed=args.seed)

    comps = {n: composition(data[n]["formula"]) for n in data}
    means = {n: mean_spectrum(data[n]["spectra"]) for n in data if data[n]["spectra"]}
    train = [n for n in tr if n in means]

    for k in (1, 3, 5):
        for label, names in (("val", va), ("holdout", he)):
            sims, exact = [], 0
            for n in names:
                if n not in means:
                    continue
                ranked = sorted(train, key=lambda t: -cos_dict(comps[n], comps[t]))[:k]
                if ranked and cos_dict(comps[n], comps[ranked[0]]) > 0.999:
                    exact += 1
                pred = mean_spectrum([means[t] for t in ranked])
                # score against every real spectrum, as the head is scored
                sims += [cos_vec(pred, s) for s in data[n]["spectra"]]
            share = 100 * exact / max(1, len([n for n in names if n in means]))
            print(
                f"  k={k}  {label:8s} cos {sum(sims)/max(1,len(sims)):.4f}   "
                f"(n={len(sims)} spectra; {share:.0f}% had an exact-composition match in train)"
            )
    print(f"\n  reference: trivial {TRIVIAL_COS:.4f} | noise ceiling {NOISE_CEIL:.4f}")


if __name__ == "__main__":
    main()
