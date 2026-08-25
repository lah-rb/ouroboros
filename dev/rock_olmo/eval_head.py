"""Score a trained head per species, and split the holdout by POLYMORPH.

WHY THIS SPLIT. The aggregate holdout number says the head beats
composition retrieval by ~0.068 cosine. That is an average over two very
different populations, and the average hides the mechanism.

A polymorph is a mineral with the same formula as another but a
different crystal structure -- Anatase/Rutile/Brookite (TiO2),
Marcasite/Pyrite (FeS2), Calcite/Aragonite (CaCO3). Their Raman spectra
differ completely, because Raman reads BONDING GEOMETRY, not
stoichiometry. Composition retrieval must fail on them by construction:
its input cannot distinguish them. It scored 0.207 on Anatase, which is
below the 0.2316 floor for two randomly chosen unrelated minerals.

So if the head's advantage is real spectroscopic knowledge rather than a
better lookup, it should be CONCENTRATED on exactly these species, where
the mineral NAME carries structural information the formula cannot. If
instead the head is only a smoother interpolator, the advantage should
be spread evenly and vanish here. That is the measurement.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "1")

import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from transformers import AutoModel, AutoTokenizer  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from retrieval_baseline import (
    composition,
    cos_dict,
    cos_vec,
    mean_spectrum,
)  # noqa: E402
from train_spectra_head import (  # noqa: E402
    BASE,
    DATA,
    SpectrumHead,
    build_split,
    prompt_for,
    TRIVIAL_COS,
    NOISE_CEIL,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True, help="dir holding head.pt")
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--prompt-mode", default="both")
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    data = json.load(open(DATA))
    holdout = set(
        json.load(
            open(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
                )
            )
        )
    )
    tr, va, he = build_split(data, holdout, seed=args.seed)

    comps = {n: composition(data[n]["formula"]) for n in data}
    means = {n: mean_spectrum(data[n]["spectra"]) for n in data if data[n]["spectra"]}
    train = [n for n in tr if n in means]

    dev = args.device
    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    bb = (
        AutoModel.from_pretrained(
            BASE, dtype=torch.float32 if dev == "cpu" else torch.bfloat16
        )
        .to(dev)
        .eval()
    )
    head = SpectrumHead(bb.config.hidden_size).to(dev).float()
    head.load_state_dict(
        torch.load(os.path.join(args.ckpt, "head.pt"), map_location=dev)
    )
    head.eval()

    def predict(name: str) -> list[float]:
        enc = tok(
            [prompt_for(name, data[name]["formula"], args.prompt_mode)],
            return_tensors="pt",
        ).to(dev)
        with torch.no_grad():
            o = bb(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).to(o.dtype)
            return head(((o * m).sum(1) / m.sum(1)).float())[0].tolist()

    poly, rest = [], []
    detail = []
    for n in he:
        if n not in means:
            continue
        ranked = sorted(train, key=lambda t: -cos_dict(comps[n], comps[t]))[:5]
        r_pred = mean_spectrum([means[t] for t in ranked])
        h_pred = predict(n)
        spectra = data[n]["spectra"]
        r = sum(cos_vec(r_pred, s) for s in spectra) / len(spectra)
        h = sum(cos_vec(h_pred, s) for s in spectra) / len(spectra)
        is_poly = cos_dict(comps[n], comps[ranked[0]]) > 0.999
        (poly if is_poly else rest).append((h, r))
        if is_poly:
            detail.append((r, h, n, ranked[0]))

    def report(lab, rows):
        if not rows:
            return
        h = sum(x[0] for x in rows) / len(rows)
        r = sum(x[1] for x in rows) / len(rows)
        print(
            f"  {lab:34s} n={len(rows):3d}   head {h:.4f}   retrieval {r:.4f}   "
            f"delta {h - r:+.4f}"
        )

    print(f"checkpoint {args.ckpt}  prompt_mode={args.prompt_mode}  seed={args.seed}")
    print(f"  (trivial {TRIVIAL_COS:.4f}, ceiling {NOISE_CEIL:.4f})\n")
    report("POLYMORPH (same-formula twin)", poly)
    report("rest of holdout", rest)
    report("all holdout", poly + rest)
    print("\n  per-polymorph, sorted by how badly retrieval did:")
    for r, h, n, t in sorted(detail):
        print(f"    {n:22s} vs {t:16s} retrieval {r:.3f} -> head {h:.3f}  {h - r:+.3f}")


if __name__ == "__main__":
    main()
