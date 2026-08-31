"""Picking peaks off an extracted trace.

Threshold policy follows the corpus convention already in use for reference
spectra: a missed peak costs a fact, an invented one teaches a falsehood. So
the prominence floor stays conservative and nothing is ever padded to reach a
count.

One deliberate difference from that convention: no top-N cap. A cap is right
for a training record; this is an ARTIFACT, and a 200-900 nm survey
legitimately carries dozens of lines. Capping here destroys information that
cannot be recovered without re-running the whole pipeline. Consumers cap.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
from scipy.ndimage import median_filter, minimum_filter1d
from scipy.signal import find_peaks, peak_widths

# Matches the reference peak-picker's min_prominence_frac so the two are
# directly comparable. Retained for that comparison only -- see `pick`.
DEFAULT_PROMINENCE_FRAC = 0.05

# Detection is defined against LOCAL noise, following the IUPAC/NIST
# convention (limit of detection at 3 sigma over local background, limit of
# quantitation at 10). 5 sigma sits on the conservative side of that.
DEFAULT_SNR_SIGMA = 5.0
# Local background and noise are estimated over a window several resolvable
# widths across, expressed in SAMPLES tied to the stroke rather than in data
# units -- a fixed nm window that suits a 780 nm survey is wider than a
# zoomed panel's entire range.
_BG_WINDOW_STROKES = 15
_BG_WINDOW_MIN = 9
_NOISE_DETREND = 5


# What is left of the position bias after the tick-centre geometry is
# corrected. It is NOT a constant to subtract: it tracks the rasteriser, not
# the figure. Changing only the antialiasing level of the renderer -- same
# geometry, same data -- moved it by 0.2 px, and 80% of the corpus is
# rasterised by publishers with unknown settings. It is reported as
# uncertainty rather than removed.
RESIDUAL_POSITION_BIAS_PX = 0.25


@dataclasses.dataclass
class Peak:
    """One peak, in data coordinates, with its own uncertainty."""

    position: float
    relative_intensity: float
    prominence: float
    fwhm: float | None
    position_uncertainty: float
    index: int

    def as_dict(self, unit_suffix: str = "") -> dict:
        pos_key = f"position_{unit_suffix}" if unit_suffix else "position"
        unc_key = (
            f"position_uncertainty_{unit_suffix}"
            if unit_suffix
            else "position_uncertainty"
        )
        fwhm_key = f"fwhm_{unit_suffix}" if unit_suffix else "fwhm"
        return {
            pos_key: round(self.position, 4),
            "relative_intensity": round(self.relative_intensity, 4),
            "prominence": round(self.prominence, 4),
            fwhm_key: round(self.fwhm, 4) if self.fwhm is not None else None,
            unc_key: round(self.position_uncertainty, 4),
        }


def _local_background_and_noise(
    y: np.ndarray, stroke_px: float
) -> tuple[np.ndarray, np.ndarray]:
    """Rolling continuum and rolling noise sigma for an extracted trace.

    The continuum is a rolling minimum smoothed by a median of the same
    width, which follows a spectrum's pedestal without being dragged up by
    the lines sitting on it. Noise is the rolling MAD of the trace after a
    short median detrend, scaled to sigma by 1.4826.
    """
    win = max(_BG_WINDOW_MIN, int(round(_BG_WINDOW_STROKES * max(stroke_px, 1.0))))
    win |= 1  # median_filter wants an odd size to stay centred
    win = min(win, max(3, (len(y) // 2) * 2 - 1))
    bg = median_filter(
        minimum_filter1d(y, size=win, mode="nearest"), size=win, mode="nearest"
    )
    resid = np.abs(y - median_filter(y, size=_NOISE_DETREND, mode="nearest"))
    sigma = 1.4826 * median_filter(resid, size=win, mode="nearest")
    positive = sigma[sigma > 0]
    floor = float(np.percentile(positive, 25)) if positive.size else 1e-9
    return bg, np.maximum(sigma, floor)


def _parabolic_offset(y: np.ndarray, i: int) -> float:
    """Sub-pixel apex offset from the three samples around a maximum.

    Measured over 811 peaks against continuous source positions, this is NOT
    the clean ~0.2 px win it is usually assumed to be. It trades the typical
    case for the tail: median error 0.36 -> 0.51 px, p95 1.40 -> 0.98 px. A
    peak a couple of samples wide is a spike rather than a parabola, so the
    fitted offset is partly noise; on resolved peaks it is real.

    It is kept because the TAIL is what matters here — a p95 under one pixel
    means almost no peak is misplaced by a whole sample — and a gate on peak
    width was tried and found inert, since nearly every peak in a dense
    survey clears any sensible width threshold.
    """
    if i <= 0 or i >= len(y) - 1:
        return 0.0
    a, b, c = float(y[i - 1]), float(y[i]), float(y[i + 1])
    denom = a - 2.0 * b + c
    if denom == 0 or not math.isfinite(denom):
        return 0.0
    off = 0.5 * (a - c) / denom
    return off if -1.0 < off < 1.0 else 0.0


def pick(
    x: np.ndarray,
    y: np.ndarray,
    *,
    criterion: str = "snr",
    snr_sigma: float = DEFAULT_SNR_SIGMA,
    stroke_px: float = 2.0,
    prominence_frac: float = DEFAULT_PROMINENCE_FRAC,
    min_distance_px: float = 1.0,
    position_uncertainty: float = 0.0,
) -> list[Peak]:
    """Peaks of a trace already mapped into data coordinates.

    ``criterion`` selects how a peak is called:

    ``"snr"`` (default) requires a peak to stand ``snr_sigma`` above the LOCAL
    continuum, which is the IUPAC/NIST convention and the only one of the two
    that is independent of the figure's dynamic range.

    ``"prominence"`` requires a fixed fraction of the FULL intensity range.
    That is not a standard criterion and it is hostage to the brightest
    feature on the page: with the tallest line at 25,000 counts it imposes a
    flat ~1,250-count floor everywhere, so a clean isolated line in a quiet
    region is discarded because of a large line hundreds of nm away.
    Measured on a real survey figure it recovered 12 of the 23 physically
    resolvable peaks against 16 for the SNR rule. It is kept only so the two
    can be compared.

    ``y`` is expected normalised 0-1 (see curve.relative_intensity).
    """
    finite = np.isfinite(x) & np.isfinite(y)
    if finite.sum() < 3:
        return []
    xf = np.asarray(x, dtype=float)[finite]
    yf = np.asarray(y, dtype=float)[finite]

    span = float(np.nanmax(yf) - np.nanmin(yf))
    if span <= 0:
        return []
    distance = max(1, int(math.ceil(min_distance_px)))
    if criterion == "snr":
        bg, sigma = _local_background_and_noise(yf, stroke_px)
        idx, props = find_peaks(
            yf,
            height=bg + snr_sigma * sigma,
            prominence=snr_sigma * sigma,
            distance=distance,
        )
    elif criterion == "prominence":
        idx, props = find_peaks(
            yf, prominence=prominence_frac * span, distance=distance
        )
    else:
        raise ValueError(f"unknown criterion {criterion!r}")
    if idx.size == 0:
        return []

    try:
        widths, _, _, _ = peak_widths(yf, idx, rel_height=0.5)
    except Exception:  # noqa: BLE001 — a degenerate width is not fatal
        widths = np.full(idx.shape, np.nan)

    # Local sample spacing converts a width in samples into data units, and
    # is not assumed uniform: a zoomed panel and a survey differ by 50x.
    out: list[Peak] = []
    for k, i in enumerate(idx):
        off = _parabolic_offset(yf, int(i))
        if 0 < i < len(xf) - 1:
            step = (xf[i + 1] - xf[i - 1]) / 2.0
        elif len(xf) > 1:
            step = xf[1] - xf[0]
        else:
            step = 0.0
        pos = float(xf[i] + off * step)
        w = widths[k] if k < len(widths) else np.nan
        fwhm = float(abs(w * step)) if np.isfinite(w) else None
        out.append(
            Peak(
                position=pos,
                relative_intensity=float(yf[i]),
                prominence=float(props["prominences"][k]),
                fwhm=fwhm,
                position_uncertainty=float(position_uncertainty),
                index=int(np.nonzero(finite)[0][i]),
            )
        )
    out.sort(key=lambda p: p.position)
    return out


def match(
    found: list[Peak], truth: list[float], tolerance: float
) -> tuple[list[tuple[Peak, float]], list[Peak], list[float]]:
    """Greedy nearest-position pairing, for scoring against known peaks.

    Returns (pairs, unmatched_found, unmatched_truth). Greedy on absolute
    distance rather than in order, so one spurious detection cannot cascade
    into a chain of wrong pairings.
    """
    pairs: list[tuple[Peak, float]] = []
    free_truth = sorted(truth)
    used_found: set[int] = set()
    cands = []
    for fi, p in enumerate(found):
        for ti, t in enumerate(free_truth):
            d = abs(p.position - t)
            if d <= tolerance:
                cands.append((d, fi, ti))
    cands.sort()
    used_truth: set[int] = set()
    for d, fi, ti in cands:
        if fi in used_found or ti in used_truth:
            continue
        used_found.add(fi)
        used_truth.add(ti)
        pairs.append((found[fi], free_truth[ti]))
    unmatched_f = [p for i, p in enumerate(found) if i not in used_found]
    unmatched_t = [t for i, t in enumerate(free_truth) if i not in used_truth]
    return pairs, unmatched_f, unmatched_t


# ── Reference alignment ───────────────────────────────────────────────

# A shift larger than this many resolvable widths means the reference was
# matched to the wrong peak. Refuse rather than move everything somewhere new.
MAX_REFERENCE_SHIFT_WIDTHS = 2.0
# ...and a candidate must be found within this many resolvable widths of the
# reference's stated position to be considered at all.
REFERENCE_SEARCH_WIDTHS = 3.0
# A shift SMALLER than this is not worth making. The offset estimated from one
# reference carries its own error (measured sd 0.64 px), so correcting a bias
# below that floor injects more noise than it removes: on a correctly
# calibrated axis, aligning took median position error from 0.183 nm to 0.343.
# Break-even sits near 0.25 resolvable widths, which is where this is set.
MIN_REFERENCE_SHIFT_WIDTHS = 0.25
# Two independent references must imply shifts agreeing within this, or one of
# them matched the wrong line.
REFERENCE_AGREEMENT_WIDTHS = 0.75


@dataclasses.dataclass
class Alignment:
    """A single global offset, and the evidence for applying it."""

    shift: float = 0.0
    applied: bool = False
    reference_value: float | None = None
    reference_source: str = "none"
    matched_position: float | None = None
    reason: str = "no reference available"

    def as_dict(self, unit_suffix: str = "") -> dict:
        key = f"shift_{unit_suffix}" if unit_suffix else "shift"
        return {
            key: round(self.shift, 5),
            "applied": self.applied,
            "reference_value": self.reference_value,
            "reference_source": self.reference_source,
            "matched_position": (
                round(self.matched_position, 5)
                if self.matched_position is not None
                else None
            ),
            "reason": self.reason,
        }


def align_to_reference(
    found: list[Peak],
    references: list[float],
    *,
    resolvable: float,
    source: str = "unknown",
) -> Alignment:
    """One global offset from reference lines of known position.

    This is wavelength calibration, not identification, and the distinction is
    what keeps it inside the project's epistemic rule. A SINGLE offset is
    estimated and applied to EVERY peak, so the spectrum's relative structure
    is untouched and the correction stays falsifiable — if the reference was
    matched to the wrong feature, every other line moves with it and the
    disagreement shows. Snapping each peak individually to its nearest
    catalogue value would instead manufacture agreement, which is the failure
    `interconnect` exists to prevent.

    A single reference can only fix an OFFSET. It cannot fix a scale error:
    the slope comes from the tick fit and is never touched here, so a spectrum
    whose nm-per-pixel is wrong will be pinned correctly at the reference and
    drift away from it.

    Three refusals, each measured rather than assumed:

    * no candidate near any reference;
    * a shift below the noise floor — the estimate carries sd ~0.64 px of its
      own, so correcting a well-calibrated axis made things WORSE (median
      error 0.183 nm to 0.343) until this dead-band was added;
    * references that disagree with each other, which is what a mis-locked
      match looks like from the inside.
    """
    if not found or not references:
        return Alignment(reason="no reference available")
    if not math.isfinite(resolvable) or resolvable <= 0:
        return Alignment(reason="figure has no resolvable width")

    search = REFERENCE_SEARCH_WIDTHS * resolvable

    # References closer together than the search window cannot be told apart
    # by this figure: both lock onto the SAME peak and then appear to disagree
    # by exactly their own separation. Ca II H and K are 3.481 nm apart, which
    # is inside the window at survey resolution, and that is precisely how
    # they failed -- every cell refused with "references disagree by 3.4810".
    usable: list[float] = []
    for ref in sorted(references):
        if all(abs(ref - u) > search for u in usable):
            usable.append(ref)

    hits: list[tuple[float, float, float]] = []  # (shift, ref, matched position)
    claimed: list[float] = []
    for ref in usable:
        near = [
            p
            for p in found
            if abs(p.position - ref) <= search and all(p.position != c for c in claimed)
        ]
        if not near:
            continue
        p = min(near, key=lambda q: abs(q.position - ref))
        claimed.append(p.position)
        hits.append((ref - p.position, ref, p.position))
    if not hits:
        return Alignment(reason="no peak near any reference")

    shifts = np.array([h[0] for h in hits], dtype=float)
    # Independent references must tell the same story. When they do not, the
    # nearest peak to at least one of them is not the line it claims to be --
    # which is exactly how a large axis error fails, by locking onto a
    # NEIGHBOURING line and implying a plausible but wrong shift.
    if (
        len(shifts) > 1
        and float(np.ptp(shifts)) > REFERENCE_AGREEMENT_WIDTHS * resolvable
    ):
        return Alignment(
            shift=float(np.median(shifts)),
            reference_value=hits[0][1],
            reference_source=source,
            matched_position=hits[0][2],
            reason=(
                f"references disagree by {float(np.ptp(shifts)):.4f}; at least "
                "one matched the wrong line"
            ),
        )

    k = int(np.argmin(np.abs(shifts - np.median(shifts))))
    shift, ref, pos = hits[k]
    floor = MIN_REFERENCE_SHIFT_WIDTHS * resolvable
    cap = MAX_REFERENCE_SHIFT_WIDTHS * resolvable
    if abs(shift) < floor:
        return Alignment(
            shift=shift,
            reference_value=ref,
            reference_source=source,
            matched_position=pos,
            reason=(
                f"implied shift {shift:.4f} is below {floor:.4f}; the axis "
                "agrees with the reference and correcting would add noise"
            ),
        )
    if abs(shift) > cap:
        return Alignment(
            shift=shift,
            reference_value=ref,
            reference_source=source,
            matched_position=pos,
            reason=f"implied shift {shift:.4f} exceeds {cap:.4f}; reference likely mismatched",
        )
    # Whether a second, independent reference agreed is part of the claim: a
    # single-reference alignment has nothing to catch a mis-locked match, and
    # the artifact must not present the two as equally supported.
    return Alignment(
        shift=shift,
        applied=True,
        reference_value=ref,
        reference_source=source,
        matched_position=pos,
        reason=(
            "aligned"
            if len(shifts) > 1
            else "aligned (single reference, no cross-check)"
        ),
    )


def apply_alignment(found: list[Peak], alignment: Alignment) -> list[Peak]:
    """Translate every peak by the alignment's offset, or none of them."""
    if not alignment.applied or alignment.shift == 0.0:
        return found
    return [
        dataclasses.replace(p, position=p.position + alignment.shift) for p in found
    ]
