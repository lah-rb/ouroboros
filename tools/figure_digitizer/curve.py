"""Getting the plotted trace back out of the pixels.

The hard part is not following a line, it is refusing everything else drawn in
the same ink. A published spectrum figure routinely carries rotated element
labels, arrows pointing into peaks, and a legend box — all black, all inside
the plot interior. A naive topmost-ink-per-column rule promotes a glyph to a
maximal-intensity peak, which is the exact failure the peak-picking convention
exists to prevent: a missed peak costs a fact, an invented one teaches a
falsehood.

The mechanism is connected components. A trace spans the interior; labels,
arrowheads and legend boxes do not. One flood fill removes all of them.

Phase 1 is single-trace by operator ruling. Multi-series figures are DETECTED
AND REFUSED rather than silently flattened, because reading a ten-trace
calibration figure as one trace would invent a spectrum that was never
plotted.
"""

from __future__ import annotations

import dataclasses

import numpy as np
from scipy import ndimage

# A component must cover this much of the interior width to be the trace.
_TRACE_MIN_COVERAGE = 0.55
# A leftover component this large, overlapping the trace's columns, means
# annotation ink is touching the curve.
_OVERLAP_MIN_PIXELS = 40
# Columns whose neighbour-to-neighbour slope is below this (in pixels of rise
# per pixel of run) count as locally flat for stroke measurement.
_FLAT_SLOPE_PX = 1.0
# Saturated, distinct hues covering this much of the interior are a series.
_SERIES_MIN_COVERAGE = 0.25
_SERIES_MIN_SATURATION = 0.25
# A hue whose pixels FILL this much of their own bounding box is a painted
# region -- a legend panel or a yellow annotation box -- not a trace. Without
# this, an annotated figure reports two series and is refused as multi-series.
_SERIES_MAX_FILL = 0.30

# A component whose topmost-ink series varies less than this is a straight
# rule -- a gridline -- not a plotted curve.
_MIN_TRACE_VARIATION_PX = 1.0
# How far in from the TOP and SIDES a near-solid line counts as frame.
_SPINE_BAND_FRAC = 0.03
_SPINE_SOLID_FRAC = 0.85
# A flat shelf covering this fraction of the interior width, sitting this far
# above the trace's own level, is a label or legend box edge.
_PLATEAU_MIN_FRAC = 0.02
_PLATEAU_MIN_RISE_FRAC = 0.15
# ...and a shelf must end with a jump this large, as a box side does. A broad
# peak apex is flat over as many columns but is left gradually.
_PLATEAU_EDGE_JUMP_FRAC = 0.06


@dataclasses.dataclass
class Trace:
    """One extracted curve, in interior pixel coordinates."""

    x_px: np.ndarray  # interior column index
    y_px: np.ndarray  # page-y of the pen CENTRE; NaN where the trace is absent
    stroke_px: float
    coverage: float
    flags: list[str]

    @property
    def n_points(self) -> int:
        return int(np.count_nonzero(np.isfinite(self.y_px)))


def count_series(rgb: np.ndarray, ink: np.ndarray) -> int:
    """How many distinct coloured series are drawn.

    Achromatic ink is one bucket however dark it is, because a black trace, a
    grey gridline and a black axis are not three series.

    A series is judged per CONNECTED COMPONENT, not per hue: a trace is one
    long, thin component, while annotation boxes are several small dense ones.
    Measuring fill across a whole hue instead lets four yellow label boxes
    spread over the plot share one large sparse bounding box, pass the
    thinness test, and refuse the figure as multi-series.
    """
    if rgb.ndim != 3 or not ink.any():
        return 1
    px = rgb.astype(np.float64)
    mx = px.max(axis=2)
    mn = px.min(axis=2)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-9), 0.0)
    chromatic = ink & (sat >= _SERIES_MIN_SATURATION)
    achromatic = ink & ~chromatic

    width = float(ink.shape[1]) or 1.0
    n = 1 if achromatic.any() else 0

    r, g, b = px[:, :, 0], px[:, :, 1], px[:, :, 2]
    denom = np.maximum(mx - mn, 1e-9)
    h = np.where(
        mx == r,
        ((g - b) / denom) % 6,
        np.where(mx == g, (b - r) / denom + 2, (r - g) / denom + 4),
    )
    hue = np.clip(h.astype(int), 0, 5)

    for bucket in range(6):
        sel = chromatic & (hue == bucket)
        if sel.sum() < 8:
            continue
        labels, count = ndimage.label(sel, structure=np.ones((3, 3), dtype=int))
        for idx in range(1, count + 1):
            comp = labels == idx
            cols = np.nonzero(comp.any(axis=0))[0]
            rows = np.nonzero(comp.any(axis=1))[0]
            if cols.size / width < _SERIES_MIN_COVERAGE:
                continue
            bw = cols.max() - cols.min() + 1
            bh = rows.max() - rows.min() + 1
            if comp.sum() / float(bw * bh) > _SERIES_MAX_FILL:
                continue  # a painted panel, not a line
            n += 1
            break
    return max(n, 1)


def _stroke_width(mask: np.ndarray, top: np.ndarray) -> float:
    """Pen width, measured only where the trace is locally FLAT.

    On a steep segment the vertical ink run is long because of SLOPE, not
    because of the pen. Averaging over all columns therefore overestimates the
    stroke and biases every extracted value downward, so the estimate is taken
    from columns whose neighbours sit at nearly the same height.
    """
    h, w = mask.shape
    runs: list[int] = []
    for x in range(1, w - 1):
        if not (
            np.isfinite(top[x - 1]) and np.isfinite(top[x]) and np.isfinite(top[x + 1])
        ):
            continue
        if abs(top[x + 1] - top[x - 1]) > 2 * _FLAT_SLOPE_PX:
            continue
        y = int(top[x])
        run = 0
        while y + run < h and mask[y + run, x]:
            run += 1
        if run:
            runs.append(run)
    if not runs:
        return 1.0
    vals, counts = np.unique(np.asarray(runs), return_counts=True)
    return float(vals[int(np.argmax(counts))])


def _plateau_above_trace(top: np.ndarray, height: int) -> bool:
    """Whether the topmost-ink series rests on a long flat shelf up high.

    This is what annotation furniture actually DOES to the extraction: the top
    edge of a label or legend box is straight, so many consecutive columns
    report an identical topmost row, well above where the curve lives.
    Detecting the box as a dense blob does not work — a LIBS survey is
    genuinely solid ink through its peak clusters, so an opening flags every
    clean figure too.

    A shelf must also END abruptly. A box has vertical sides, so the topmost
    row jumps by a large fraction of the plot at its edge; a broad peak's
    apex is flat over just as many columns but is approached and left
    gradually, which is what made this fire on clean zoomed figures.
    """
    fin = np.isfinite(top)
    if fin.sum() < 16:
        return False
    vals = top[fin]
    median = float(np.median(vals))
    min_run = max(8, int(_PLATEAU_MIN_FRAC * len(vals)))
    high_by = _PLATEAU_MIN_RISE_FRAC * height
    edge_jump = _PLATEAU_EDGE_JUMP_FRAC * height

    start = 0
    for k in range(1, len(vals) + 1):
        same = k < len(vals) and abs(vals[k] - vals[k - 1]) <= 1.0
        if same:
            continue
        run = k - start
        if run >= min_run and vals[start] < median - high_by:
            before = abs(vals[start] - vals[start - 1]) if start > 0 else edge_jump
            after = abs(vals[k] - vals[k - 1]) if k < len(vals) else edge_jump
            if max(before, after) >= edge_jump:
                return True
        start = k
    return False


def _top_series(sel: np.ndarray, width: int) -> np.ndarray:
    top = np.full(width, np.nan)
    for x in range(width):
        rows = np.nonzero(sel[:, x])[0]
        if rows.size:
            top[x] = float(rows.min())
    return top


def _strip_spines(ink: np.ndarray) -> np.ndarray:
    """Remove the frame's top and side lines, never its bottom.

    The trace TOUCHES the frame where it meets the plot edge, so the two are
    one connected component whose topmost-ink series is the flat top spine.
    Skipping flat components then discards the trace along with the
    furniture, and every figure comes back empty.

    Only the top and the sides are stripped. The bottom is where a spectrum's
    baseline lives, and an earlier version that stripped a band at every edge
    deleted 41% of the ink on a noisy baseline and shattered the curve into
    99 fragments. The x-axis itself is already outside the caller's crop.
    """
    out = ink.copy()
    h, w = out.shape
    band_r = max(1, int(_SPINE_BAND_FRAC * h))
    band_c = max(1, int(_SPINE_BAND_FRAC * w))
    for r in range(min(band_r, h)):
        if out[r, :].mean() >= _SPINE_SOLID_FRAC:
            out[r, :] = False
    for c in list(range(min(band_c, w))) + list(range(max(0, w - band_c), w)):
        if out[:, c].mean() >= _SPINE_SOLID_FRAC:
            out[:, c] = False
    return out


def extract_trace(ink: np.ndarray) -> Trace | None:
    """The plotted curve inside an already-cropped plot interior.

    Components are taken widest-first, but one is only accepted once its
    topmost-ink series actually VARIES. That single test does the work of two
    earlier mechanisms that both misfired:

      - a fixed pixel inset cannot exclude a frame spine, because line
        thickness scales with dpi; at 600 dpi the top spine spanned the full
        width, won the widest-component test, and the trace came back dead
        flat along the top of the plot;
      - stripping near-solid rows in a band at each edge DID remove the
        spine, but it also removed trace ink: on a spectrum whose baseline
        noise fills those rows it deleted 41% of the ink and shattered the
        curve into 99 fragments, none wide enough to qualify.

    A spine is straight; a plotted curve is not.

    KNOWN LIMIT: a spectrum with a mathematically EXACT flat baseline is
    straight too, and will be skipped along with the furniture. Real
    instrument output always carries baseline noise, so this bites only on
    synthetic data.
    """
    if not ink.any():
        return None
    ink = _strip_spines(ink)
    if not ink.any():
        return None
    labels, n = ndimage.label(ink, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return None

    width = ink.shape[1]
    spans = []
    for idx in range(1, n + 1):
        cols = np.nonzero(labels == idx)[1]
        spans.append((len(np.unique(cols)), idx))
    spans.sort(reverse=True)

    chosen = None
    for span, idx in spans:
        if span / float(width or 1) < _TRACE_MIN_COVERAGE:
            break  # sorted, so nothing narrower qualifies either
        sel = labels == idx
        top = _top_series(sel, width)
        fin = top[np.isfinite(top)]
        if fin.size and float(np.std(fin)) < _MIN_TRACE_VARIATION_PX:
            continue  # a frame spine or a rule, not a curve
        chosen = (span, idx, sel, top)
        break
    if chosen is None:
        return None
    span, best_idx, sel, top = chosen

    stroke = _stroke_width(sel, top)
    flags: list[str] = []

    # Annotation furniture that MERGED with the trace leaves no separate
    # component to find -- an arrow physically connects a label box to the
    # curve. What it does instead is put the topmost-ink series on a long
    # flat shelf high above the trace, which is directly detectable.
    if _plateau_above_trace(top, ink.shape[0]):
        flags.append("annotation_overlap")

    # ...and furniture that did NOT merge shows up as a leftover component
    # sitting inside the trace's own column span.
    trace_cols = np.nonzero(sel.any(axis=0))[0]
    lo, hi = int(trace_cols.min()), int(trace_cols.max())
    for span_o, idx_o in spans:
        if idx_o == best_idx:
            continue
        comp = labels == idx_o
        if comp.sum() < _OVERLAP_MIN_PIXELS:
            continue
        cols = np.nonzero(comp.any(axis=0))[0]
        if cols.size and lo <= cols.mean() <= hi:
            if "annotation_overlap" not in flags:
                flags.append("annotation_overlap")
            break

    # The topmost ink pixel is the TOP EDGE of the pen; the value it draws is
    # the pen's centre, half a stroke further down the page. And column k
    # spans [k, k+1), so its sample sits at the centre k+0.5 -- mapping the
    # left edge puts every position out by exactly half a pixel.
    return Trace(
        x_px=np.arange(width, dtype=float) + 0.5,
        y_px=top + stroke / 2.0,
        stroke_px=stroke,
        coverage=float(span) / float(width or 1),
        flags=flags,
    )


def to_values(trace: Trace, x_cal, y_cal, x_offset_px: float, y_offset_px: float):
    """Map an extracted trace into data coordinates.

    Offsets convert interior-local pixels back to whole-image pixels, which is
    the frame the calibrations were fitted in.
    """
    xs = x_cal.to_value(trace.x_px + x_offset_px)
    ys = y_cal.to_value(trace.y_px + y_offset_px) if y_cal else trace.y_px
    return np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)


def relative_intensity(y: np.ndarray) -> np.ndarray:
    """Normalise a trace to 0-1 without asserting a continuum model.

    Phase 1 does NOT subtract a baseline. Where y=0 sits is a fact the axis
    calibration supplies; what the spectrum's pedestal means is a physical
    modelling choice, and asserting one here would be the same class of error
    as ranking intensities across elements.
    """
    finite = np.isfinite(y)
    if not finite.any():
        return y
    lo = float(np.nanmin(y[finite]))
    hi = float(np.nanmax(y[finite]))
    if hi <= lo:
        return np.zeros_like(y)
    return (y - lo) / (hi - lo)
