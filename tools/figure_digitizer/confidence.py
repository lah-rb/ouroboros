"""Detection curve: how likely is a plotted line to survive digitisation?

Fitted 2026-09-01 on 116,808 peak-observations from the synthetic instrument
(5 real LIBS spectra x 5 spans x 4 pens x 3 dpi), floored 10-sigma detection
against raw-data truth. Held out BY SPAN the model scores AUC 0.71-0.88 and is
calibrated within 0.03 per decile; at the FIGURE level it predicts recovery
with R2 0.942 and median error 0.035 (dev/FIGURE_DIGITIZER_2026-08-30.md,
"Pushing separation below the pen, and predicting it").

WHAT IT IS FOR. Two things the digitiser could not previously say:

  * TRIAGE -- before spending a vision call on a figure, its grid and pen
    alone give recovery to a factor of ~2 (geometry-only R2 0.89). A survey
    figure whose d50 is several nm is not worth the call.
  * CONFIDENCE -- after extraction, the recovered peaks' own separations and
    relative intensities give the expected recovery of THIS figure to a few
    percent, and a per-peak probability that goes out with the value.

WHAT IT IS NOT. Not a claim about any single peak's truth. `resolvable_unit`
(grid x stroke) is the pen-merge term alone and understates the real limit
~2x; d50 here carries the grid term the bench showed dominates, and the
intensity term that absorbed the remaining scatter. "Minimum resolvable
feature" is a CURVE in separation and intensity, not a number.

Units: `grid` is data-units per pixel, `pen` is the measured stroke in data
units (grid x stroke_px), `sep` is neighbour separation in data units,
`intensity` is the line's relative intensity on the 0-1 normalised trace.
Everything is unit-agnostic -- the core never learns what a nm is.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

# Frozen logistic coefficients. Feature order is load-bearing:
#   z = B0 + B1*ln(sep/grid) + B2*ln(sep/pen) + B3*ln(intensity) + B4*ln(pen/grid)
# Do not refit casually: a refit must reproduce the held-out numbers above and
# be recorded in the dev doc, or the tests below will say so.
B0 = 0.369612
B1 = 0.645865  # separation in GRID units   -- the max-pooling term
B2 = 0.515010  # separation in PEN units    -- the merge term
B3 = 0.768281  # relative intensity         -- the strongest single term
B4 = 0.130855  # stroke in pixels (pen/grid)

FIT_N = 116_808
FIT_DATE = "2026-09-01"

# Median relative intensity of a 10-sigma line in the fitting set. Used when a
# caller has geometry but no line list yet (triage).
MEDIAN_INTENSITY = 0.053

# Triage thresholds on expected recovery at median intensity for a nominal
# line list. Chosen from the span sweep: <50 nm figures sit ~0.9, 100 nm
# ~0.5, surveys <0.15.
TRIAGE_DIGITIZE = 0.60
TRIAGE_LOW = 0.25


def logit_detect(sep: float, intensity: float, grid: float, pen: float) -> float:
    if not (sep > 0 and intensity > 0 and grid > 0 and pen > 0):
        return -math.inf
    return (
        B0
        + B1 * math.log(sep / grid)
        + B2 * math.log(sep / pen)
        + B3 * math.log(intensity)
        + B4 * math.log(pen / grid)
    )


def p_detect(sep: float, intensity: float, grid: float, pen: float) -> float:
    """Probability a line at this separation and intensity survives the pen,
    the grid and a floored 10-sigma criterion."""
    z = logit_detect(sep, intensity, grid, pen)
    if z == -math.inf:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


def d50(grid: float, pen: float, intensity: float = MEDIAN_INTENSITY) -> float:
    """Separation at which detection is 50/50, in DATA units.

    Closed form from the logit: sep50 = exp(-(B0 - B1 ln g - B2 ln p
    + B3 ln I + B4 ln(p/g)) / (B1 + B2)). At median intensity this reduces to
    roughly grid x (5.2 + 0.62 x stroke_px) -- 6.4 / 7.3 / 8.7 / 10.1 px at 2 / 3 / 5 / 8 px pens.
    """
    if not (grid > 0 and pen > 0 and intensity > 0):
        return math.inf
    k = (
        B0
        - B1 * math.log(grid)
        - B2 * math.log(pen)
        + B3 * math.log(intensity)
        + B4 * math.log(pen / grid)
    )
    return math.exp(-k / (B1 + B2))


def expected_recovery(
    seps: Sequence[float],
    intensities: Sequence[float] | None,
    grid: float,
    pen: float,
) -> float:
    """Mean detection probability over a figure's line list.

    `seps` are nearest-neighbour separations in data units; `intensities`
    the corresponding relative intensities (median assumed when None). This
    is the quantity that scored R2 0.942 against observed recovery.
    """
    seps = list(seps)
    if not seps:
        return 0.0
    if intensities is None:
        intensities = [MEDIAN_INTENSITY] * len(seps)
    return sum(p_detect(s, i, grid, pen) for s, i in zip(seps, intensities)) / len(seps)


def neighbour_separations(positions: Iterable[float]) -> list[float]:
    """Nearest-neighbour separation for each position (inf for a lone peak)."""
    pos = sorted(positions)
    if len(pos) < 2:
        return [math.inf] * len(pos)
    out = []
    for i, p in enumerate(pos):
        left = p - pos[i - 1] if i > 0 else math.inf
        right = pos[i + 1] - p if i + 1 < len(pos) else math.inf
        out.append(min(left, right))
    return out


def triage(
    grid: float,
    pen: float,
    seps: Sequence[float] | None = None,
    intensities: Sequence[float] | None = None,
    nominal_sep: float | None = None,
    nominal_intensity: float = MEDIAN_INTENSITY,
) -> dict:
    """Should this figure be digitised, and how much should we trust it?

    Two bases, and the difference between them is load-bearing:

    * ``line_list`` -- the figure's own recovered peaks (`seps`,
      `intensities`) give its expected recovery directly. This is the
      quantity that scored R2 0.942.
    * ``geometry_only`` -- before extraction, recovery is estimated for a
      line at `nominal_sep`, an ABSOLUTE separation in data units that the
      technique adapter supplies (for LIBS, the median nearest-neighbour
      spacing of 10-sigma lines in real spectra). It must be absolute: a
      first version scaled it to 3 x d50 and returned 0.78 for a 20 nm zoom,
      a 100 nm window and a 600 nm survey alike -- a verdict that could not
      fail. Caught by the sweep's own numbers (0.9 / 0.5 / <0.15).

    With neither a line list nor a nominal separation there is no basis for
    a verdict, and the function says so rather than guessing.
    """
    d = d50(grid, pen)
    if seps:
        rec = expected_recovery(seps, intensities, grid, pen)
        basis = "line_list"
    elif nominal_sep and nominal_sep > 0:
        rec = p_detect(nominal_sep, nominal_intensity, grid, pen)
        basis = "geometry_only"
    else:
        return {
            "d50_unit": d,
            "expected_recovery": None,
            "basis": "none",
            "verdict": "no_basis",
            "model": {"fit_date": FIT_DATE, "n": FIT_N},
        }
    verdict = (
        "digitize"
        if rec >= TRIAGE_DIGITIZE
        else "digitize_low_confidence" if rec >= TRIAGE_LOW else "refuse"
    )
    return {
        "d50_unit": d,
        "expected_recovery": rec,
        "basis": basis,
        "verdict": verdict,
        "model": {"fit_date": FIT_DATE, "n": FIT_N},
    }
