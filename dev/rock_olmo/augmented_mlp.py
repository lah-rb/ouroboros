"""Does STRUCTURE close the gap that composition provably cannot?

THE ARGUMENT THIS TESTS. Run 2 established that everything the system
knows it reads off the chemical formula, and that composition is
insufficient by construction for polymorphs: Anatase and Rutile are both
TiO2, their Raman spectra differ completely, and every composition-based
predictor we have scores at or below the unrelated-minerals floor on
them. Raman reads bonding geometry. Composition does not encode bonding
geometry. So the ceiling is a MISSING INPUT, not a missing parameter --
which is consistent with LoRA being a null result.

mindat's geomaterials dump carries the missing input for 94.8% of our
species: crystal system, space-group number, cell parameters, and the
Strunz class. It separates every polymorph pair we failed on -- Anatase
(a=3.78, c=9.51) and Rutile (a=4.59, c=2.96) are both tetragonal but
have completely different cells.

This script is the cheapest possible test of that claim: the same MLP,
the same splits, the same loss, composition features PLUS structure, so
the only thing that changes is the input. If holdout does not move, the
augmentation is not worth building.

FEATURE NOTES:
  * cell lengths enter as log(a) and the RATIOS c/a, b/a. The ratios are
    the part that matters -- they are scale-free shape descriptors, and
    the Anatase/Rutile distinction IS a c/a difference (2.51 vs 0.64).
  * every block carries a missing-indicator, because ~5% of species are
    absent from mindat and a zero would otherwise read as a real value.
  * space group enters one-hot over crystal system plus the number
    scaled to [0,1]; the raw number is ordinal-ish but not linear, so it
    is a weak feature on its own and present mainly for tie-breaking.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys

import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from composition_mlp import ELEMENTS, comp_vector  # noqa: E402
from train_spectra_head import (  # noqa: E402
    DATA,
    N_BINS,
    SpectrumHead,
    build_split,
    spectral_loss,
    TRIVIAL_COS,
    NOISE_CEIL,
)

SYSTEMS = (
    "Triclinic",
    "Monoclinic",
    "Orthorhombic",
    "Tetragonal",
    "Trigonal",
    "Hexagonal",
    "Isometric",
    "Amorphous",
)
CIF_FEATURES = os.path.expanduser("~/corpora/rock-olmo-training/cif_features.json")
STRUNZ_N = 10
MINDAT = os.path.expanduser("~/corpora/mineral-refs/mindat/geomaterials.jsonl")


def load_mindat() -> dict:
    out = {}
    with open(MINDAT) as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            n = (r.get("name") or "").strip()
            if n:
                out[n.lower()] = r
    return out


def load_cifs() -> dict:
    if not os.path.exists(CIF_FEATURES):
        return {}
    return {k.lower(): v for k, v in json.load(open(CIF_FEATURES)).items()}


def cif_vector(cf: dict | None) -> list[float]:
    """Geometry that actually sets a Raman frequency.

    mindat's metadata gave +0.071 overall but MINUS 0.016 on polymorphs:
    crystal system and cell lengths describe a structure without
    describing its bonds. These features are the bonds. Frequency goes
    roughly as sqrt(k/mu), the force constant k rises steeply as a bond
    shortens, and coordination sets how that force is shared -- so the
    shortest bond, the coordination number, and the spread of bond
    lengths within a polyhedron are the quantities with a mechanism
    behind them rather than a correlation.

    Layout: [log shortest bond, mean CN, max CN, log volume/atom,
             log density, log n_sites, mean bond spread, present]
    """
    if not cf:
        return [0.0] * 8
    sb = cf.get("shortest_bond_a") or 0.0
    coord = list((cf.get("coordination") or {}).values()) or [0.0]
    bonds = (cf.get("bonds") or {}).values()
    spreads = [b.get("spread", 0.0) for b in bonds] or [0.0]
    return [
        math.log(sb) if sb > 0 else 0.0,
        sum(coord) / len(coord),
        max(coord),
        math.log(cf["volume_per_atom"]) if cf.get("volume_per_atom") else 0.0,
        math.log(cf["density"]) if cf.get("density") else 0.0,
        math.log(cf["n_sites"]) if cf.get("n_sites") else 0.0,
        sum(spreads) / len(spreads),
        1.0,
    ]


def _f(v):
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def struct_vector(rec: dict | None) -> list[float]:
    sysv = [0.0] * len(SYSTEMS)
    strz = [0.0] * STRUNZ_N
    cell = [0.0] * 5  # log a, c/a, b/a, log dcalc, hardness/10
    sg = [0.0, 0.0]  # sg/230, present flag
    present = [0.0]
    if rec is None:
        return sysv + strz + cell + sg + present
    present = [1.0]
    cs = rec.get("csystem")
    if cs in SYSTEMS:
        sysv[SYSTEMS.index(cs)] = 1.0
    try:
        s1 = int(rec.get("strunz10ed1"))
        if 1 <= s1 <= STRUNZ_N:
            strz[s1 - 1] = 1.0
    except (TypeError, ValueError):
        pass
    a, b, c = _f(rec.get("a")), _f(rec.get("b")), _f(rec.get("c"))
    if a:
        cell[0] = math.log(a)
        # b and c default to a for the high-symmetry systems where mindat
        # simply omits them (cubic a=b=c, tetragonal/hexagonal a=b). Reading
        # the omission as zero would invent a degenerate cell.
        cell[1] = (c / a) if c else 1.0
        cell[2] = (b / a) if b else 1.0
    d = _f(rec.get("dcalc"))
    if d:
        cell[3] = math.log(d)
    h = _f(rec.get("hmax"))
    if h:
        cell[4] = h / 10.0
    try:
        n = int(rec.get("spacegroup") or 0)
        if 1 <= n <= 230:
            sg = [n / 230.0, 1.0]
    except (TypeError, ValueError):
        pass
    return sysv + strz + cell + sg + present


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument(
        "--features",
        default="comp+struct",
        choices=("comp", "struct", "comp+struct", "cif", "comp+struct+cif", "comp+cif"),
    )
    args = ap.parse_args()

    torch.manual_seed(args.seed)
    data = json.load(open(DATA))
    hp = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
    )
    holdout = set(json.load(open(hp))) if os.path.exists(hp) else set()
    tr, va, he = build_split(data, holdout, seed=args.seed)
    mind = load_mindat()

    cifs = load_cifs()
    vecs, n_struct, n_cif = {}, 0, 0
    for n in data:
        rec = mind.get(n.lower())
        cf = cifs.get(n.lower())
        n_struct += rec is not None
        n_cif += cf is not None
        c = comp_vector(data[n]["formula"])
        s = struct_vector(rec)
        g = cif_vector(cf)
        vecs[n] = {
            "comp": c,
            "struct": s,
            "cif": g,
            "comp+struct": c + s,
            "comp+cif": c + g,
            "comp+struct+cif": c + s + g,
        }[args.features]
    dim = len(next(iter(vecs.values())))

    def rows(names):
        return [
            (n, sp) for n in names if data[n]["spectra"] for sp in data[n]["spectra"]
        ]

    train, val, held = rows(tr), rows(va), rows(he)
    head = SpectrumHead(dim, N_BINS).float()
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
    for _ in range(args.epochs):
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
    print(
        f"seed {args.seed:9d} features={args.features:16s} dim={dim:3d} "
        f"struct={100*n_struct/len(data):.0f}% cif={100*n_cif/len(data):.0f}%  "
        f"val {tv:.4f}  holdout {th:.4f}"
    )


if __name__ == "__main__":
    main()
