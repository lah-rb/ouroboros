"""Where a figure crop came from, and how much information it can carry.

The stored crops under ``databank/figures/<key>/fig_NN.png`` are 160-dpi page
renders that paddlex cut out and the extractor renamed. Both the page index
and the bounding box were discarded on the way through ``_collect_figures``,
and ``fig_NN`` is neither reading order nor collision-free across pages — so a
crop on disk has no link back to the document it came from.

This module re-derives that link instead of waiting on an extractor change.
Normalised cross-correlation of the crop against a page render locates it
exactly (measured 24/24 crops at NCC >= 0.98 across six papers), which then
unlocks the two things the crop itself cannot give:

  - the NATIVE embedded image, typically 2.8x the crop's resolution, because
    the crop is a render of a placement whereas the PDF holds the original;
  - the VECTOR paths, where the figure was drawn rather than photographed, in
    which case there is no rasterisation loss at all.

Every tier reports its own precision, and the two halves of the page-render
tier are kept apart on purpose: re-rendering vector art at 600 dpi is a
genuine information gain, while re-rendering a placed JPEG at 600 dpi is a
finer grid over the same information. Conflating them manufactures a
precision number that is not real.
"""

from __future__ import annotations

import dataclasses
import io
import math
import os

import fitz  # pymupdf
import numpy as np
from PIL import Image
from scipy.signal import fftconvolve

# paddlex renders pages at this dpi, so a stored crop is in these pixels and
# `pt = px * 72 / CROP_DPI` converts a crop box back to PDF user space.
CROP_DPI = 160.0
PT_PER_INCH = 72.0

# A relocation is accepted at this correlation. Real figure crops measured
# 0.9916-0.9995; the floor sits below that band and well above noise.
MIN_NCC = 0.98
# ...and must beat the best correlation OUTSIDE its own neighbourhood by this
# much, so a page carrying two near-identical panels cannot silently bind the
# crop to the wrong one.
MIN_MARGIN = 0.05
# Radius (in crop pixels) suppressed around the peak before reading the
# runner-up. The correlation surface is smooth near its maximum, so a margin
# read too close to the peak measures the surface's own width, not a rival.
_PEAK_SUPPRESS_PX = 30

# A crop this close to the page render's own dimensions is the known
# full-page leak, not a figure: paddlex sometimes emits the whole page as a
# region and it lands in the crop directory as a low-numbered fig_NN.
_FULL_PAGE_TOL_PX = 4

# How much of the located rect an image placement must cover — or how much of
# the placement must fall inside the rect — before its xref is accepted as
# that figure's native source. Below this the placement merely overlaps.
_NATIVE_COVER_FRAC = 0.95
# ...and when the placement is the contained one, it must still be this much
# of the rect, so a logo sitting inside a figure box cannot win.
_NATIVE_MIN_RECT_FRAC = 0.40

# Curve-shape detector. Journal PDFs are full of long vector paths that are
# TEXT GLYPH OUTLINES, not plot traces — a typical one runs 113 segments over
# a 55x6 pt box and is non-monotone in x. A plot curve is long in x, tall
# enough to carry signal, and advances left-to-right.
_CURVE_MIN_POINTS = 40
_CURVE_MIN_X_EXTENT_PT = 120.0
_CURVE_MIN_Y_EXTENT_PT = 30.0
_CURVE_MIN_MONOTONE_FRAC = 0.90


@dataclasses.dataclass(frozen=True)
class Relocation:
    """Where on which page a stored crop actually sits."""

    page: int
    ncc: float
    margin: float
    rect_pt: tuple[float, float, float, float]
    crop_px: tuple[int, int]
    full_page: bool

    @property
    def located(self) -> bool:
        return self.ncc >= MIN_NCC and (self.full_page or self.margin >= MIN_MARGIN)


@dataclasses.dataclass(frozen=True)
class Source:
    """The pixels a digitiser should read, and what they can resolve.

    ``tier`` is one of vector / native_raster / render_vector / render_resampled.
    The last two are both page renders and differ in what a higher dpi buys:
    real detail for vector-backed art, a finer grid over the same information
    for a placed raster.
    """

    tier: str
    page: int
    rect_pt: tuple[float, float, float, float]
    limited_by: str
    effective_dpi: float
    resolution_gain_vs_crop: float
    xref: int | None = None
    native_px: tuple[int, int] | None = None
    sub_box_px: tuple[float, float, float, float] | None = None
    native_rect_pt: tuple[float, float, float, float] | None = None
    rect_covered_by_native: float = 0.0
    native_ext: str | None = None
    n_vector_curves: int = 0
    jpeg_round_trips: int = 1


def _gray(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("L"), dtype=np.float64)


def _page_gray(doc, pno: int, dpi: float = CROP_DPI) -> np.ndarray:
    pm = doc[pno].get_pixmap(dpi=int(dpi))
    img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    return _gray(img)


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of two equally-shaped images, for the full-page case."""
    if a.shape != b.shape:
        return -1.0
    x = a.ravel() - a.mean()
    y = b.ravel() - b.mean()
    denom = math.sqrt(float(np.dot(x, x)) * float(np.dot(y, y)))
    return float(np.dot(x, y) / denom) if denom > 0 else -1.0


def _ncc(template: np.ndarray, page: np.ndarray) -> tuple[float, float, int, int]:
    """Peak normalised cross-correlation of template within page.

    Returns (best, runner_up, x, y). The runner-up is read with the peak's own
    neighbourhood suppressed, so it reports a rival match rather than the
    shoulder of the same one.
    """
    if template.shape[0] > page.shape[0] or template.shape[1] > page.shape[1]:
        return (-1.0, -1.0, 0, 0)
    t = template - template.mean()
    t_energy = float(np.sum(t * t))
    if t_energy <= 0:  # a blank crop correlates with everything
        return (-1.0, -1.0, 0, 0)

    num = fftconvolve(page, t[::-1, ::-1], mode="valid")
    ones = np.ones_like(t)
    s1 = fftconvolve(page, ones, mode="valid")
    s2 = fftconvolve(page * page, ones, mode="valid")
    n = t.size
    var = np.maximum(s2 - s1 * s1 / n, 1e-9)
    r = num / np.maximum(np.sqrt(var * t_energy), 1e-9)

    y, x = np.unravel_index(np.argmax(r), r.shape)
    best = float(r[y, x])
    rr = r.copy()
    rr[
        max(0, y - _PEAK_SUPPRESS_PX) : y + _PEAK_SUPPRESS_PX,
        max(0, x - _PEAK_SUPPRESS_PX) : x + _PEAK_SUPPRESS_PX,
    ] = -1.0
    runner = float(rr.max()) if rr.size else -1.0
    return (best, runner, int(x), int(y))


class PageIndex:
    """Per-page geometry, built once and reused across a paper's crops.

    Both halves of this are things that must not be recomputed per crop:
    ``get_drawings()`` parses a page's content stream, and rendering a page is
    the single most expensive step in the sweep. A paper's figures are heavily
    clustered onto a few pages, so the render cache almost always hits.
    """

    def __init__(self, doc, max_pages: int = 0):
        self.doc = doc
        self.n = doc.page_count if max_pages <= 0 else min(doc.page_count, max_pages)
        self._sizes: list[list[tuple[float, float]]] | None = None
        self._renders: dict[int, np.ndarray] = {}

    @property
    def sizes(self) -> list[list[tuple[float, float]]]:
        if self._sizes is None:
            self._sizes = [_page_rect_sizes(self.doc[p]) for p in range(self.n)]
        return self._sizes

    def gray(self, pno: int) -> np.ndarray:
        img = self._renders.get(pno)
        if img is None:
            img = _page_gray(self.doc, pno)
            # A paper's crops cluster onto a handful of pages, so a small
            # cache captures nearly all the reuse without holding a 190-page
            # document's renders in memory.
            if len(self._renders) > 8:
                self._renders.clear()
            self._renders[pno] = img
        return img


def _page_rect_sizes(page) -> list[tuple[float, float]]:
    """Dimensions of every drawing bbox and image placement on a page."""
    sizes: list[tuple[float, float]] = []
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is not None and r.width > 1 and r.height > 1:
                sizes.append((r.width, r.height))
        for img in page.get_images(full=True):
            try:
                for r in page.get_image_rects(img[0]):
                    if r.width > 1 and r.height > 1:
                        sizes.append((r.width, r.height))
            except Exception:  # noqa: BLE001 — an unreachable xref is not fatal
                continue
    except Exception:  # noqa: BLE001 — a broken page must not stop the sweep
        return []
    return sizes


def _candidate_pages(index: PageIndex, crop_px: tuple[int, int]) -> list[int]:
    """Pages holding a region shaped like this crop, best first.

    A correlation sweep costs one FFT per page. Almost always the crop's own
    placement is still on the page as a drawing bbox or an image rect, so
    ranking by dimension match first collapses the sweep to one or two
    candidates. Pages matching nothing are still swept, just last — which is
    why this ORDERS the document rather than filtering it.
    """
    want_w = crop_px[0] * PT_PER_INCH / CROP_DPI
    want_h = crop_px[1] * PT_PER_INCH / CROP_DPI
    scored: list[tuple[float, int]] = []
    for pno, sizes in enumerate(index.sizes):
        best = math.inf
        for w, h in sizes:
            err = abs(w - want_w) / want_w + abs(h - want_h) / want_h
            best = min(best, err)
        scored.append((best, pno))
    scored.sort()
    return [pno for _, pno in scored]


def relocate(crop_path: str, doc, index: PageIndex | None = None) -> Relocation | None:
    """Locate a stored crop on its source page.

    Every page is swept. An earlier 40-page cap looked like a cost control and
    was really a silent truncation: 26 of 30 relocation failures in the first
    corpus survey were figures on page 41 or later of a long paper, reported
    as "could not place this" when the sweep had simply never looked. Ranking
    by dimension match keeps the usual cost at one or two correlations no
    matter how long the document is, so the cap bought nothing.

    Returns None only when the crop cannot be read. A crop that correlates
    poorly still comes back — with its measured ncc and margin — because "we
    could not place this" is a result the caller records, not an error it
    swallows.
    """
    try:
        with Image.open(crop_path) as im:
            crop = _gray(im)
            crop_px = (im.width, im.height)
    except Exception:  # noqa: BLE001 — an unreadable crop is a datum, not a crash
        return None

    if index is None:
        index = PageIndex(doc)

    best: tuple[float, float, int, int, int] | None = None
    full_page = False
    for pno in _candidate_pages(index, crop_px):
        page = index.gray(pno)

        # A crop the size of the whole page is the known full-page leak — but
        # EVERY page of a uniform document matches that shape, so the
        # dimensions say what the crop IS and never which page it came from.
        # At equal size the correlation degenerates to a single Pearson r,
        # cheap enough to run against every page and exact enough to pick the
        # right one. Returning on the first dimension match instead binds the
        # crop to whichever page happened to sort first.
        if (
            abs(page.shape[1] - crop_px[0]) <= _FULL_PAGE_TOL_PX
            and abs(page.shape[0] - crop_px[1]) <= _FULL_PAGE_TOL_PX
        ):
            full_page = True
            r = _pearson(crop, page)
            if best is None or r > best[0]:
                best = (r, -1.0, 0, 0, pno)
            continue

        ncc, runner, x, y = _ncc(crop, page)
        if best is None or ncc > best[0]:
            best = (ncc, runner, x, y, pno)
        if ncc >= MIN_NCC and (ncc - runner) >= MIN_MARGIN:
            break  # unambiguous: no reason to sweep the rest of the document

    if best is None:
        return None

    ncc, runner, x, y, pno = best
    if full_page:
        pr = doc[pno].rect
        rect_pt = (pr.x0, pr.y0, pr.x1, pr.y1)
    else:
        s = PT_PER_INCH / CROP_DPI
        rect_pt = (x * s, y * s, (x + crop_px[0]) * s, (y + crop_px[1]) * s)
    return Relocation(
        page=pno,
        ncc=ncc,
        margin=ncc - runner,
        rect_pt=rect_pt,
        crop_px=crop_px,
        full_page=full_page,
    )


def native_image(doc, page: int, rect_pt) -> dict | None:
    """The embedded image behind a located rect, at its own resolution.

    The crop is a 160-dpi render of a placement; the PDF still holds whatever
    was placed. Where that placement is a single image covering the rect, its
    pixels are strictly better — measured 2.8x on median across the corpus.

    ``xres``/``yres`` are NOT usable here: they report a field stored in the
    image, 96 in every probe file, not the resolution at which it was placed.
    Effective dpi is the only honest figure, and it comes from the geometry.
    """
    rect = fitz.Rect(*rect_pt)
    area = abs(rect.get_area())
    if area <= 0:
        return None
    pg = doc[page]
    try:
        images = pg.get_images(full=True)
    except Exception:  # noqa: BLE001
        return None

    best: dict | None = None
    for img in images:
        xref = img[0]
        try:
            placements = pg.get_image_rects(xref)
        except Exception:  # noqa: BLE001
            continue
        for pl in placements:
            if pl.width <= 0 or pl.height <= 0:
                continue
            inter = pl & rect
            inter_area = abs(inter.get_area())
            if inter_area <= 0:
                continue
            of_rect = inter_area / area
            of_placement = inter_area / abs(pl.get_area())

            # Two ways an image is this figure's source, and requiring only
            # the first misses the common case. Either the placement covers
            # the rect (a panel cut out of a larger image), or the placement
            # sits INSIDE the rect because the crop box was drawn wide enough
            # to take in axis labels and margin that are page text, not
            # image. The second still needs a size floor, or a publisher logo
            # inside the rect would qualify.
            covers_rect = of_rect >= _NATIVE_COVER_FRAC
            inside_rect = (
                of_placement >= _NATIVE_COVER_FRAC and of_rect >= _NATIVE_MIN_RECT_FRAC
            )
            if not (covers_rect or inside_rect):
                continue

            try:
                info = doc.extract_image(xref)
            except Exception:  # noqa: BLE001 — an undecodable xref falls through
                continue
            nw, nh = int(info["width"]), int(info["height"])
            if nw <= 0 or nh <= 0:
                continue

            # Fractions are taken against the intersection, so a rect wider
            # than its placement clamps to the pixels that actually exist.
            fx0 = max(0.0, (inter.x0 - pl.x0) / pl.width)
            fy0 = max(0.0, (inter.y0 - pl.y0) / pl.height)
            fx1 = min(1.0, (inter.x1 - pl.x0) / pl.width)
            fy1 = min(1.0, (inter.y1 - pl.y0) / pl.height)
            sub = (fx0 * nw, fy0 * nh, fx1 * nw, fy1 * nh)
            if (sub[2] - sub[0]) <= 1 or (sub[3] - sub[1]) <= 1:
                continue

            cand = {
                "xref": xref,
                "native_px": (nw, nh),
                "sub_box_px": sub,
                "native_rect_pt": (inter.x0, inter.y0, inter.x1, inter.y1),
                "effective_dpi": nw / (pl.width / PT_PER_INCH),
                "rect_covered": of_rect,
                "ext": info.get("ext", ""),
            }
            if best is None or inter_area > best["_inter"]:
                cand["_inter"] = inter_area
                best = cand
    if best is not None:
        best.pop("_inter", None)
    return best


def _has_vector_art(page, rect) -> bool:
    """Whether any vector drawing falls inside a rect.

    Deliberately unfiltered: this answers "would re-rendering resolve more
    detail", which is true of any drawn content, not only of trace-shaped
    paths.
    """
    try:
        for d in page.get_drawings():
            r = d.get("rect")
            if r is not None and abs((r & rect).get_area()) > 0:
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _polylines(page, rect) -> list[np.ndarray]:
    """Point sequences from the page's vector art, clipped to a rect."""
    out: list[np.ndarray] = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return out
    for d in drawings:
        r = d.get("rect")
        if r is None or abs((r & rect).get_area()) <= 0:
            continue
        pts: list[tuple[float, float]] = []
        for item in d.get("items", []):
            kind = item[0]
            if kind == "l":
                pts.extend([(p.x, p.y) for p in item[1:3]])
            elif kind == "c":
                pts.extend([(p.x, p.y) for p in item[1:5]])
            # A rectangle or quad is frame or fill, never a trace — so its
            # corners are skipped. Aborting the whole path on one instead
            # loses the trace whenever a plot's frame and curve were emitted
            # as a single drawing, which is common.
        if len(pts) >= _CURVE_MIN_POINTS:
            out.append(np.asarray(pts, dtype=np.float64))
    return out


def vector_curves(doc, page: int, rect_pt) -> list[np.ndarray]:
    """Vector paths inside a rect that are shaped like a plotted trace.

    Only about 8% of papers put their spectra on the page this way — and the
    two that did in a 24-paper sample were both theses, not journal
    typesetting. So this is an opportunistic fast path, not the main one. Its
    real value is as ground truth: a curve recovered here has ZERO
    rasterisation loss, so rendering the same rect and re-extracting measures
    exactly what rasterisation costs.

    The shape test is what separates a trace from a glyph outline. Text
    rendered as paths produces long, dense, non-monotone segment runs over a
    few points of page — the reason a naive segment count reports two thirds
    of a corpus as vector when the true rate is 8%.
    """
    rect = fitz.Rect(*rect_pt)
    curves = []
    for pts in _polylines(doc[page], rect):
        xs, ys = pts[:, 0], pts[:, 1]
        x_extent = float(xs.max() - xs.min())
        y_extent = float(ys.max() - ys.min())
        if x_extent < _CURVE_MIN_X_EXTENT_PT or y_extent < _CURVE_MIN_Y_EXTENT_PT:
            continue
        dx = np.diff(xs)
        nz = dx[dx != 0]
        if nz.size == 0:
            continue
        monotone = max(float((nz > 0).mean()), float((nz < 0).mean()))
        if monotone < _CURVE_MIN_MONOTONE_FRAC:
            continue
        curves.append(pts)
    return curves


def choose_source(doc, reloc: Relocation) -> Source:
    """Pick the best available pixel source for a located figure.

    Order is vector, then native raster, then a page render — and the render
    tier splits by what backs it, because that determines whether a higher dpi
    buys information or only a finer grid.
    """
    rect = fitz.Rect(*reloc.rect_pt)
    curves = vector_curves(doc, reloc.page, reloc.rect_pt)
    if curves:
        return Source(
            tier="vector",
            page=reloc.page,
            rect_pt=reloc.rect_pt,
            limited_by="vector_source",
            effective_dpi=math.inf,
            resolution_gain_vs_crop=math.inf,
            n_vector_curves=len(curves),
            jpeg_round_trips=0,
        )

    native = native_image(doc, reloc.page, reloc.rect_pt)
    if native is not None:
        # Gain is a ratio of pixel DENSITIES, not of box widths: the crop rect
        # can be wider than the placement it contains, and a width ratio would
        # then report a resolution loss that is really just margin.
        return Source(
            tier="native_raster",
            page=reloc.page,
            rect_pt=reloc.rect_pt,
            limited_by="native_image_dpi",
            effective_dpi=native["effective_dpi"],
            resolution_gain_vs_crop=native["effective_dpi"] / CROP_DPI,
            xref=native["xref"],
            native_px=native["native_px"],
            sub_box_px=native["sub_box_px"],
            native_rect_pt=native["native_rect_pt"],
            rect_covered_by_native=native["rect_covered"],
            native_ext=native["ext"],
            # The crop is already one JPEG round trip; a native JPEG is the
            # same single encode, read without the render in between.
            jpeg_round_trips=1 if native["ext"] in ("jpeg", "jpg") else 0,
        )

    # No image covers the rect, so ask whether anything was DRAWN there.
    # `_polylines` is the wrong instrument for that question — it filters for
    # trace-shaped paths of 40+ points, so a plot assembled from short
    # segments would answer "no art" and be mislabelled as a resampled
    # placement, claiming a precision limit it does not have.
    backed_by_art = _has_vector_art(doc[reloc.page], rect)
    return Source(
        tier="render_vector" if backed_by_art else "render_resampled",
        page=reloc.page,
        rect_pt=reloc.rect_pt,
        limited_by="render_dpi" if backed_by_art else "native_image_dpi",
        effective_dpi=CROP_DPI,
        resolution_gain_vs_crop=1.0,
        jpeg_round_trips=1,
    )


def find_pdf(working_dir: str, key: str) -> str | None:
    """The source PDF for a paper key, if it is still on disk."""
    for sub in ("pdfs", "papers", "downloads", ""):
        cand = os.path.join(working_dir, sub, f"{key}.pdf") if sub else None
        if cand and os.path.isfile(cand):
            return cand
    import glob as _glob

    hits = _glob.glob(os.path.join(working_dir, "**", f"{key}.pdf"), recursive=True)
    return hits[0] if hits else None


def load_pixels(doc, src: Source, dpi: float | None = None):
    """The best available pixels for a sourced figure, as a PIL image.

    For a native raster this decodes the embedded image and crops to the
    figure's sub-box, which is the whole point of the tier: no render step
    sits between the original pixels and the reader. Every other tier renders
    the located rect, by default at the source's own effective dpi so the
    caller gets what the precision block promised.
    """
    from PIL import Image

    if src.tier == "native_raster" and src.xref is not None:
        try:
            info = doc.extract_image(src.xref)
            img = Image.open(io.BytesIO(info["image"])).convert("RGB")
            if src.sub_box_px:
                x0, y0, x1, y1 = (int(round(v)) for v in src.sub_box_px)
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(img.width, max(x1, x0 + 1)), min(
                    img.height, max(y1, y0 + 1)
                )
                img = img.crop((x0, y0, x1, y1))
            return img
        except Exception:  # noqa: BLE001 — fall through to a render
            pass

    use_dpi = dpi or (src.effective_dpi if math.isfinite(src.effective_dpi) else 600.0)
    use_dpi = max(72.0, min(float(use_dpi), 900.0))
    pm = doc[src.page].get_pixmap(dpi=int(use_dpi), clip=fitz.Rect(*src.rect_pt))
    return Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
