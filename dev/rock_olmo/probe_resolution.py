#!/usr/bin/env python3
"""Does bands → name survive measurement variation? (PROCEDURE §22h; rock venv)

Stripped records (only the cue fields; corpus_xml.stripped_record), two pairs — bands →
name and bands + lines → name (LIBS lines exact) — under four kinds of band values:

  exact      the canonical strongest four, as §22g trained them
  synthetic  FRESH instrument draws (a probe-only seed namespace, never trained), forced
             per class — lab / portable / handheld — jittered as synth_variance models and
             rounded to the class grid (1 / 5 / 10 cm-1), 4 draws per species and class
  real       a HELD-OUT real RRUFF / ROD re-measurement of the same species
             (real_remeasurements.json: its own strongest four), at native integers and
             rounded to 5 and 10 cm-1
and three renderings of the resolution field: none, correct (the grid actually used),
wrong (1 for coarse draws, 10 for lab draws) — so the probe shows whether the model reads
the field. PSM and SPM. Groups: T (the probe_pairs seeded 60), R (100 seeded trained
species that have a real re-measurement), V (the val species, exact only).

  ./.venv/bin/python probe_resolution.py --models v3g,v3r
  ./.venv/bin/python probe_resolution.py --models v3r,v3d --digits --jitter-lines --tag digits   # §22i
  ./.venv/bin/python probe_resolution.py --models v3d,v3b --digits --jitter-lines --tag budget   # §22j

§22i options: --digits writes band and line values digit by digit (the arm's training
rendering); --jitter-lines gives every non-exact condition FRESH LIBS line draws (probe-only
namespace), so bands + lines cannot be answered from exact lines. Results go to
resolution_<tag>_*.

§22j additions (read every model number against what its input allows):
  ceiling    a zero-parameter peak matcher over the same values (Library): the 1,785
             rendered species keyed by their canonical strongest four bands and four
             lines; a query scores each species by the F1 of its bands matched within
             ±max(5, grid) cm-1, plus the F1 of its lines within ±0.10 nm when the pair
             shows lines; ties split. Every table row carries it.
  masks      one field replaced by a constant (the library's per-slot median, identical for
             every species): bands → name with the bands masked (spectrum-blind), and
             bands + lines → name with either field masked — a shortcut shows up as a
             score that survives its field being masked.
  carry      jittered and real rows split by whether any shown band crossed a hundreds
             boundary relative to the canonical band it came from (499 → 501): in digit
             rendering such a band shares no leading digit with the reference.
  headline   exact vs fresh synthetic vs real side by side per model, at the rendering the
             model was trained with (§22g: no field; later arms: the correct field).

§22k (--variation, tag variation): the representation arm's rendering. Bands are the
strongest K of a spectrum, POSITION-SORTED as <band> elements (digits, jittered lines,
resolution field), drawn from the full spectra (export_spectra.py):
  exact·kK     the canonical strongest K, K = 4, 6, 8
  synthetic    FRESH draws of the §22k variation model (synth_variance.vary_spectrum: band
               loss, extra bands, re-ranking; probe-only seed namespace), forced per
               instrument class, K = 6
  real·kK      the held-out real re-measurement's own strongest K, K = 4, 6, 8; and K = 6
               rounded to grids 5 and 10
The ceiling's library is keyed on each species' canonical strongest K (the same K).
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import json
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corpus_xml as cx  # noqa: E402
import corpus_xml_variation as cv  # noqa: E402
import probe_chains as pc  # noqa: E402
import probe_pairs as pp  # noqa: E402
import synth_variance as sv  # noqa: E402
from corpus_xml_resolution import GRID, jitter_lines, quantize  # noqa: E402
from fim_transform import fim_wrap  # noqa: E402

REAL = os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2r/docs/real_remeasurements.json")
PAIRS = ((("bands",), "name"), (("bands", "lines"), "name"))
DRAWS = 4
FIELD_RENDERINGS = ("none", "correct", "wrong")
MASKS = {"bands>name": ("mask-bands",), "bands+lines>name": ("mask-bands", "mask-lines")}
#: the field rendering each arm trained with (§22g had no field; every later arm writes it)
TRAINED_RENDERING = {"v3g": "none"}
MODEL_ORDER = ("v3g", "v3r", "v3d", "v3b", "v3k")
KS = (4, 6, 8)
SYNTH_K = 6
BAND_TOL, LINE_TOL = 5, 0.10


class Library:
    """The ceiling: a zero-parameter peak matcher over every rendered species."""

    def __init__(self, path: str = pc.RECORDS, spectra: dict | None = None):
        """Keyed on the records' strongest four; with `spectra` (§22k: {species: [(pos, rel)]})
        also on each species' canonical strongest K, K in KS (NaN-padded when shorter)."""
        d = json.load(open(path))
        recs = d["trained"] + d["untouched"]
        self.names = [r["species"] for r in recs]
        self.idx = {s: i for i, s in enumerate(self.names)}
        self.bands = np.array([r["bands"] for r in recs], dtype=float)
        self.lines = np.array([r["libs"] for r in recs], dtype=float)
        self.const_bands = [int(round(x)) for x in np.median(self.bands, axis=0)]
        self.const_lines = [round(float(x), 2) for x in np.median(self.lines, axis=0)]
        self.bands_k: dict[int, np.ndarray] = {}
        self.const_bands_k: dict[int, list[int]] = {}
        for k in KS if spectra else ():
            rows = [[p for p, _ in sv.strongest(spectra[s], k)] for s in self.names]
            self.bands_k[k] = np.array([r + [np.nan] * (k - len(r)) for r in rows], dtype=float)
            cols = [c for c in self.bands_k[k].T if not np.isnan(c).all()]
            self.const_bands_k[k] = sorted({int(round(float(np.nanmedian(c)))) for c in cols})

    @staticmethod
    def _f1(q, lib: np.ndarray, tol: float) -> np.ndarray:
        d = np.abs(np.asarray(q, dtype=float)[None, :, None] - lib[:, None, :]) <= tol  # NaN slots never match
        valid = np.maximum((~np.isnan(lib)).sum(1), 1)
        mq, ml = d.any(2).mean(1), d.any(1).sum(1) / valid
        return np.where(mq + ml > 0, 2 * mq * ml / np.maximum(mq + ml, 1e-9), 0.0)

    def top1(self, species: str, bands=None, lines=None, grid: int = 1, k: int | None = None) -> float:
        """Top-1 credit for `species` (ties split); a query with no field is chance.
        `k` keys the library on the canonical strongest k (§22k); default: the records' four."""
        s = np.zeros(len(self.names))
        if bands is not None:
            s += self._f1(bands, self.bands_k[k] if k else self.bands, max(BAND_TOL, grid))
        if lines is not None:
            s += self._f1(lines, self.lines, LINE_TOL)
        win = np.flatnonzero(s == s.max()).tolist()
        return (self.idx[species] in win) / len(win)


def crosses_100(shown, canon, *, by_index: bool) -> bool:
    """Did any shown band cross a hundreds boundary relative to the band it came from?
    Synthetic draws keep the band order (by_index); a real band is paired with the
    nearest canonical band, and only pairs within 15 cm-1 count as the same band."""
    if by_index:
        pairs = list(zip(shown, canon))
    else:
        pairs = [(b, min(canon, key=lambda c: abs(c - b))) for b in shown]
        pairs = [(b, c) for b, c in pairs if abs(b - c) <= 15]
    return any(int(b) // 100 != int(c) // 100 for b, c in pairs)


def forced_draw(rec: cx.XmlRecord, klass: str, k: int) -> tuple[cx.XmlRecord, int]:
    """A fresh draw from one instrument class, as synth_variance.jitter_raman models it."""
    rng = sv.rng_for("probe-s2r", rec.species, klass, k)
    off = 0.0
    if klass != "lab" and rng.random() < sv.OUTLIER_SHARE:
        off = rng.choice((-1, 1)) * rng.uniform(4.0, 9.0)
    grid = GRID[klass]
    bands = [quantize(b + rng.gauss(0.0, sv.RAMAN_SIGMA[klass]) + off, grid) for b in rec.bands]
    return dataclasses.replace(rec, bands=bands), grid


def forced_variation_draw(species: str, peaks, pool, klass: str, d: int, k: int = SYNTH_K) -> tuple[list[int], int]:
    """(position-sorted grid-rounded bands, grid): a fresh §22k variation draw from one class."""
    rng = sv.rng_for("probe-s2k", species, klass, d)
    lo, hi = sv.RAMAN_CUTOFF[klass]
    b_lo, b_hi = sv.RAMAN_BANDWIDTH[klass]
    off = rng.choice((-1, 1)) * rng.uniform(4.0, 9.0) if klass != "lab" and rng.random() < sv.OUTLIER_SHARE else 0.0
    inst = sv.RamanInstrument(klass, 532, round(rng.uniform(b_lo, b_hi), 1), round(rng.uniform(lo, hi), 0),
                              sv.RAMAN_TOL[klass][0], sv.CALIBRATION[0][0], round(off, 1))
    grid = GRID[klass]
    return sorted({quantize(p, grid) for p, _ in sv.strongest(sv.vary_spectrum(rng, list(peaks), inst, pool), k)}), grid


DIGITS = False
JITTER_LINES = False
SORTED = False


def variants_for(rec: cx.XmlRecord, pair: str, grid: int, wrong: int, lib: Library, k: int | None = None) -> list[tuple[str, int | None, cx.XmlRecord]]:
    """[(rendering, resolution field, record)]: the three field renderings, then the masks
    (each masked record keeps the correct field, as every digit arm trained)."""
    out = [("none", None, rec), ("correct", grid, rec), ("wrong", wrong, rec)]
    const = lib.const_bands_k[k] if k else lib.const_bands
    for m in MASKS[pair]:
        masked = dataclasses.replace(rec, bands=const) if m == "mask-bands" else dataclasses.replace(rec, libs=lib.const_lines)
        out.append((m, grid, masked))
    return out


def ceiling_for(lib: Library, species: str, cues: tuple[str, ...], rendering: str, rec: cx.XmlRecord, grid: int, k: int | None = None) -> float:
    bands = None if rendering == "mask-bands" else rec.bands
    lines = rec.libs if "lines" in cues and rendering != "mask-lines" else None
    return lib.top1(species, bands, lines, grid, k)


def _add(jobs, lib, species, group, condition, rec, grid, wrong, *, k=0, key_k=None, carry=None, extra=None):
    """Every pair x rendering x FIM order for one shown record."""
    g = pc.load_gold(species)
    if JITTER_LINES and not condition.startswith("exact"):
        rec = jitter_lines(rec, "probe", condition, k)
    for cues, target in PAIRS:
        pair = "+".join(cues) + ">" + target
        for rendering, res, r in variants_for(rec, pair, grid, wrong, lib, key_k):
            text = cx.stripped_record(r, set(cues), target, raman_resolution=res, digits=DIGITS, sorted_bands=SORTED)
            pre, suf = text.split(cx.BLANK)
            ceiling = ceiling_for(lib, species, cues, rendering, r, grid, key_k)
            for order in cx.ORDERS:
                jobs.append({"species": species, "group": group, "condition": condition, "pair": pair, "rendering": rendering,
                             "ceiling": ceiling, "carry": carry, "prompt": fim_wrap(pre, "", suf, order), "gold_rec": g, **(extra or {})})


def build_jobs_variation(groups: dict, real: dict, real_full: dict, canon: dict, pool: list, lib: Library) -> list[dict]:
    """§22k conditions (see the module docstring)."""
    jobs: list[dict] = []
    for grp in ("T", "V"):
        for sp in groups[grp]:
            rec = pp._rec(pc.load_gold(sp))
            allc = [p for p, _ in canon[sp]]
            for k in KS if grp == "T" else (SYNTH_K,):
                shown = sorted(int(round(p)) for p, _ in sv.strongest(canon[sp], k))
                _add(jobs, lib, sp, grp, f"exact·k{k}", dataclasses.replace(rec, bands=shown), 1, 10, key_k=k, carry=False)
            if grp == "T":
                for klass in ("lab", "portable", "handheld"):
                    for d in range(DRAWS):
                        shown, grid = forced_variation_draw(sp, canon[sp], pool, klass, d)
                        _add(jobs, lib, sp, grp, f"synthetic·{klass}", dataclasses.replace(rec, bands=shown), grid,
                             10 if klass == "lab" else 1, k=d, key_k=SYNTH_K, carry=crosses_100(shown, allc, by_index=False))
    for sp in groups["R"]:
        m = real[sp]
        rec = pp._rec(pc.load_gold(sp))
        allc = [p for p, _ in canon[sp]]
        peaks = [tuple(x) for x in real_full[sp]["peaks"]]
        extra = {"set_match": m["set_matches_canonical_5"], "top_match": m["top_matches_canonical_5"]}
        for k in KS:
            shown = sorted({int(round(p)) for p, _ in sv.strongest(peaks, k)})
            _add(jobs, lib, sp, "R", f"real·k{k}", dataclasses.replace(rec, bands=shown), 1, 10, key_k=k,
                 carry=crosses_100(shown, allc, by_index=False), extra=extra)
        for grid in (5, 10):
            shown = sorted({quantize(p, grid) for p, _ in sv.strongest(peaks, SYNTH_K)})
            _add(jobs, lib, sp, "R", f"real·k{SYNTH_K}·grid{grid}", dataclasses.replace(rec, bands=shown), grid, 1,
                 key_k=SYNTH_K, carry=crosses_100(shown, allc, by_index=False), extra=extra)
    return jobs


def build_jobs(groups: dict, real: dict, lib: Library) -> list[dict]:
    jobs: list[dict] = []

    def add(species, group, condition, rec, grid, wrong, *, k=0, carry=None, extra=None):
        _add(jobs, lib, species, group, condition, rec, grid, wrong, k=k, carry=carry, extra=extra)

    for grp in ("T", "V"):
        for sp in groups[grp]:
            rec = pp._rec(pc.load_gold(sp))
            add(sp, grp, "exact", rec, 1, 10, carry=False)
            if grp == "T":
                for klass in ("lab", "portable", "handheld"):
                    for k in range(DRAWS):
                        jrec, grid = forced_draw(rec, klass, k)
                        add(sp, grp, f"synthetic·{klass}", jrec, grid, 10 if klass == "lab" else 1, k=k,
                            carry=crosses_100(jrec.bands, rec.bands, by_index=True))
    for sp in groups["R"]:
        m = real[sp]
        canon = pp._rec(pc.load_gold(sp))
        base = dataclasses.replace(canon, bands=list(m["bands"]))
        extra = {"set_match": m["set_matches_canonical_5"], "top_match": m["top_matches_canonical_5"]}
        add(sp, "R", "real·native", base, 1, 10, carry=crosses_100(base.bands, canon.bands, by_index=False), extra=extra)
        for grid in (5, 10):
            q = dataclasses.replace(base, bands=[quantize(b, grid) for b in base.bands])
            add(sp, "R", f"real·grid{grid}", q, grid, 1, carry=crosses_100(q.bands, canon.bands, by_index=False), extra=extra)
    return jobs


def keys_for(row: dict) -> list[tuple[str, str]]:
    """Every (group, condition) cell a probe row counts toward."""
    g, c = row["group"], row["condition"]
    cells = [(g, c)]
    synth = c.startswith("synthetic")
    if synth:
        cells.append((g, "synthetic·all"))
    if g == "R":
        sub = "set-match" if row.get("set_match") else ("top-match" if row.get("top_match") else "neither")
        cells.append((f"R:{sub}", c))
    if row.get("carry") is not None and not c.startswith("exact"):
        split = f"{g}:{'crosses-100' if row['carry'] else 'same-100'}"
        cells.append((split, c))
        if synth:
            cells.append((split, "synthetic·all"))
    return cells


def summarise(results: list[dict], jobs: list[dict]) -> dict:
    tab = collections.defaultdict(list)
    for r in results:
        for g, c in keys_for(r):
            tab[(r["model"], g, c, r["pair"], r["rendering"])].append(r["status"] == "HIT")
    for j in jobs:
        for g, c in keys_for(j):
            tab[("ceiling", g, c, j["pair"], j["rendering"])].append(j["ceiling"])
    return {"|".join(k): (round(sum(v) / len(v), 3), len(v)) for k, v in tab.items()}


#: report layout per mode: headline columns, shortcut conditions, boundary rows, detail rows
LAYOUT = {
    "legacy": {
        "headline": [("T", "exact"), ("T", "synthetic·all"), ("T", "synthetic·lab"), ("T", "synthetic·portable"), ("T", "synthetic·handheld"),
                     ("R", "real·native"), ("R", "real·grid5"), ("R", "real·grid10"), ("V", "exact")],
        "shortcut": [("T", "exact"), ("T", "synthetic·all"), ("R", "real·native")],
        "boundary": [("T", "synthetic·all"), ("T", "synthetic·lab"), ("T", "synthetic·portable"), ("T", "synthetic·handheld"),
                     ("R", "real·native"), ("R", "real·grid5"), ("R", "real·grid10")],
        "detail": [("T", "exact"), ("T", "synthetic·lab"), ("T", "synthetic·portable"), ("T", "synthetic·handheld"),
                   ("R", "real·native"), ("R", "real·grid5"), ("R", "real·grid10"),
                   ("R:set-match", "real·native"), ("R:set-match", "real·grid5"), ("R:set-match", "real·grid10"),
                   ("R:top-match", "real·grid10"), ("R:neither", "real·grid10"), ("V", "exact")],
    },
    "variation": {
        "headline": [("T", "exact·k4"), ("T", "exact·k6"), ("T", "exact·k8"), ("T", "synthetic·all"), ("T", "synthetic·lab"),
                     ("T", "synthetic·portable"), ("T", "synthetic·handheld"), ("R", "real·k4"), ("R", "real·k6"), ("R", "real·k8"),
                     ("R", "real·k6·grid5"), ("R", "real·k6·grid10"), ("V", "exact·k6")],
        "shortcut": [("T", "exact·k6"), ("T", "synthetic·all"), ("R", "real·k6")],
        "boundary": [("T", "synthetic·all"), ("T", "synthetic·lab"), ("T", "synthetic·portable"), ("T", "synthetic·handheld"),
                     ("R", "real·k6"), ("R", "real·k6·grid5"), ("R", "real·k6·grid10")],
        "detail": [("T", "exact·k4"), ("T", "exact·k6"), ("T", "exact·k8"), ("T", "synthetic·lab"), ("T", "synthetic·portable"),
                   ("T", "synthetic·handheld"), ("R", "real·k4"), ("R", "real·k6"), ("R", "real·k8"), ("R", "real·k6·grid5"),
                   ("R", "real·k6·grid10"), ("R:set-match", "real·k4"), ("R:set-match", "real·k6"), ("R:top-match", "real·k6"),
                   ("R:neither", "real·k6"), ("V", "exact·k6")],
    },
}  # fmt: skip


def write_report(summary: dict, models: list[str], groups: dict, path: str) -> None:
    def val(m, g, c, p, r):
        return summary.get(f"{m}|{g}|{c}|{p}|{r}")

    def cell(m, g, c, p, r):
        v = val(m, g, c, p, r)
        return f"{v[0]:.2f}" if v else "—"

    def tr(m):
        return TRAINED_RENDERING.get(m, "correct")

    rows_of = lambda: [("ceiling", "correct"), *((m, tr(m)) for m in models)]  # noqa: E731
    L = ["# Resolution probe: does bands → name survive measurement variation?\n",
         f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} by `dev/rock_olmo/probe_resolution.py`. "
         "HIT rate, PSM and SPM pooled. Rendering = the resolution field: none / correct (the grid actually used) / wrong. "
         f"Rendering flags: digits={DIGITS}, jittered lines={JITTER_LINES}, position-sorted <band> lists={SORTED}.\n",
         "**ceiling** = a zero-parameter peak matcher on the same values: the 1,785 rendered species keyed by their "
         f"canonical strongest four bands (+ four lines); F1 of bands matched within ±max({BAND_TOL}, grid) cm-1, plus F1 of "
         f"lines within ±{LINE_TOL} nm when the pair shows lines; ties split. Model cells in the first three tables use the "
         "rendering each model was trained with (v3g: no field; later arms: the correct field).\n"]
    for p in ("bands>name", "bands+lines>name"):
        L.append(f"\n## {p.replace('>', ' → ')}\n")
        L.append("### Headline: exact vs jittered vs real\n")
        lay = LAYOUT["variation" if SORTED else "legacy"]
        cols = lay["headline"]
        L.append("| | " + " | ".join(f"{g} {c}" for g, c in cols) + " |")
        L.append("|---|" + "---|" * len(cols))
        for m, r in rows_of():
            L.append(f"| {m} | " + " | ".join(cell(m, g, c, p, r) for g, c in cols) + " |")
        L.append("\n### Shortcut check: one field masked (a constant, identical for every species)\n")
        masks = ("correct", *MASKS[p])
        conds = lay["shortcut"]
        L.append("| | " + " | ".join(f"{g} {c} · {'unmasked' if mk == 'correct' else mk}" for g, c in conds for mk in masks) + " |")
        L.append("|---|" + "---|" * (len(conds) * len(masks)))
        for m, _ in rows_of():
            L.append(f"| {m} | " + " | ".join(cell(m, g, c, p, mk) for g, c in conds for mk in masks) + " |")
        L.append("\n### Hundreds-boundary split: did a shown band cross a hundreds boundary (499 → 501)?\n")
        conds = lay["boundary"]
        L.append("| condition | rows crossing | " + " | ".join(f"{m} same | {m} crosses" for m, _ in rows_of()) + " |")
        L.append("|---|---|" + "---|---|" * (len(models) + 1))
        for g, c in conds:
            same, cross = val("ceiling", f"{g}:same-100", c, p, "correct"), val("ceiling", f"{g}:crosses-100", c, p, "correct")
            ns, nc = (same[1] if same else 0), (cross[1] if cross else 0)
            share = f"{nc / (ns + nc):.0%} of {ns + nc}" if ns + nc else "—"
            L.append(f"| {g} {c} | {share} | " + " | ".join(
                f"{cell(m, f'{g}:same-100', c, p, r)} | {cell(m, f'{g}:crosses-100', c, p, r)}" for m, r in rows_of()) + " |")
        L.append("\n### Every condition by field rendering\n")
        L.append("| group | condition | ceiling | " + " | ".join(f"{m} none | {m} correct | {m} wrong" for m in models) + " |")
        L.append("|---|---|---|" + "---|---|---|" * len(models))
        for g, c in lay["detail"]:
            L.append(f"| {g} | {c} | {cell('ceiling', g, c, p, 'correct')} | "
                     + " | ".join(f"{cell(m, g, c, p, 'none')} | {cell(m, g, c, p, 'correct')} | {cell(m, g, c, p, 'wrong')}" for m in models) + " |")
    n = {k: len(groups[k]) for k in groups}
    L.append(f"\nGroups: T {n['T']}, R {n['R']} (real re-measurements), V {n['V']}.")
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")
    print(f"-> {path}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="v3g")
    ap.add_argument("--seed", type=int, default=20260922)
    ap.add_argument("--n-real", type=int, default=100)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/v3/pair_probe"))
    ap.add_argument("--digits", action="store_true")
    ap.add_argument("--jitter-lines", action="store_true")
    ap.add_argument("--variation", action="store_true", help="§22k: position-sorted strongest-K lists (implies --digits --jitter-lines)")
    ap.add_argument("--tag", default="", help="suffix for the result files (resolution_<tag>_...)")
    args = ap.parse_args()
    global DIGITS, JITTER_LINES, SORTED
    DIGITS, JITTER_LINES, SORTED = args.digits or args.variation, args.jitter_lines or args.variation, args.variation
    pre = f"resolution_{args.tag}_" if args.tag else "resolution_"
    groups = pp.sample_species({"T": 60, "V": 19, "U": 0}, args.seed)
    real = json.load(open(REAL))
    val = set(groups["V"])
    trained_names = {r["species"] for r in json.load(open(pc.RECORDS))["trained"]}
    cands = sorted(s for s in real if s in trained_names and s not in val)
    random.Random(args.seed).shuffle(cands)
    groups["R"] = cands[: args.n_real]
    if args.variation:
        canon, real_full, pool = cv.load_spectra()
        lib = Library(spectra=canon)
        jobs = build_jobs_variation(groups, real, real_full, canon, pool, lib)
    else:
        lib = Library()
        jobs = build_jobs(groups, real, lib)
    os.makedirs(args.out, exist_ok=True)
    json.dump({k: v for k, v in groups.items()}, open(os.path.join(args.out, pre + "items_frozen.json"), "w"), indent=1)
    print(f"groups T {len(groups['T'])} R {len(groups['R'])} V {len(groups['V'])} -> {len(jobs):,} prompts per model", flush=True)
    rpath = os.path.join(args.out, pre + "results.jsonl")
    by_model = collections.defaultdict(list)
    if os.path.exists(rpath):
        for line in open(rpath):
            r = json.loads(line)
            by_model[r["model"]].append(r)
    stale = [m for m, rs in by_model.items() if len(rs) != len(jobs)]
    if stale:  # an earlier version of this probe (different job set): re-run those models
        print(f"re-running {stale}: cached rows do not match the current job set", flush=True)
        for m in stale:
            del by_model[m]
        with open(rpath, "w", encoding="utf-8") as fh:
            for rs in by_model.values():
                for r in rs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    for m in args.models.split(","):
        if m in by_model:
            print(f"{m}: cached", flush=True)
            continue
        t0 = time.time()
        gens = pc.generate(pp.ALL_MODELS[m] if m in pp.ALL_MODELS else os.path.expanduser(m), [j["prompt"] for j in jobs], "cuda:0", 24)
        with open(rpath, "a", encoding="utf-8") as fh:
            for j, gt in zip(jobs, gens):
                ans = pc.extract(gt, "xml", "name")
                row = {k: v for k, v in j.items() if k not in ("gold_rec", "prompt")}
                row.update(model=m, answer=ans[:60], status=pc.score("name", ans, j["gold_rec"]))
                by_model[m].append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"{m}: {len(jobs):,} prompts in {time.time() - t0:.0f}s", flush=True)
    results = [r for rs in by_model.values() for r in rs]
    summary = summarise(results, jobs)
    json.dump(summary, open(os.path.join(args.out, pre + "summary.json"), "w"), indent=1)
    models = [m for m in MODEL_ORDER if m in by_model] + sorted(m for m in by_model if m not in MODEL_ORDER)
    write_report(summary, models, groups, os.path.join(args.out, pre + "report.md"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
