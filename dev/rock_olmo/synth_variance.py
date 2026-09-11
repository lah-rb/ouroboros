#!/usr/bin/env python3
"""Instrument-variance sampler for the synthetic corpus (stdlib; both venvs).

WHY. The pilot must teach the model that a peak list from an UNKNOWN
instrument still names the species, so every rendered measurement carries
sampled instrument variance instead of the archive's exact positions. The
distributions are the ones the corpus and the reference archive measured:

  Raman  RRUFF within-species jitter: |Δ| median 1.3 cm-1, p75 ~3, p90 5–6,
         p95 8–9; the beryl seven-spectrometer paper: ±2–4 cm-1 on portable
         units with one outlier; strongest-band identity flips in 29 % of
         same-species pairs when the top two bands are close; five
         excitation lines (488/532/633/785/830 nm); low-wavenumber cutoffs
         and weak-band loss on wide-slit portable and handheld units.
  LIBS   line POSITIONS are fixed physics (±0.02–0.10 nm of calibration);
         intensity RATIOS vary with plasma temperature, matrix and gating;
         resonance lines self-absorb; the spectral window drops lines.

DETERMINISM. Every draw is seeded from (fact_id, template_id, variant), so a
render is reproducible and the manifest's realised quantiles are a property
of the corpus, not of the run.

PRESENTATION RULE. Peak lists are FULL lists with intensities (position
sorted, strongest listed = 1.00), never strongest-N alone: the strongest
band changes identity in 29 % of same-species pairs, so a strongest-N key
is the thing an unknown instrument breaks first (PROCEDURE §20).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass, field

RAMAN_CLASSES = (("lab", 0.50), ("portable", 0.35), ("handheld", 0.15))
RAMAN_SIGMA = {"lab": 1.2, "portable": 3.0, "handheld": 3.5}
RAMAN_CUTOFF = {
    "lab": (50.0, 100.0),
    "portable": (100.0, 200.0),
    "handheld": (150.0, 250.0),
}
RAMAN_TOL = {
    "lab": ("±1 cm-1", "±2 cm-1"),
    "portable": ("±2 cm-1", "±3 cm-1"),
    "handheld": ("±3 cm-1", "±5 cm-1"),
}
RAMAN_BANDWIDTH = {"lab": (2.0, 5.0), "portable": (6.0, 12.0), "handheld": (9.0, 15.0)}
EXCITATION_NM = ((532, 0.45), (785, 0.35), (633, 0.10), (488, 0.05), (830, 0.05))
CALIBRATION = (
    ("the 520.5 cm-1 silicon line", 0.55),
    ("a neon lamp", 0.25),
    ("no daily calibration", 0.20),
)
OUTLIER_SHARE = (
    0.25  # calibrated with RAMAN_SIGMA against the RRUFF quantiles (see test)
)
FLIP_MARGIN = 0.15
FLIP_P = 0.29

LIBS_WINDOWS = (((200.0, 500.0), 0.3), ((350.0, 900.0), 0.4), ((190.0, 1040.0), 0.3))
LIBS_T_RANGE = (8000.0, 12000.0)
LIBS_POS_JITTER = (0.02, 0.10)
LIBS_RESOLUTION = (0.05, 1.0)


def rng_for(*parts: object) -> random.Random:
    """A generator seeded from the parts (fact_id, template_id, variant)."""
    seed = int(
        hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16], 16
    )
    return random.Random(seed)


def _weighted(rng: random.Random, pairs) -> object:
    r = rng.random() * sum(w for _, w in pairs)
    acc = 0.0
    for value, w in pairs:
        acc += w
        if r <= acc:
            return value
    return pairs[-1][0]


# ── Raman ────────────────────────────────────────────────────────────


@dataclass
class RamanInstrument:
    klass: str
    laser_nm: int
    bandwidth_cm1: float
    cutoff_cm1: float
    tol: str
    calibration: str
    offset_cm1: float = 0.0  # a miscalibrated unit's constant shift

    def slots(self) -> dict:
        """Template slots this instrument fills."""
        return {
            "instrument_class": self.klass,
            "laser": f" at {self.laser_nm} nm excitation",
            "laser_nm": str(self.laser_nm),
            "tol": self.tol,
            "calibration": self.calibration,
            "window": f"{int(round(self.cutoff_cm1))}–{4000 if self.klass == 'lab' else 3200} cm-1",
        }


def sample_raman(rng: random.Random) -> RamanInstrument:
    klass = str(_weighted(rng, RAMAN_CLASSES))
    lo, hi = RAMAN_CUTOFF[klass]
    b_lo, b_hi = RAMAN_BANDWIDTH[klass]
    offset = 0.0
    if klass != "lab" and rng.random() < OUTLIER_SHARE:
        offset = rng.choice((-1, 1)) * rng.uniform(4.0, 9.0)
    return RamanInstrument(
        klass=klass,
        laser_nm=int(_weighted(rng, EXCITATION_NM)),
        bandwidth_cm1=round(rng.uniform(b_lo, b_hi), 1),
        cutoff_cm1=round(rng.uniform(lo, hi), 0),
        tol=rng.choice(RAMAN_TOL[klass]),
        calibration=str(_weighted(rng, CALIBRATION)),
        offset_cm1=round(offset, 1),
    )


def jitter_raman(rng: random.Random, inst: RamanInstrument) -> float:
    """One band's position error: class-scaled Gaussian plus the unit's
    constant offset. Aggregated over the class mix this reproduces the RRUFF
    within-species quantiles (tested)."""
    return rng.gauss(0.0, RAMAN_SIGMA[inst.klass]) + inst.offset_cm1


def perturb_bands(
    rng: random.Random,
    bands: list[float],
    rel: list[float] | None,
    inst: RamanInstrument,
) -> list[tuple[float, float]]:
    """(position, relative_intensity) pairs as this instrument would report
    them: jittered, cut off below the unit's low limit, weak bands lost on
    wide-slit units, the strongest-band identity flipped when the top two are
    close, renormalised so the strongest LISTED band is 1.00, position-sorted.
    """
    if not bands:
        return []
    if not rel or len(rel) != len(bands):
        # positions only (older payloads): a decaying intensity ladder in the
        # archive's listing order keeps the list shape honest
        rel = [round(1.0 / (1 + 0.35 * i), 3) for i in range(len(bands))]
    pairs = [(float(b), float(r)) for b, r in zip(bands, rel)]
    # weak-band loss grows with bandwidth: floor 0.03 (sharp) .. 0.15 (wide)
    floor = 0.03 + 0.12 * min(1.0, max(0.0, (inst.bandwidth_cm1 - 2.0) / 13.0))
    kept = [(b, r) for b, r in pairs if b >= inst.cutoff_cm1 and r >= floor]
    if not kept:
        kept = pairs[:1]
    kept = [(b + jitter_raman(rng, inst), r * rng.uniform(0.85, 1.15)) for b, r in kept]
    # strongest-identity flip
    by_rel = sorted(kept, key=lambda p: -p[1])
    if (
        len(by_rel) >= 2
        and by_rel[0][1] - by_rel[1][1] <= FLIP_MARGIN
        and rng.random() < FLIP_P
    ):
        top, second = by_rel[0], by_rel[1]
        kept = [
            (b, second[1] if (b, r) == top else (top[1] if (b, r) == second else r))
            for b, r in kept
        ]
    top = max(r for _, r in kept) or 1.0
    out = [(round(b, 1), round(min(1.0, r / top), 2)) for b, r in kept]
    return sorted(out, key=lambda p: p[0])


def fmt_peaks(pairs: list[tuple[float, float]]) -> str:
    """The seeker schema: ``465.1 (1.00), 206.8 (0.42)`` -- corpus_inverse.fmt_peaks."""
    return ", ".join(f"{b:.1f} ({r:.2f})" for b, r in pairs)


# ── LIBS ─────────────────────────────────────────────────────────────


@dataclass
class LibsInstrument:
    window_nm: tuple[float, float]
    resolution_nm: float
    plasma_t_k: float
    gate_scale: dict = field(default_factory=dict)  # stage label -> intensity scale
    self_absorption: bool = False

    def slots(self) -> dict:
        lo, hi = self.window_nm
        return {
            "window": f"{int(lo)}–{int(hi)} nm",
            "libs_T": f"{self.plasma_t_k:,.0f}",
            "resolution": f"{self.resolution_nm:g} nm",
            "instrument_class": "broadband" if hi - lo > 600 else "windowed",
        }


def sample_libs(rng: random.Random) -> LibsInstrument:
    lo_t, hi_t = LIBS_T_RANGE
    return LibsInstrument(
        window_nm=tuple(_weighted(rng, LIBS_WINDOWS)),  # type: ignore[arg-type]
        resolution_nm=round(rng.uniform(*LIBS_RESOLUTION), 2),
        plasma_t_k=round(rng.uniform(lo_t, hi_t), -2),
        gate_scale={},
        self_absorption=rng.random() < 0.35,
    )


def perturb_libs(
    rng: random.Random, groups: list[dict], inst: LibsInstrument
) -> list[dict]:
    """Groups as ``libs_lines`` payloads hold them ({stage_label, lines:[{nm,
    rel, ritz}]}) as this instrument would report them: positions nudged by
    calibration, out-of-window lines dropped, per-stage (matrix/gating)
    intensity scaling, the strongest resonance lines flattened when the unit
    self-absorbs, renormalised so the strongest listed line is 100."""
    lo, hi = inst.window_nm
    out = []
    for g in groups:
        scale = rng.uniform(0.6, 1.4)
        lines = []
        for ln in g.get("lines") or []:
            nm = float(ln["nm"])
            if nm < lo or nm > hi:
                continue
            nm += rng.choice((-1, 1)) * rng.uniform(*LIBS_POS_JITTER)
            rel = float(ln.get("rel") or 0.0) * scale
            if inst.self_absorption and rel >= 60:
                rel *= rng.uniform(0.45, 0.8)
            lines.append({**ln, "nm": round(nm, 2), "rel": rel})
        if lines:
            out.append({**g, "lines": lines})
    top = max((ln["rel"] for g in out for ln in g["lines"]), default=0.0) or 1.0
    for g in out:
        for ln in g["lines"]:
            ln["rel"] = round(100.0 * ln["rel"] / top, 1)
    return out


def instrument_record(inst) -> dict:
    """What the doc provenance keeps about the sampled instrument."""
    d = asdict(inst)
    if "window_nm" in d:
        d["window_nm"] = list(d["window_nm"])
    return d
