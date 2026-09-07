"""Relocating a crop, and knowing what its pixels can resolve.

The properties that matter are about HONESTY, not cleverness: a crop must
land on the page it actually came from, a tier must not claim a precision it
does not have, and a path that is text must never be mistaken for a plotted
curve — that last one is what made a naive segment count report two thirds of
the corpus as vector when the true rate is 8%.
"""

from __future__ import annotations

import io

import fitz  # pymupdf
import pytest
from PIL import Image

from tools.figure_digitizer import source as S


def _doc_with_plot(n_pages: int = 3, plot_page: int = 1) -> fitz.Document:
    """A document whose pages differ, with one drawn 'plot'."""
    doc = fitz.open()
    for pno in range(n_pages):
        page = doc.new_page(width=400, height=500)
        page.insert_text((50, 60), f"page {pno} " * 6, fontsize=11)
        if pno == plot_page:
            shape = page.new_shape()
            shape.draw_rect(fitz.Rect(80, 200, 320, 380))
            pts = [(80 + i * 2.4, 340 - (40 if i % 17 else 120)) for i in range(101)]
            shape.draw_polyline([fitz.Point(*p) for p in pts])
            shape.finish(width=1.0)
            shape.commit()
    return doc


def _crop_png(doc, pno: int, rect: fitz.Rect, tmp_path) -> str:
    """Render a page at crop dpi and cut out a known box, as the extractor does."""
    pm = doc[pno].get_pixmap(dpi=int(S.CROP_DPI))
    img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
    k = S.CROP_DPI / S.PT_PER_INCH
    box = (
        int(rect.x0 * k),
        int(rect.y0 * k),
        int(rect.x1 * k),
        int(rect.y1 * k),
    )
    out = tmp_path / "fig_00.png"
    img.crop(box).save(out)
    return str(out)


def test_a_crop_lands_on_the_page_it_came_from(tmp_path):
    doc = _doc_with_plot()
    rect = fitz.Rect(80, 200, 320, 380)
    crop = _crop_png(doc, 1, rect, tmp_path)

    reloc = S.relocate(crop, doc)
    assert reloc is not None and reloc.located
    assert reloc.page == 1
    assert reloc.ncc >= S.MIN_NCC
    for got, want in zip(reloc.rect_pt, (rect.x0, rect.y0, rect.x1, rect.y1)):
        assert abs(got - want) < 1.0
    doc.close()


def test_a_full_page_crop_finds_its_own_page_not_the_first_one(tmp_path):
    """CAUGHT LIVE. Every page of a uniform document matches a full-page
    crop's dimensions, so returning on the first dimension match bound the
    crop to whichever page happened to sort first — two different crops both
    claimed page 4. The shape says WHAT the crop is; only correlation says
    WHICH page."""
    doc = _doc_with_plot(n_pages=4, plot_page=2)
    page_rect = doc[2].rect
    crop = _crop_png(doc, 2, page_rect, tmp_path)

    reloc = S.relocate(crop, doc)
    assert reloc is not None
    assert reloc.full_page, "a page-sized crop must be flagged as the full-page leak"
    assert reloc.page == 2
    doc.close()


def test_a_glyph_shaped_path_is_not_a_plot_curve():
    """Journal PDFs draw text as vector outlines: dense, short, non-monotone.
    Counting segments calls them curves; only the shape test rejects them."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    shape = page.new_shape()
    # A tight, back-and-forth scribble in a 55x6 pt box — the measured shape
    # of a real glyph outline that a segment count reported as a curve.
    pts = [(100 + (i % 7) * 8, 300 + (i % 5) * 1.2) for i in range(120)]
    shape.draw_polyline([fitz.Point(*p) for p in pts])
    shape.finish(width=0.5)
    shape.commit()

    curves = S.vector_curves(doc, 0, (80, 280, 380, 340))
    assert curves == [], "a glyph outline must not be reported as a plot curve"
    doc.close()


def test_a_real_polyline_is_found_as_a_vector_curve():
    doc = _doc_with_plot()
    curves = S.vector_curves(doc, 1, (70, 190, 330, 390))
    assert curves, "a long monotone-x polyline is a plot curve"
    assert len(curves[0]) >= S._CURVE_MIN_POINTS
    doc.close()


def test_a_vector_backed_rect_is_not_called_a_resampled_placement(tmp_path):
    """CAUGHT LIVE. `_has_vector_art` answers 'would re-rendering resolve
    more detail'. Reusing the 40-point curve filter for that question labelled
    plots built from short segments as resampled placements, claiming a
    precision ceiling they do not have."""
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    shape = page.new_shape()
    for i in range(12):  # short segments only: no path reaches 40 points
        shape.draw_line(fitz.Point(100 + i * 10, 300), fitz.Point(110 + i * 10, 320))
    shape.finish(width=1.0)
    shape.commit()

    rect = fitz.Rect(90, 290, 240, 330)
    crop = _crop_png(doc, 0, rect, tmp_path)
    reloc = S.relocate(crop, doc)
    src = S.choose_source(doc, reloc)
    assert src.tier == "render_vector"
    assert src.limited_by == "render_dpi"
    doc.close()


def test_native_pixels_are_preferred_and_report_their_own_dpi(tmp_path):
    """An embedded image beats a render of it, and effective dpi comes from
    the geometry — never from the stored xres/yres, which report a field
    inside the file rather than the resolution it was placed at."""
    big = Image.new("RGB", (900, 600), "white")
    for x in range(0, 900, 9):  # structure, so correlation has something to bite
        for y in range(0, 600, 7):
            big.putpixel((x, y), (20, 20, 20))
    buf = io.BytesIO()
    big.save(buf, format="PNG")

    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    placement = fitz.Rect(100, 150, 300, 283)
    page.insert_image(placement, stream=buf.getvalue())

    crop = _crop_png(doc, 0, placement, tmp_path)
    reloc = S.relocate(crop, doc)
    assert reloc is not None and reloc.located

    src = S.choose_source(doc, reloc)
    assert src.tier == "native_raster"
    assert src.limited_by == "native_image_dpi"
    # 900 px across 200 pt is 324 dpi, comfortably above the 160-dpi crop.
    assert src.effective_dpi == pytest.approx(324, rel=0.02)
    assert src.resolution_gain_vs_crop > 2.0
    doc.close()


def test_an_image_smaller_than_the_crop_rect_still_counts_as_native(tmp_path):
    """CAUGHT LIVE. Crop boxes routinely take in axis labels and margin that
    are page text, so the placement sits INSIDE the rect and covers well under
    95% of it. Requiring the image to cover the rect discarded a real 300-dpi
    native source and fell back to a 160-dpi render."""
    img = Image.new("RGB", (600, 400), "white")
    for x in range(0, 600, 6):
        img.putpixel((x, x % 400), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    placement = fitz.Rect(120, 200, 270, 300)
    page.insert_image(placement, stream=buf.getvalue())
    page.insert_text((110, 320), "Wavelength (nm)", fontsize=8)

    rect = fitz.Rect(105, 190, 285, 330)  # wider than the image, as crops are
    crop = _crop_png(doc, 0, rect, tmp_path)
    reloc = S.relocate(crop, doc)
    src = S.choose_source(doc, reloc)

    assert src.tier == "native_raster"
    assert 0.0 < src.rect_covered_by_native < 0.95
    doc.close()


def test_a_tiny_logo_inside_a_rect_is_not_that_figures_native_source(tmp_path):
    """The containment rule needs a size floor, or any small image dropped
    inside a figure box would be accepted as its source."""
    logo = Image.new("RGB", (40, 40), "black")
    buf = io.BytesIO()
    logo.save(buf, format="PNG")

    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    shape = page.new_shape()
    shape.draw_rect(fitz.Rect(100, 200, 300, 360))
    shape.finish(width=1.0)
    shape.commit()
    page.insert_image(fitz.Rect(110, 210, 130, 230), stream=buf.getvalue())

    rect = fitz.Rect(100, 200, 300, 360)
    crop = _crop_png(doc, 0, rect, tmp_path)
    reloc = S.relocate(crop, doc)
    src = S.choose_source(doc, reloc)
    assert src.tier != "native_raster"
    doc.close()


def test_an_unreadable_crop_is_a_result_not_a_crash(tmp_path):
    bad = tmp_path / "not_an_image.png"
    bad.write_bytes(b"nonsense")
    doc = _doc_with_plot()
    assert S.relocate(str(bad), doc) is None
    doc.close()
