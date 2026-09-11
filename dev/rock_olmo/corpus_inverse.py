#!/usr/bin/env python3
"""Inverse-identification experiment corpus: the seeker's output IS the prompt.

Runs under the ROOT venv (imports reference_layer; no torch). Writes JSONL that
`package_inverse.py` (rock venv) packs.

WHY (PROCEDURE.md §19, 2026-09-10). The v4 inverse examples were built from
`bands[:4]` of a position-sorted list — the four LOWEST bands — a key that is
ambiguous for 56 % of species at ±10 cm⁻¹, rendered once per species, with the
peak intensities dropped. The model learned the answer format and a prior. The
operator's reframing: the eventual use is "feed the model a peak-seeker's output
over an unknown spectrum from an instrument with varied priors, get a
prediction", so the training prompt must be exactly that — one fixed schema
produced by an algorithm over spectra with known identities, with the
variation coming from the DATA (every spectrum, every archive tier, oriented
and unoriented, plus perturbed re-runs of the seeker), not from wording.

WHAT THIS RENDERS
- Source spectra: RRUFF archives (excellent/fair/poor/unrated, oriented and
  unoriented, LR-Raman) via reference_layer.iter_rruff; ROD via iter_rod.
- Seeker: reference_layer.pick_peaks (the project's own peak picker), kept to
  the 12 strongest peaks, position-sorted, relative intensities attached.
- Splits: one excellent_unoriented spectrum per species with ≥ 2 such spectra
  is HELD OUT (val_heldout, in-distribution instrument); ALL of ROD is held out
  (val_rod, an unseen instrument family). Everything else trains.
- Augmentation (train only): K perturbed copies of each spectrum — calibration
  shift, smooth gain envelope (orientation-like), fluorescence baseline, noise,
  scan-window truncation, resolution smoothing, seeker-threshold jitter — each
  re-picked by the seeker, so every prompt is still "the algorithm's output".
- Two directions from the same peak list, 1:1 by construction:
    identify:  <schema peak list> → species (formula)[; also consistent with …]
    predict:   species (formula), laser → <schema peak list>
  The "also consistent with" tail is deterministic: other species whose
  canonical four strongest bands all lie within ±10 cm⁻¹ (the confusable 11 %).
- Also writes knn_library.json (train peak lists per species) for the kNN
  control in probe_inverse.py, and probe_items.json (held-out + ROD items).

    ../../.venv/bin/python corpus_inverse.py --out ~/corpora/rock-olmo-training/v4/inverse_exp --aug 4 [--dry-run]
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference_layer as rl  # noqa: E402

ARCHIVES = [
    "excellent_unoriented.zip",
    "excellent_oriented.zip",
    "fair_unoriented.zip",
    "fair_oriented.zip",
    "poor_unoriented.zip",
    "unrated_unoriented.zip",
    "unrated_oriented.zip",
    "LR-Raman.zip",
]
MAX_PEAKS = 12
TOL = 10.0  # cm-1, the field's comparison tolerance


# ── schema (shared with inference; keep byte-stable) ─────────────────────────
def fmt_peaks(peaks: list[dict]) -> str:
    return ", ".join(f"{p['position_cm-1']:.1f} ({p['relative_intensity']:.2f})" for p in peaks)


def identify_prompt(peaks: list[dict], laser: str) -> str:
    exc = f"{laser} nm excitation" if laser else "excitation unknown"
    return (
        f"Raman peak list ({exc}; {len(peaks)} peaks, position cm-1 with relative "
        f"intensity, strongest = 1.00):\n{fmt_peaks(peaks)}\nPhase:"
    )


def identify_completion(species: str, formula: str, also: list[tuple[str, str]]) -> str:
    s = f" {species} ({formula})." if formula else f" {species}."
    if also:
        s = s[:-1] + "; also consistent with: " + ", ".join(
            f"{n} ({f})" if f else n for n, f in also
        ) + "."
    return s


def predict_prompt(species: str, formula: str, laser: str) -> str:
    exc = f"at {laser} nm excitation" if laser else "(excitation unspecified)"
    who = f"{species} ({formula})" if formula else species
    return (
        f"Predicted Raman peak list for {who} {exc} (position cm-1 with relative "
        f"intensity, strongest = 1.00):"
    )


def predict_completion(peaks: list[dict]) -> str:
    return " " + fmt_peaks(peaks) + "."


# ── seeker over (possibly perturbed) spectra ─────────────────────────────────
def pick(spectrum, prom=0.05, sep=8.0) -> list[dict]:
    peaks = rl.pick_peaks(spectrum, min_prominence_frac=prom, min_separation=sep)
    peaks = sorted(peaks, key=lambda p: -p["relative_intensity"])[:MAX_PEAKS]
    top = max((p["relative_intensity"] for p in peaks), default=0.0) or 1.0
    # renormalise so the strongest LISTED peak is 1.00 — the schema says so, and the
    # picker's figure is relative to the whole spectrum's range (baseline included)
    peaks = [{**p, "relative_intensity": round(p["relative_intensity"] / top, 3)} for p in peaks]
    return sorted(peaks, key=lambda p: p["position_cm-1"])


def perturb(spectrum, rng: random.Random):
    """A perturbed spectrum + the seeker parameters to use on it."""
    xs = [x for x, _ in spectrum]
    ys = [y for _, y in spectrum]
    ymax = max(ys) or 1.0
    # calibration shift
    if rng.random() < 0.7:
        d = rng.uniform(-3.0, 3.0)
        xs = [x + d for x in xs]
    # orientation-like smooth gain envelope
    if rng.random() < 0.6:
        a, L, x0 = rng.uniform(0.1, 0.5), rng.uniform(300, 1200), rng.uniform(0, 1200)
        ys = [y * (1 + a * math.sin(2 * math.pi * (x - x0) / L)) for x, y in zip(xs, ys)]
    # fluorescence baseline (sloped exponential or quadratic)
    if rng.random() < 0.6:
        B = rng.uniform(0.05, 0.8) * ymax
        if rng.random() < 0.5:
            tau, xc = rng.uniform(300, 2000), rng.uniform(-200, 400)
            ys = [y + B * math.exp(-(x - xc) / tau) for x, y in zip(xs, ys)]
        else:
            x1 = xs[-1] or 1.0
            c = rng.uniform(-1, 1)
            ys = [y + B * (0.5 + c * (x / x1) + 0.5 * (x / x1) ** 2) for x, y in zip(xs, ys)]
    # resolution smoothing (moving average in points ≈ Gaussian-ish)
    if rng.random() < 0.4 and len(ys) > 20:
        w = rng.randint(2, 9)
        acc, out = 0.0, []
        buf = collections.deque()
        for y in ys:
            buf.append(y)
            acc += y
            if len(buf) > w:
                acc -= buf.popleft()
            out.append(acc / len(buf))
        ys = out
    # noise
    if rng.random() < 0.8:
        s = rng.uniform(0.005, 0.04) * ymax
        ys = [y + rng.gauss(0, s) for y in ys]
    # scan-window truncation
    if rng.random() < 0.5:
        lo, hi = rng.uniform(60, 220), rng.uniform(850, 1300)
        pts = [(x, y) for x, y in zip(xs, ys) if lo <= x <= hi]
        if len(pts) > 50:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    prom = rng.uniform(0.03, 0.08)
    sep = rng.uniform(6.0, 10.0)
    return list(zip(xs, ys)), prom, sep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--aug", type=int, default=4, help="perturbed copies per train spectrum")
    ap.add_argument("--seed", type=int, default=20260910)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="records per archive (dev)")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    t0 = time.time()

    # 1. load every RRUFF record once, pick base peaks
    recs = []
    for arc in ARCHIVES:
        n = 0
        try:
            for r in rl.iter_rruff(arc, limit=args.limit):
                base = pick(r["spectrum"])
                if len(base) < 3:
                    continue
                recs.append({
                    "species": r["species"], "formula": r.get("ideal_formula") or "",
                    "laser": (r.get("laser_nm") or "").strip(), "id": r.get("rruff_id") or "",
                    "archive": arc.replace(".zip", ""), "spectrum": r["spectrum"], "base": base,
                })
                n += 1
        except Exception as e:
            print(f"  {arc}: {type(e).__name__}: {str(e)[:80]}", flush=True)
        print(f"[{time.time()-t0:4.0f}s] {arc}: {n} spectra with >=3 peaks", flush=True)
    rod = []
    for r in rl.iter_rod():
        spec = r.get("spectrum") or []
        base = pick(spec) if spec else []
        if len(base) >= 3:
            rod.append({"species": r["species"], "formula": r.get("formula") or "",
                        "laser": (r.get("laser_nm") or "").strip(), "id": r.get("rod_id") or "",
                        "archive": "ROD", "base": base, "device": r.get("device") or ""})
    print(f"[{time.time()-t0:4.0f}s] ROD: {len(rod)} spectra with >=3 peaks", flush=True)

    # 2. held-out split: one excellent_unoriented spectrum per species with >= 2 of them
    by_sp = collections.defaultdict(list)
    for i, r in enumerate(recs):
        if r["archive"] == "excellent_unoriented":
            by_sp[r["species"]].append(i)
    heldout = set()
    for sp, idxs in by_sp.items():
        if len(idxs) >= 2:
            heldout.add(rng.choice(idxs))
    train = [r for i, r in enumerate(recs) if i not in heldout]
    val = [recs[i] for i in sorted(heldout)]

    # 3. canonical fingerprints (richest excellent_unoriented TRAIN spectrum) → confusable sets
    canon = {}
    for r in train:
        if r["archive"] != "excellent_unoriented":
            continue
        if r["species"] not in canon or len(r["base"]) > len(canon[r["species"]]["base"]):
            canon[r["species"]] = r
    key = {sp: sorted(p["position_cm-1"] for p in sorted(r["base"], key=lambda p: -p["relative_intensity"])[:4])
           for sp, r in canon.items() if len(r["base"]) >= 4}
    also: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    items = list(key.items())
    for sp, k in items:
        for sp2, k2 in items:
            if sp2 != sp and all(abs(a - b) <= TOL for a, b in zip(k, k2)):
                also[sp].append((sp2, canon[sp2]["formula"]))
        also[sp] = sorted(also[sp])[:2]
    n_conf = sum(1 for sp in key if also[sp])
    print(f"[{time.time()-t0:4.0f}s] train {len(train)} spectra ({len({r['species'] for r in train})} species), "
          f"heldout {len(val)}, ROD {len(rod)}; confusable species {n_conf}/{len(key)}", flush=True)

    # 4. render
    def ex(kind, ident, prompt, completion, r, val_flag):
        return {"fact_id": ident, "kind": kind, "frame": 0, "prompt": prompt, "completion": completion,
                "species": r["species"], "source": r["archive"], "canonical": False, "val": val_flag}
    out_train, n_aug_fail = [], 0
    for r in train:
        variants = [r["base"]]
        for k in range(args.aug):
            spec, prom, sep = perturb(r["spectrum"], rng)
            pk = pick(spec, prom, sep)
            if len(pk) >= 3:
                variants.append(pk)
            else:
                n_aug_fail += 1
        for j, pk in enumerate(variants):
            ident = f"{r['archive']}:{r['id'] or r['species']}:v{j}"
            out_train.append(ex("identify", ident + ":inv", identify_prompt(pk, r["laser"]),
                                identify_completion(r["species"], r["formula"], also.get(r["species"], [])), r, False))
            out_train.append(ex("predict", ident + ":fwd", predict_prompt(r["species"], r["formula"], r["laser"]),
                                predict_completion(pk), r, False))
    out_val = [ex("identify", f"{r['archive']}:{r['id']}:heldout", identify_prompt(r["base"], r["laser"]),
                  identify_completion(r["species"], r["formula"], also.get(r["species"], [])), r, True) for r in val]
    out_rod = [ex("identify", f"ROD:{r['id']}", identify_prompt(r["base"], r["laser"]),
                  identify_completion(r["species"], r["formula"], also.get(r["species"], [])), r, True) for r in rod]
    rod_seen = sum(1 for r in rod if r["species"] in {t["species"] for t in train})
    est_tok = sum(len(e["prompt"]) + len(e["completion"]) for e in out_train) / 3.6
    print(f"[{time.time()-t0:4.0f}s] train examples {len(out_train):,} (aug failures {n_aug_fail}), "
          f"heldout {len(out_val)}, ROD {len(out_rod)} ({rod_seen} with a train species); "
          f"~{est_tok/1e6:.1f}M train tokens", flush=True)
    print("example identify:\n" + out_train[0]["prompt"] + out_train[0]["completion"])
    print("example predict:\n" + out_train[1]["prompt"] + out_train[1]["completion"][:120])
    if args.dry_run:
        return 0
    os.makedirs(os.path.join(args.out, "docs"), exist_ok=True)
    for name, rows in (("inverse_train", out_train), ("inverse_val_heldout", out_val), ("inverse_val_rod", out_rod)):
        with open(os.path.join(args.out, "docs", name + ".jsonl"), "w") as f:
            for e in rows:
                f.write(json.dumps(e) + "\n")
    lib = collections.defaultdict(list)
    for r in train:
        lib[r["species"]].append({"peaks": r["base"], "laser": r["laser"], "archive": r["archive"], "id": r["id"]})
    json.dump(lib, open(os.path.join(args.out, "docs", "knn_library.json"), "w"))
    json.dump({"heldout": [{"species": r["species"], "formula": r["formula"], "laser": r["laser"], "id": r["id"], "peaks": r["base"]} for r in val],
               "rod": [{"species": r["species"], "formula": r["formula"], "laser": r["laser"], "id": r["id"], "peaks": r["base"], "device": r.get("device", "")} for r in rod]},
              open(os.path.join(args.out, "docs", "probe_items.json"), "w"))
    json.dump({"seed": args.seed, "aug": args.aug, "archives": ARCHIVES, "max_peaks": MAX_PEAKS, "tol": TOL,
               "train_spectra": len(train), "train_species": len({r['species'] for r in train}),
               "heldout": len(val), "rod": len(rod), "rod_species_seen_in_train": rod_seen,
               "confusable_species": n_conf, "canonical_species": len(key),
               "train_examples": len(out_train), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
              open(os.path.join(args.out, "docs_manifest.json"), "w"), indent=1)
    print(f"[{time.time()-t0:4.0f}s] wrote {args.out}/docs", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
