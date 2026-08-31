"""Finding the axes, and turning pixels into data coordinates.

This module carries the error term that rasterisation bounds do NOT cover.
How finely a figure is sampled is a fact about its pixels, measurable and
capped; where its axes say those pixels sit is a reading of printed tick
labels, and a calibration off by one tick spacing produces a perfectly
plausible spectrum in the wrong place. Nothing about resolution catches that.

So the defence is overdetermination and refusal, not cleverness: fit N matched
(pixel, value) pairs rather than assuming two endpoints, report the residual
as a first-class number, and reject the figure when the ticks do not fit a
line. A figure we do not understand is not a figure to guess at.

Everything here is technique-agnostic. No unit is named; values are whatever
the caller's tick labels said.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

# Ink is anything this far from the modal background, on a 0-255 scale. A
# fixed floor beats a purely adaptive threshold because a mostly-white plot
# has a near-zero deviation median, which would make any MAD multiple collapse.
_INK_MIN_DELTA = 40.0
_INK_SPREAD_FRAC = 0.35

# A line must span this much of the ink bounding box to be a frame or axis
# candidate. Gridlines and a flat spectrum baseline also clear it, which is
# why span alone never decides.
_LINE_SPAN_FRAC = 0.55
# Ticks are short marks perpendicular to the axis, and their length is a
# FRACTION of the plot, not a pixel count: a 4pt tick is 9px at 160 dpi and
# 33px at 600. Fixed pixel bounds silently found no ticks at all above about
# 300 dpi, and then rejected every axis for having none.
_TICK_MIN_FRAC = 0.004
_TICK_MAX_FRAC = 0.045
_TICK_MIN_FLOOR = 2
_TICK_MAX_FLOOR = 12
# Marks closer together than this fraction of the plot are one tick.
_TICK_MERGE_FRAC = 0.006
# Major ticks are evenly spaced, and they are placed to sub-pixel accuracy —
# so the grid tolerance is ABSOLUTE, not a fraction of the step. A
# proportional tolerance lets a WRONG step survive: seeded on a frame corner
# it drifted 21 px per interval and still fit inside 18% of a 128 px step,
# absorbing the real ticks into a bogus grid.
_TICK_GRID_TOL_FRAC = 0.004
_TICK_GRID_TOL_FLOOR = 2.0
# Minor ticks are drawn shorter than major ones. Below this ratio the two
# cannot be told apart and everything found is treated as major.
_MAJOR_MINOR_RATIO = 1.4
# A calibration this far off a straight line means the figure was not
# understood. Refuse rather than emit a plausible wrong answer.
MAX_RESIDUAL_PX = 2.0
# Declaring a log axis is asymmetric on purpose: a false log call corrupts
# every value, a missed one is only a rejection.
_LOG_TRIGGER_RESIDUAL_PX = 1.5
_LOG_IMPROVEMENT_FACTOR = 3.0
# When tick count and label count disagree, the winning alignment must beat
# the runner-up by this fraction of the value range, or the pairing is a
# guess and the figure is refused.
_ALIGN_MIN_MARGIN_FRAC = 0.05


@dataclasses.dataclass
class Axis:
    """One detected axis line and the tick marks attached to it."""

    orientation: str  # "x" or "y"
    position_px: int  # row (for x) or column (for y) the line sits on
    span_px: tuple[int, int]
    ticks_px: list[float]

    @property
    def n_ticks(self) -> int:
        return len(self.ticks_px)


@dataclasses.dataclass
class Calibration:
    """A pixel-to-value mapping, with the evidence for trusting it."""

    scale: str  # "linear" or "log"
    slope: float
    intercept: float
    n_points: int
    r2: float
    max_residual_px: float
    source_of_labels: str = "unknown"
    alignment_margin: float = math.inf
    ambiguous: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.max_residual_px <= MAX_RESIDUAL_PX
            and self.n_points >= 2
            and not self.ambiguous
        )

    def to_value(self, px):
        v = self.slope * np.asarray(px, dtype=float) + self.intercept
        return 10**v if self.scale == "log" else v

    def unit_per_px(self) -> float:
        """Grid spacing in data units. Meaningless for a log axis."""
        return abs(self.slope)


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Boolean mask of drawn pixels.

    Background is the MODAL luminance rather than an Otsu split: plots with a
    shaded panel or a large filled legend push Otsu's threshold into the
    middle of the ink, while the mode stays on the paper.
    """
    g = gray.astype(np.float64)
    hist = np.bincount(np.clip(g, 0, 255).astype(np.uint8).ravel(), minlength=256)
    bg = float(np.argmax(hist))
    dev = np.abs(g - bg)
    spread = float(np.percentile(dev, 99.5))
    return dev > max(_INK_MIN_DELTA, _INK_SPREAD_FRAC * spread)


def _longest_run(row: np.ndarray) -> tuple[int, int, int]:
    """(length, start, end) of the longest contiguous True run."""
    if not row.any():
        return (0, 0, 0)
    padded = np.concatenate(([False], row, [False]))
    d = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    lengths = ends - starts
    k = int(np.argmax(lengths))
    return (int(lengths[k]), int(starts[k]), int(ends[k] - 1))


def _line_candidates(mask: np.ndarray, axis: int, min_span: float) -> list[tuple]:
    """Rows (axis=1) or columns (axis=0) holding a long contiguous ink run.

    Total ink prefilters the scan: a line spanning `min_span` pixels must have
    at least that many ink pixels, and at 600 dpi that prunes ~4,000 rows down
    to a handful before any run-length work happens.
    """
    totals = mask.sum(axis=1 if axis == 1 else 0)
    out = []
    for i in np.flatnonzero(totals >= min_span):
        line = mask[i, :] if axis == 1 else mask[:, i]
        length, a, b = _longest_run(line)
        if length >= min_span:
            out.append((int(i), length, a, b))
    return out


def _largest_regular_subset(
    marks: list[tuple[float, float]], tol: float
) -> list[tuple[float, float]]:
    """The biggest subset of marks lying on a common arithmetic grid.

    Major ticks are evenly spaced; the things that impersonate them are not.
    A spectrum trace descending to touch the x-axis produces short
    perpendicular runs at many columns — indistinguishable from inward ticks
    one at a time, and obviously not ticks once you ask them to be regular.

    Ties on length are broken by how tightly the chain fits its own step, so a
    sloppy grid seeded on a frame corner loses to the true one.
    """
    if len(marks) < 3:
        return list(marks)
    pts = sorted(marks, key=lambda m: m[0])
    best: list[tuple[float, float]] = []
    best_err = math.inf
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            step = pts[j][0] - pts[i][0]
            if step <= tol:
                continue
            chain = [pts[i]]
            errs: list[float] = []
            expect = pts[i][0] + step
            for p in pts[j:]:
                if abs(p[0] - expect) <= tol:
                    chain.append(p)
                    errs.append(abs(p[0] - expect))
                    expect = p[0] + step
            err = float(np.mean(errs)) if errs else math.inf
            if len(chain) > len(best) or (len(chain) == len(best) and err < best_err):
                best, best_err = chain, err
    return best if len(best) >= 3 else list(marks)


def _major_only(marks: list[tuple[float, float]]) -> list[float]:
    """Drop minor ticks, which are drawn shorter than major ones.

    Without this the minor ticks are found too — correctly, since they really
    are regular — and then paired ordinally against the major LABELS, which
    silently shifts every value. A 15-mark grid read against 8 labels put the
    calibration out by 700 nm while reporting a 0.37 px residual.

    Runs AFTER the regularity filter, never before. Applied to the raw marks
    it reads its length statistics off a population contaminated by the
    trace's own touch points, and those are long enough to push the cut above
    the real ticks and evict them — which took a working inward-tick case from
    8 correct ticks to 4 wrong ones.
    """
    if len(marks) < 3:
        return list(marks)
    lengths = np.array([m[1] for m in marks], dtype=float)
    lo = float(np.percentile(lengths, 25))
    hi = float(np.percentile(lengths, 75))
    if lo <= 0 or hi / lo < _MAJOR_MINOR_RATIO:
        return list(marks)
    cut = (lo + hi) / 2.0
    kept = [m for m in marks if m[1] >= cut]
    return kept if len(kept) >= 3 else list(marks)


def _marks_on_side(
    mask: np.ndarray,
    pos: int,
    span: tuple[int, int],
    orientation: str,
    sign: int,
    tick_min: int,
    tick_max: int,
    merge: int,
) -> list[tuple[float, float]]:
    """Tick marks on ONE side of a candidate axis line, as (centre, length)."""
    a, b = span
    h, w = mask.shape
    hits: list[tuple[int, int]] = []
    for k in range(a, b + 1):
        run = 0
        for d in range(2, tick_max + 2):
            p = pos + sign * d
            if orientation == "x":
                if not (0 <= p < h) or not mask[p, k]:
                    break
            else:
                if not (0 <= p < w) or not mask[k, p]:
                    break
            run += 1
        if tick_min <= run <= tick_max:
            hits.append((k, run))

    marks: list[tuple[float, float]] = []
    group: list[tuple[int, int]] = []
    for k, run in hits:
        if group and k - group[-1][0] > merge:
            marks.append(
                (float(np.mean([g[0] for g in group])), float(max(g[1] for g in group)))
            )
            group = []
        group.append((k, run))
    if group:
        marks.append(
            (float(np.mean([g[0] for g in group])), float(max(g[1] for g in group)))
        )
    return marks


def _count_ticks(
    mask: np.ndarray,
    pos: int,
    span: tuple[int, int],
    orientation: str,
    plot_size: int,
) -> list[float]:
    """Tick marks attached perpendicular to a candidate axis line.

    The OUTWARD side is tried first and kept if it yields a regular grid,
    because outside the plot interior there is nothing but ticks and labels.
    Inward ticks are common enough in journal figures that they must still be
    supported — but inside the interior the trace itself descends to meet the
    axis, and those touch points are short perpendicular runs indistinguishable
    from ticks except by regularity.
    """
    tick_min = max(_TICK_MIN_FLOOR, int(_TICK_MIN_FRAC * plot_size))
    tick_max = max(_TICK_MAX_FLOOR, int(_TICK_MAX_FRAC * plot_size))
    merge = max(2, int(_TICK_MERGE_FRAC * plot_size))

    # For x the interior is ABOVE the axis (smaller row index), so outward is
    # +1; for y the interior is to the RIGHT, so outward is -1.
    outward = +1 if orientation == "x" else -1
    tol = max(_TICK_GRID_TOL_FLOOR, _TICK_GRID_TOL_FRAC * plot_size)
    for sign in (outward, -outward):
        marks = _marks_on_side(
            mask, pos, span, orientation, sign, tick_min, tick_max, merge
        )
        # Regularity first — it is the strong discriminator and it removes the
        # trace's touch points. Only then is length used to drop minor ticks,
        # on a population that is already all ticks.
        regular = _largest_regular_subset(marks, tol)
        major = _major_only(regular)
        if len(major) < len(regular):
            major = _largest_regular_subset(major, tol)
        if len(major) >= 3:
            return [m[0] for m in major]
    return []


def find_axes(mask: np.ndarray) -> tuple[Axis | None, Axis | None]:
    """The x and y axis lines of a plot, if this looks like a plot at all.

    "Longest run of a minority colour" is not enough, and the ways it fails
    are all present in real figures: plots are BOXED so there are four long
    runs not two; GRIDLINES can outrun an axis broken by inward ticks; and the
    spectrum's own flat baseline is a full-width dark run sitting a few pixels
    above the x-axis.

    The discriminator is two-part — an axis lies on the boundary of the ink
    bounding box AND has tick marks attached perpendicular to it. The baseline
    satisfies neither, and a gridline satisfies neither.
    """
    ys, xs = np.nonzero(mask)
    if ys.size == 0:
        return None, None
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    box_w = max(x1 - x0, 1)
    box_h = max(y1 - y0, 1)

    rows = _line_candidates(mask, 1, _LINE_SPAN_FRAC * box_w)
    cols = _line_candidates(mask, 0, _LINE_SPAN_FRAC * box_h)

    def pick(cands, orientation, prefer_high):
        plot_size = box_h if orientation == "x" else box_w
        scored = []
        for pos, _length, a, b in cands:
            ticks = _count_ticks(mask, pos, (a, b), orientation, plot_size)
            if len(ticks) < 2:
                continue
            scored.append((pos, a, b, ticks))
        if not scored:
            return None
        # Among lines that carry ticks, take the outermost — the bottom row
        # for x, the leftmost column for y. An inner line with ticks would be
        # a twin axis, which phase 1 refuses elsewhere.
        best = (
            max(scored, key=lambda s: s[0])
            if prefer_high
            else min(scored, key=lambda s: s[0])
        )
        return Axis(orientation, best[0], (best[1], best[2]), best[3])

    return pick(rows, "x", True), pick(cols, "y", False)


def interior(mask: np.ndarray, x_axis: Axis, y_axis: Axis) -> tuple[int, int, int, int]:
    """The plot's data region as (left, top, right, bottom) in pixels."""
    ys, xs = np.nonzero(mask)
    top = int(ys.min())
    right = int(xs.max())
    return (y_axis.position_px, top, right, x_axis.position_px)


def calibrate(
    ticks_px: list[float],
    values: list[float],
    *,
    source_of_labels: str = "unknown",
    allow_log: bool = True,
    alignment_margin: float = math.inf,
) -> Calibration:
    """Fit pixel positions to data values over N points.

    N points rather than two endpoints is the whole defence against a
    hallucinated axis range. If a vision model reports 200-900 but seven tick
    labels fit a line at 0.4 px residual, the ticks win and the claimed range
    is discarded. If the two cannot be reconciled, the caller refuses.
    """
    px = np.asarray(ticks_px, dtype=float)
    vals = np.asarray(values, dtype=float)
    if px.size != vals.size or px.size < 2:
        return Calibration("linear", 0.0, 0.0, int(px.size), 0.0, math.inf)

    # Three collinear points always fit a line, so a sparse detection paired
    # against a longer label list can be confidently wrong: a thick trace hid
    # five of eight ticks, the best of six alignments won on noise, and the
    # calibration came out 6,343 nm off while reporting a 0.28 px residual.
    # Refuse when the runner-up alignment explains the marks nearly as well.
    ambiguous = alignment_margin < _ALIGN_MIN_MARGIN_FRAC * max(
        float(np.ptp(vals)), 1e-9
    )

    def fit(v):
        slope, intercept = np.polyfit(px, v, 1)
        pred = slope * px + intercept
        resid = v - pred
        # Residual is reported in PIXELS, not data units, so the number means
        # the same thing on a wavelength axis and a 2-theta one.
        resid_px = np.abs(resid / slope) if slope != 0 else np.abs(resid) * math.inf
        ss_res = float(np.sum(resid**2))
        ss_tot = float(np.sum((v - v.mean()) ** 2)) or 1e-12
        return slope, intercept, 1.0 - ss_res / ss_tot, float(np.max(resid_px))

    slope, intercept, r2, max_res = fit(vals)
    scale = "linear"

    if allow_log and max_res > _LOG_TRIGGER_RESIDUAL_PX and np.all(vals > 0):
        l_slope, l_intercept, l_r2, l_max_res = fit(np.log10(vals))
        if l_max_res * _LOG_IMPROVEMENT_FACTOR < max_res:
            scale, slope, intercept, r2, max_res = (
                "log",
                l_slope,
                l_intercept,
                l_r2,
                l_max_res,
            )

    return Calibration(
        scale=scale,
        slope=float(slope),
        intercept=float(intercept),
        n_points=int(px.size),
        r2=float(r2),
        max_residual_px=float(max_res),
        source_of_labels=source_of_labels,
        alignment_margin=float(alignment_margin),
        ambiguous=bool(ambiguous),
    )


def _grid_indices(seq: list[float]) -> tuple[np.ndarray, float]:
    """Integer position of each entry on its own regular grid.

    The base step is the SMALLEST gap present, because a missing entry shows
    up as a doubled gap and the median would then be wrong by that factor.
    """
    arr = np.asarray(seq, dtype=float)
    if arr.size < 2:
        return np.zeros(arr.size, dtype=int), 0.0
    diffs = np.diff(arr)
    base = float(np.min(diffs[diffs > 0])) if np.any(diffs > 0) else 0.0
    if base <= 0:
        return np.zeros(arr.size, dtype=int), 0.0
    return np.round((arr - arr[0]) / base).astype(int), base


def match_labels_to_ticks(
    ticks_px: list[float], values: list[float], *, descending: bool = False
) -> tuple[list[float], list[float], float]:
    """Pair detected tick positions with label values; report the ambiguity.

    Returns (ticks, values, margin) where margin is how much worse the
    runner-up alignment fits. It is infinite when the counts agree, and small
    when several alignments explain the marks equally well — in which case the
    caller must refuse rather than pick one.

    ``descending`` says the values fall as the pixel coordinate rises, which
    is ALWAYS the case for a y-axis: page y grows downward while the plotted
    quantity grows upward. Sorting both ascending silently mirrors the axis,
    and because evenly spaced ticks fit a straight line just as well reversed,
    the result is a calibration that is upside down at a 0.31 px residual —
    wrong on every one of ten rendered y-axes while reporting a clean fit.

    Both sequences are monotone along the axis, so ordinal pairing is correct
    when the counts agree. When they DISAGREE, taking the first n of each is
    only right if the missing marks are at the end — and they usually are not.
    A thick trace obscured one interior tick, ordinal truncation shifted every
    remaining pair by one, and the calibration came out 99 nm wrong while
    still reporting a 0.43 px residual.

    So every alignment of the shorter run against the longer is scored, and
    the best-fitting one wins. Ticks and labels are both on regular grids, so
    the correct alignment fits enormously better than any other.
    """
    t = sorted(float(x) for x in ticks_px)
    v = sorted((float(x) for x in values), reverse=descending)
    if min(len(t), len(v)) < 2:
        return [], [], 0.0
    if len(t) == len(v):
        return t, v, math.inf

    # Both runs sit on regular grids, so each entry gets an integer index and
    # alignment is a single offset between the two index sets. A sliding
    # CONTIGUOUS window cannot express a gap in the middle, which is exactly
    # what an obscured interior tick produces — that shape fit at a 28 px
    # residual because no window could skip the hole.
    ti, tstep = _grid_indices(t)
    vi, vstep = _grid_indices(v)
    if tstep <= 0 or vstep <= 0:
        n = min(len(t), len(v))
        return t[:n], v[:n], 0.0

    vpos = {int(k): val for k, val in zip(vi, v)}
    scored: list[tuple[float, list[float], list[float]]] = []
    for off in range(-len(v), len(v) + 1):
        pt, pv = [], []
        for k, px_ in zip(ti, t):
            hit = vpos.get(int(k) + off)
            if hit is not None:
                pt.append(px_)
                pv.append(hit)
        if len(pt) < 2:
            continue
        slope, intercept = np.polyfit(pt, pv, 1)
        resid = float(
            np.max(np.abs(np.asarray(pv) - (slope * np.asarray(pt) + intercept)))
        )
        # A pairing that explains more marks is worth more than one that
        # explains two of them perfectly.
        scored.append((resid - 0.001 * len(pt), pt, pv))
    if not scored:
        n = min(len(t), len(v))
        return t[:n], v[:n], 0.0
    scored.sort(key=lambda s: s[0])
    margin = scored[1][0] - scored[0][0] if len(scored) > 1 else math.inf
    return scored[0][1], scored[0][2], margin
