"""Species -> binned Raman spectrum, for regression-head training.

WHY A BINNED VECTOR AND NOT A PEAK LIST. The token-generation run failed
because a band list is a variable-length SET and the model had to sample
it digit by digit out of a 100k vocabulary. A fixed-length binned
spectrum removes both problems: it is permutation-invariant (a set has
no order to get wrong), it is the same shape for every mineral, and it
is regressed with L1 rather than sampled. This is the AtomGPT property
head applied to a spectrum instead of a scalar.

WHY THE WHOLE SPECTRUM AND NOT JUST PEAKS. We have the RRUFF spectra
themselves, so binning the measured curve keeps information a peak-pick
throws away — relative intensities, band widths, shoulders the picker
was deliberately too conservative to call. It also removes the
peak-picker from the training path entirely, so a bad pick can no longer
teach a wrong fact.
"""

from __future__ import annotations

import collections
import json
import os
import re

from reference_layer import iter_rruff

#: The fingerprint region. Below 100 cm-1 a Raman spectrum shows the
#: notch filter rather than the sample; above 1200 most minerals have
#: only OH/H2O stretches, which are informative but sparse and would
#: dominate the vector with empty bins.
GRID_MIN, GRID_MAX, N_BINS = 100.0, 1200.0, 128
BIN_WIDTH = (GRID_MAX - GRID_MIN) / N_BINS


def bin_spectrum(samples: list[tuple[float, float]]) -> list[float] | None:
    """Resample onto the fixed grid, max-pooled per bin, peak-normalised.

    MAX pooling, not mean: a narrow band that falls inside a wide bin
    would be averaged away against its own baseline, and the band is the
    signal. Normalising to the spectrum's own maximum makes shapes
    comparable across instruments and acquisition times, which is what
    we want the model to learn — the pattern, not the absolute counts.
    """
    if len(samples) < 32:
        return None
    bins = [0.0] * N_BINS
    seen = [False] * N_BINS
    lo = min(y for _, y in samples)
    for x, y in samples:
        if x < GRID_MIN or x >= GRID_MAX:
            continue
        i = int((x - GRID_MIN) / BIN_WIDTH)
        v = y - lo
        if v > bins[i]:
            bins[i] = v
        seen[i] = True
    if sum(seen) < N_BINS * 0.6:
        return None
    peak = max(bins)
    if peak <= 0:
        return None
    return [round(b / peak, 5) for b in bins]


def build(
    archives: tuple[str, ...] = ("excellent_unoriented.zip", "excellent_oriented.zip")
) -> dict:
    """species -> {formula, spectra: [binned, ...]}"""
    out: dict[str, dict] = {}
    for arch in archives:
        for rec in iter_rruff(arch):
            species = (rec.get("species") or "").strip()
            if not species:
                continue
            binned = bin_spectrum(rec["spectrum"])
            if binned is None:
                continue
            slot = out.setdefault(
                species,
                {"formula": rec.get("ideal_formula", ""), "spectra": [], "ids": []},
            )
            slot["spectra"].append(binned)
            slot["ids"].append(rec.get("rruff_id", ""))
            if not slot["formula"]:
                slot["formula"] = rec.get("ideal_formula", "")
    return out


if __name__ == "__main__":
    data = build()
    n_spec = sum(len(v["spectra"]) for v in data.values())
    print(f"species with usable spectra: {len(data)}")
    print(f"total spectra: {n_spec}")
    counts = collections.Counter(len(v["spectra"]) for v in data.values())
    print(
        f"spectra per species: {dict(sorted(counts.items())[:6])} ... max {max(counts)}"
    )
    path = os.path.expanduser("~/corpora/rock-olmo-training/spectra_binned.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, "w"))
    print(f"wrote {path}")
