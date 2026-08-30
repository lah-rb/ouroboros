"""Render spectra into plots whose truth is known exactly.

This is an instrument, not a fixture. It is built BEFORE the extractor on
purpose: an extractor written first gets tuned against figures somebody
eyeballed, and every number it then produces is unfalsifiable. Here the
polyline, the axis mapping and the tick values are all known by construction,
so a later measurement can be scored rather than admired.

Rendering goes through pymupdf rather than matplotlib, which is absent from
the root venv and stays absent. That is not a compromise — it is better for
this job:

  - the ground-truth polyline is exact, in PDF user space;
  - the output is genuine VECTOR art, so the vector tier is testable;
  - the same page renders at any dpi, so the render tier is testable;
  - embedding that render as a JPEG in a second PDF gives an exact native
    raster case with a CONTROLLABLE native/crop ratio;
  - and it exercises the same pymupdf calls the digitiser itself makes.

The adversarial arms are independently toggleable because the point is to
find which one breaks the extractor, not to produce a plot that looks nice.
Real corpus figures are boxed, tick inward, carry an exponent multiplier on
the y-axis, and have annotation boxes with arrows crossing the trace — every
one of those is an arm here.
"""

from __future__ import annotations

import dataclasses
import math
import os

import fitz  # pymupdf
import numpy as np

# Matplotlib's default figure is 6.4x4.8in at 100dpi; published figures cluster
# near it, so it is the default geometry here too.
DEFAULT_W_PT = 6.4 * 72
DEFAULT_H_PT = 4.8 * 72


@dataclasses.dataclass
class PlotSpec:
    """One point in the adversarial grid."""

    width_pt: float = DEFAULT_W_PT
    height_pt: float = DEFAULT_H_PT
    margin_left: float = 62.0
    margin_right: float = 22.0
    margin_top: float = 34.0
    margin_bottom: float = 48.0

    x_range: tuple[float, float] | None = None  # None = the spectrum's own
    boxed: bool = True  # 4 spines, as most journals draw
    ticks: str = "out"  # out | in | both
    n_major_ticks: int = 7
    minor_ticks: bool = False
    gridlines: bool = False

    y_scale: str = "linear"  # linear | log
    y_exponent: bool = False  # render a "1e4" multiplier over the y-axis
    axis_extends: bool = False  # axis runs past the last tick

    stroke_pt: float = 1.0
    annotations: int = 0  # labelled boxes with arrows into the trace
    legend: bool = False

    title: str = ""
    x_label: str = "Wavelength (nm)"
    y_label: str = "Intensity"


@dataclasses.dataclass
class GroundTruth:
    """Everything the renderer knew, so a measurement can be scored."""

    x_data: np.ndarray  # the exact values drawn
    y_data: np.ndarray
    interior_pt: tuple[float, float, float, float]
    x_domain: tuple[float, float]  # data value at interior left / right edge
    y_domain: tuple[float, float]  # data value at interior bottom / top edge
    x_ticks: list[tuple[float, float]]  # (value, x in points)
    y_ticks: list[tuple[float, float]]
    y_exponent: int
    y_scale: str
    stroke_pt: float
    spec: PlotSpec
    # Where the DRAWN axis line stops. Equal to the interior edge when the
    # axis terminates at the data range, and beyond it when it does not — in
    # which case reading the axis endpoint as the range is wrong by exactly
    # this difference, which is why the digitiser must refuse instead.
    x_axis_end_pt: float = 0.0

    def x_to_pt(self, v):
        x0, x1 = self.x_domain
        lo, _, hi, _ = self.interior_pt
        return lo + (np.asarray(v) - x0) / (x1 - x0) * (hi - lo)

    def y_to_pt(self, v):
        """Data value to page y. Page y grows DOWNWARD, data grows up."""
        y0, y1 = self.y_domain
        _, t, _, b = self.interior_pt
        f = (self._fwd(np.asarray(v, dtype=float)) - self._fwd(y0)) / (
            self._fwd(y1) - self._fwd(y0)
        )
        return b - f * (b - t)

    def _fwd(self, v):
        if self.y_scale == "log":
            return np.log10(np.maximum(v, 1e-12))
        return v

    def nm_per_px(self, dpi: float) -> float:
        lo, _, hi, _ = self.interior_pt
        px = (hi - lo) * dpi / 72.0
        return (self.x_domain[1] - self.x_domain[0]) / px


def load_two_column(path: str) -> tuple[np.ndarray, np.ndarray]:
    """A 2-column wavelength/intensity file, with or without a header.

    The operator's spectra are ``wavelength,intensity`` with a header row;
    reference exports often drop it. Sniffing the first line covers both
    without a format flag nobody would remember to set.
    """
    with open(path, encoding="utf-8-sig") as fh:
        first = fh.readline()
    skip = 0
    try:
        parts = first.replace(",", " ").split()
        float(parts[0])
        float(parts[1])
    except (ValueError, IndexError):
        skip = 1
    data = np.loadtxt(path, delimiter=",", skiprows=skip)
    if data.ndim != 2 or data.shape[1] < 2:
        raise ValueError(f"{path}: expected two columns")
    return data[:, 0].astype(float), data[:, 1].astype(float)


def _ticks_for_step(lo: float, hi: float, step: float) -> list[float]:
    out = []
    v = math.ceil(lo / step) * step
    while v <= hi + step * 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def _nice_ticks(lo: float, hi: float, n: int) -> list[float]:
    """Human tick values across a range, the way a plotting library picks them.

    The step is chosen by which one lands CLOSEST to the requested count, not
    by the first that exceeds a raw spacing. That distinction is what makes
    tick density an actual experimental arm: the naive rule returned 4 ticks
    for every request between 5 and 9 on a 180-961 nm range, so an arm meant
    to sweep 3/5/7/9 would silently have measured one setting four times.
    """
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n - 1, 1)
    mag = 10 ** math.floor(math.log10(raw))
    best: tuple[float, float] | None = None
    for scale in (0.1, 1.0, 10.0):
        for mult in (1, 2, 2.5, 5):
            step = mult * mag * scale
            got = _ticks_for_step(lo, hi, step)
            if not got:
                continue
            cost = abs(len(got) - n)
            if best is None or cost < best[0]:
                best = (cost, step)
    return _ticks_for_step(lo, hi, best[1]) if best else [lo, hi]


def render(
    x: np.ndarray,
    y: np.ndarray,
    spec: PlotSpec,
    out_pdf: str,
) -> GroundTruth:
    """Draw a spectrum as a vector plot and return exactly what was drawn."""
    if spec.x_range is not None:
        m = (x >= spec.x_range[0]) & (x <= spec.x_range[1])
        x, y = x[m], y[m]
    if x.size < 2:
        raise ValueError("empty spectrum after range selection")

    doc = fitz.open()
    page = doc.new_page(width=spec.width_pt, height=spec.height_pt)
    left = spec.margin_left
    right = spec.width_pt - spec.margin_right
    top = spec.margin_top
    bottom = spec.height_pt - spec.margin_bottom
    interior = (left, top, right, bottom)

    x_dom = (float(x.min()), float(x.max()))
    y_lo = float(y.min())
    y_hi = float(y.max())
    if spec.y_scale == "log":
        y_lo = max(y_lo, max(y[y > 0].min() if (y > 0).any() else 1e-6, 1e-6))
    pad = (y_hi - y_lo) * 0.05 or 1.0
    y_dom = (y_lo, y_hi + pad) if spec.y_scale == "linear" else (y_lo, y_hi)

    # An exponent multiplier printed over the axis is how plotting libraries
    # keep tick labels short. The digitiser has to notice it: reading the
    # labels alone puts every intensity out by orders of magnitude.
    exponent = 0
    if spec.y_exponent and y_dom[1] > 0:
        exponent = int(math.floor(math.log10(y_dom[1])))

    gt = GroundTruth(
        x_data=x,
        y_data=y,
        interior_pt=interior,
        x_domain=x_dom,
        y_domain=y_dom,
        x_ticks=[],
        y_ticks=[],
        y_exponent=exponent,
        y_scale=spec.y_scale,
        stroke_pt=spec.stroke_pt,
        spec=spec,
    )

    xt = [
        v for v in _nice_ticks(*x_dom, spec.n_major_ticks) if x_dom[0] <= v <= x_dom[1]
    ]
    if spec.y_scale == "log":
        lo_e, hi_e = math.floor(math.log10(y_dom[0])), math.ceil(math.log10(y_dom[1]))
        yt = [10**e for e in range(lo_e, hi_e + 1) if y_dom[0] <= 10**e <= y_dom[1]]
    else:
        yt = [v for v in _nice_ticks(*y_dom, 6) if y_dom[0] <= v <= y_dom[1]]
    gt.x_ticks = [(v, float(gt.x_to_pt(v))) for v in xt]
    gt.y_ticks = [(v, float(gt.y_to_pt(v))) for v in yt]

    # Every shape below is finish()ed AND commit()ed. finish() only closes the
    # current path group; commit() is what writes it to the page, so a missing
    # commit silently renders nothing at all.
    if spec.gridlines:
        shape = page.new_shape()
        for _, px in gt.x_ticks:
            shape.draw_line(fitz.Point(px, top), fitz.Point(px, bottom))
        for _, py in gt.y_ticks:
            shape.draw_line(fitz.Point(left, py), fitz.Point(right, py))
        shape.finish(color=(0.85, 0.85, 0.85), width=0.5)
        shape.commit()

    # Axes. When the axis "extends", it runs past the last tick — which makes
    # the endpoints useless as calibration anchors, and the digitiser is
    # required to refuse rather than assume they are the range.
    over = 18.0 if spec.axis_extends else 0.0
    gt.x_axis_end_pt = right + over
    shape = page.new_shape()
    if spec.boxed:
        shape.draw_rect(fitz.Rect(left, top, right + over, bottom))
    else:
        shape.draw_line(fitz.Point(left, bottom), fitz.Point(right + over, bottom))
        shape.draw_line(fitz.Point(left, bottom), fitz.Point(left, top - over))
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()

    shape = page.new_shape()
    tick_len = 4.0
    inward = spec.ticks in ("in", "both")
    outward = spec.ticks in ("out", "both")
    for _, px in gt.x_ticks:
        if outward:
            shape.draw_line(fitz.Point(px, bottom), fitz.Point(px, bottom + tick_len))
        if inward:
            shape.draw_line(fitz.Point(px, bottom), fitz.Point(px, bottom - tick_len))
    for _, py in gt.y_ticks:
        if outward:
            shape.draw_line(fitz.Point(left, py), fitz.Point(left - tick_len, py))
        if inward:
            shape.draw_line(fitz.Point(left, py), fitz.Point(left + tick_len, py))
    if spec.minor_ticks and len(gt.x_ticks) > 1:
        step = (gt.x_ticks[1][1] - gt.x_ticks[0][1]) / 2.0
        for _, px in gt.x_ticks[:-1]:
            shape.draw_line(
                fitz.Point(px + step, bottom), fitz.Point(px + step, bottom + 2.0)
            )
    shape.finish(color=(0, 0, 0), width=0.8)
    shape.commit()

    # The trace, drawn as one polyline over every sample. Nothing is
    # downsampled: collapsing many data points into one column IS the
    # rasterisation loss under measurement, and doing it here would hide it.
    px = gt.x_to_pt(x)
    py = gt.y_to_pt(y)
    shape = page.new_shape()
    shape.draw_polyline([fitz.Point(float(a), float(b)) for a, b in zip(px, py)])
    shape.finish(color=(0, 0, 0), width=spec.stroke_pt, closePath=False)
    shape.commit()

    _labels(page, gt, exponent)
    if spec.annotations:
        _annotate(page, gt, spec.annotations)
    if spec.legend:
        _legend(page, gt)

    os.makedirs(os.path.dirname(os.path.abspath(out_pdf)), exist_ok=True)
    doc.save(out_pdf)
    doc.close()
    return gt


def _labels(page, gt: GroundTruth, exponent: int) -> None:
    left, top, right, bottom = gt.interior_pt
    for v, px in gt.x_ticks:
        s = f"{v:g}"
        page.insert_text(
            (px - 3 * len(s) * 0.5, bottom + 15), s, fontsize=8, color=(0, 0, 0)
        )
    scale = 10.0**exponent if exponent else 1.0
    for v, py in gt.y_ticks:
        s = f"{v / scale:g}" if gt.y_scale == "linear" else f"{v:g}"
        page.insert_text((left - 8 - 4.2 * len(s), py + 3), s, fontsize=8)
    if exponent:
        page.insert_text((left - 6, top - 8), f"1e{exponent}", fontsize=8)
    if gt.spec.title:
        page.insert_text(
            ((left + right) / 2 - 2.6 * len(gt.spec.title), top - 12),
            gt.spec.title,
            fontsize=10,
        )
    page.insert_text(
        ((left + right) / 2 - 2.4 * len(gt.spec.x_label), bottom + 34),
        gt.spec.x_label,
        fontsize=9,
    )
    page.insert_text((14, (top + bottom) / 2), gt.spec.y_label, fontsize=9, rotate=90)


def _annotate(page, gt: GroundTruth, n: int) -> None:
    """Labelled boxes with arrows into the trace — the real-figure hazard.

    These sit INSIDE the plot interior in trace-identical ink, which is what
    makes a topmost-pixel rule promote a glyph to a maximal peak. The
    positions are the actual tallest peaks, so the arrows land where it hurts.
    """
    left, top, right, bottom = gt.interior_pt
    order = np.argsort(gt.y_data)[::-1]
    picked: list[int] = []
    for idx in order:
        xv = gt.x_data[idx]
        if all(
            abs(xv - gt.x_data[j]) > (gt.x_domain[1] - gt.x_domain[0]) / 12
            for j in picked
        ):
            picked.append(int(idx))
        if len(picked) >= n:
            break
    for k, idx in enumerate(picked):
        tx = float(gt.x_to_pt(gt.x_data[idx]))
        ty = float(gt.y_to_pt(gt.y_data[idx]))
        bx = left + 14 + (k % 3) * (right - left - 30) / 3
        by = top + 14 + (k // 3) * 28
        box = fitz.Rect(bx, by, bx + 74, by + 18)
        shape = page.new_shape()
        shape.draw_rect(box)
        shape.finish(color=(0, 0, 0), fill=(1, 1, 0.75), width=0.6)
        shape.commit()
        shape = page.new_shape()
        shape.draw_line(fitz.Point(box.x1, box.y1), fitz.Point(tx, ty))
        shape.finish(color=(0, 0, 0), width=0.6)
        shape.commit()
        page.insert_text(
            (bx + 4, by + 13),
            f"x={gt.x_data[idx]:.3f}",
            fontsize=7,
        )


def _legend(page, gt: GroundTruth) -> None:
    left, top, right, _ = gt.interior_pt
    box = fitz.Rect(right - 104, top + 8, right - 8, top + 38)
    shape = page.new_shape()
    shape.draw_rect(box)
    shape.finish(color=(0, 0, 0), fill=(1, 1, 1), width=0.6)
    shape.commit()
    shape = page.new_shape()
    shape.draw_line(
        fitz.Point(box.x0 + 6, box.y0 + 14), fitz.Point(box.x0 + 26, box.y0 + 14)
    )
    shape.finish(color=(0, 0, 0), width=1.0)
    shape.commit()
    page.insert_text((box.x0 + 32, box.y0 + 17), "sample", fontsize=7)


def rasterize(
    pdf_path: str,
    out_png: str,
    dpi: int = 160,
    jpeg_quality: int | None = None,
) -> str:
    """Render a synthetic plot to pixels, optionally through a JPEG.

    The JPEG arm matters because a stored corpus crop has already been through
    one encode, and ringing around a 1-2 px peak is the same magnitude as the
    signal it sits on.
    """
    from PIL import Image

    doc = fitz.open(pdf_path)
    pm = doc[0].get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    doc.close()
    if jpeg_quality is not None:
        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=jpeg_quality)
        buf.seek(0)
        img = Image.open(buf).convert("RGB")
    os.makedirs(os.path.dirname(os.path.abspath(out_png)), exist_ok=True)
    img.save(out_png)
    return out_png
