"""The synthetic instrument, and whether its truth is actually true.

This module exists to score later phases, so its own correctness cannot be
assumed — a renderer that quietly drew nothing would make every downstream
measurement look like an extractor bug. The tests here pin the mapping, the
adversarial arms, and one end-to-end property: a rendered plot must be
recoverable as a vector curve whose points land on the data that was drawn.
That last one is the mechanism Gate B runs on.
"""

from __future__ import annotations

import numpy as np
import pytest

from tools.figure_digitizer import source as S
from tools.figure_digitizer import synth


def _spectrum(n: int = 2000, peaks=((30.0, 5.0), (60.0, 9.0))):
    x = np.linspace(20.0, 100.0, n)
    y = np.full_like(x, 0.4)
    for centre, height in peaks:
        y += height * np.exp(-((x - centre) ** 2) / (2 * 0.25**2))
    return x, y


def test_the_axis_mapping_agrees_with_what_was_drawn(tmp_path):
    x, y = _spectrum()
    gt = synth.render(x, y, synth.PlotSpec(), str(tmp_path / "p.pdf"))
    left, top, right, bottom = gt.interior_pt

    assert float(gt.x_to_pt(gt.x_domain[0])) == pytest.approx(left)
    assert float(gt.x_to_pt(gt.x_domain[1])) == pytest.approx(right)
    # Page y grows downward, so the LOW data value maps to the HIGH page y.
    assert float(gt.y_to_pt(gt.y_domain[0])) == pytest.approx(bottom)
    assert float(gt.y_to_pt(gt.y_domain[1])) == pytest.approx(top)


def test_a_rendered_plot_is_recoverable_as_a_vector_curve(tmp_path):
    """Gate B's mechanism end to end: the polyline drawn from known data must
    come back out of the PDF, at the pixel positions the mapping predicts.
    If this fails, no rasterisation-loss number measured against it is real."""
    import fitz  # pymupdf

    x, y = _spectrum()
    pdf = str(tmp_path / "p.pdf")
    gt = synth.render(x, y, synth.PlotSpec(), pdf)

    doc = fitz.open(pdf)
    page_rect = doc[0].rect
    curves = S.vector_curves(doc, 0, (0, 0, page_rect.width, page_rect.height))
    assert curves, "the trace must be findable as a vector curve"

    got = max(curves, key=len)
    # The recovered curve spans the interior and follows the mapping.
    assert got[:, 0].min() == pytest.approx(gt.interior_pt[0], abs=2.0)
    assert got[:, 0].max() == pytest.approx(gt.interior_pt[2], abs=2.0)

    # The tallest drawn point must land where the mapping says it does.
    apex = int(np.argmax(y))
    want_x, want_y = float(gt.x_to_pt(x[apex])), float(gt.y_to_pt(y[apex]))
    d = np.hypot(got[:, 0] - want_x, got[:, 1] - want_y)
    assert d.min() < 1.0, "the drawn apex is not where the ground truth claims"
    doc.close()


def test_nothing_silently_renders_blank(tmp_path):
    """CAUGHT LIVE. pymupdf's finish() only closes a path group; commit() is
    what writes it. Without the commit the page came out with no axes and no
    trace at all, and every arm would have scored a perfect zero."""
    import fitz  # pymupdf

    x, y = _spectrum()
    pdf = str(tmp_path / "p.pdf")
    synth.render(x, y, synth.PlotSpec(gridlines=True), pdf)
    doc = fitz.open(pdf)
    drawings = doc[0].get_drawings()
    doc.close()
    assert len(drawings) >= 3, "axes, ticks and trace must all reach the page"


def test_tick_density_is_a_real_arm():
    """The naive 'first step above the raw spacing' rule returned the same 4
    ticks for every request from 5 to 9, so an arm meant to sweep tick density
    would have measured one setting repeatedly."""
    counts = {n: len(synth._nice_ticks(180.0, 961.0, n)) for n in (3, 5, 7)}
    assert len(set(counts.values())) > 1, f"tick count never varied: {counts}"


def test_the_exponent_multiplier_is_recorded(tmp_path):
    """A '1e4' printed over the axis is how libraries keep labels short.
    Reading the labels without it puts every intensity out by 10,000x."""
    x, y = _spectrum()
    gt = synth.render(
        x, y * 10000, synth.PlotSpec(y_exponent=True), str(tmp_path / "p.pdf")
    )
    assert gt.y_exponent == 4


def test_an_extending_axis_runs_past_its_last_tick(tmp_path):
    """When the axis does not terminate at the data range, its endpoints are
    not calibration anchors — the digitiser must refuse, not rescale."""
    x, y = _spectrum()
    gt = synth.render(x, y, synth.PlotSpec(axis_extends=True), str(tmp_path / "p.pdf"))
    last_tick_x = max(px for _, px in gt.x_ticks)
    assert gt.x_axis_end_pt > last_tick_x
    assert gt.x_axis_end_pt > gt.interior_pt[2]


def test_a_log_axis_maps_logarithmically(tmp_path):
    x, y = _spectrum()
    y = np.clip(y, 0.01, None) * 100
    gt = synth.render(x, y, synth.PlotSpec(y_scale="log"), str(tmp_path / "p.pdf"))
    lo, hi = gt.y_domain
    mid_geometric = float(np.sqrt(lo * hi))
    _, top, _, bottom = gt.interior_pt
    assert float(gt.y_to_pt(mid_geometric)) == pytest.approx(
        (top + bottom) / 2, abs=1.0
    )


def test_zooming_changes_what_the_figure_can_resolve(tmp_path):
    """The whole resolution argument in one assertion: a survey and a window
    of the same spectrum do not carry the same information per pixel."""
    x, y = _spectrum()
    survey = synth.render(x, y, synth.PlotSpec(), str(tmp_path / "a.pdf"))
    window = synth.render(
        x, y, synth.PlotSpec(x_range=(58.0, 62.0)), str(tmp_path / "b.pdf")
    )
    assert window.nm_per_px(160) < survey.nm_per_px(160) / 10


def test_a_two_column_file_loads_with_or_without_a_header(tmp_path):
    a = tmp_path / "with.csv"
    a.write_text("wavelength,intensity\n200.0,1.0\n200.5,2.0\n", encoding="utf-8")
    b = tmp_path / "without.csv"
    b.write_text("200.0,1.0\n200.5,2.0\n", encoding="utf-8")
    for p in (a, b):
        w, i = synth.load_two_column(str(p))
        assert list(w) == [200.0, 200.5]
        assert list(i) == [1.0, 2.0]


def test_rasterising_honours_dpi_and_jpeg(tmp_path):
    from PIL import Image

    x, y = _spectrum()
    pdf = str(tmp_path / "p.pdf")
    spec = synth.PlotSpec()
    synth.render(x, y, spec, pdf)

    lo = synth.rasterize(pdf, str(tmp_path / "lo.png"), dpi=80)
    hi = synth.rasterize(pdf, str(tmp_path / "hi.png"), dpi=320)
    assert Image.open(hi).width == pytest.approx(4 * Image.open(lo).width, abs=4)

    jpg = synth.rasterize(pdf, str(tmp_path / "j.png"), dpi=160, jpeg_quality=60)
    plain = synth.rasterize(pdf, str(tmp_path / "n.png"), dpi=160)
    a = np.asarray(Image.open(jpg).convert("L"), dtype=float)
    b = np.asarray(Image.open(plain).convert("L"), dtype=float)
    assert np.abs(a - b).mean() > 0.0, "the jpeg arm must actually degrade pixels"
