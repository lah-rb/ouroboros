"""Axis detection and calibration.

This is the module carrying the error term that resolution does not bound: a
calibration off by one tick spacing yields a perfectly plausible spectrum in
the wrong place, and no amount of dpi catches it. So the tests here are
weighted toward the ways a WRONG answer can look right, and toward refusal
being available as an outcome.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from tools.figure_digitizer import axes as A
from tools.figure_digitizer import synth as S


def _spectrum(n=4000):
    x = np.linspace(200.0, 800.0, n)
    y = np.full_like(x, 2.0)
    rng = np.random.default_rng(7)
    for centre in (250, 310, 393, 397, 405, 589, 656):
        y += 40 * np.exp(-((x - centre) ** 2) / (2 * 0.8**2))
    y += rng.normal(0, 0.2, n)
    return x, np.clip(y, 0, None)


def _render_mask(tmp_path, spec, dpi=160, name="p"):
    x, y = _spectrum()
    pdf = str(tmp_path / f"{name}.pdf")
    png = str(tmp_path / f"{name}.png")
    gt = S.render(x, y, spec, pdf)
    S.rasterize(pdf, png, dpi=dpi)
    gray = np.asarray(Image.open(png).convert("L"), dtype=float)
    return gt, A.ink_mask(gray), dpi / 72.0


def test_the_axes_are_found_where_they_were_drawn(tmp_path):
    gt, mask, k = _render_mask(tmp_path, S.PlotSpec())
    xa, ya = A.find_axes(mask)
    assert xa is not None and ya is not None
    assert abs(xa.position_px - gt.interior_pt[3] * k) < 2.0
    assert abs(ya.position_px - gt.interior_pt[0] * k) < 3.0


def test_the_spectrums_own_baseline_is_not_mistaken_for_the_axis(tmp_path):
    """A flat baseline is a full-width dark run a few pixels above the x-axis.
    Span alone cannot tell them apart; only the tick test can."""
    gt, mask, k = _render_mask(tmp_path, S.PlotSpec())
    xa, _ = A.find_axes(mask)
    true_axis_row = gt.interior_pt[3] * k
    assert xa.position_px == pytest.approx(true_axis_row, abs=2.0)
    # The baseline sits well above the axis; picking it would show up as a
    # detection many pixels high.
    assert xa.position_px > true_axis_row - 5


def test_gridlines_do_not_win_over_the_axis(tmp_path):
    gt, mask, k = _render_mask(tmp_path, S.PlotSpec(gridlines=True))
    xa, ya = A.find_axes(mask)
    assert xa is not None and ya is not None
    assert abs(xa.position_px - gt.interior_pt[3] * k) < 2.0


def test_inward_ticks_are_found(tmp_path):
    """Journal figures commonly tick inward. A detector that only looks
    outward finds none, then rejects the real axis for having no ticks."""
    gt, mask, _ = _render_mask(tmp_path, S.PlotSpec(ticks="in"))
    xa, _ = A.find_axes(mask)
    assert xa is not None and xa.n_ticks >= 3


def test_minor_ticks_are_not_counted_as_major(tmp_path):
    """CAUGHT LIVE. Minor ticks are regular too, so the regularity filter keeps
    them; paired ordinally against the major labels they put the calibration
    out by 700 nm while reporting a 0.37 px residual."""
    gt, mask, _ = _render_mask(tmp_path, S.PlotSpec(ticks="both", minor_ticks=True))
    xa, _ = A.find_axes(mask)
    assert xa is not None
    assert xa.n_ticks == len(gt.x_ticks)


def test_ticks_are_found_at_high_dpi(tmp_path):
    """CAUGHT LIVE. Tick length scales with dpi — a 4pt tick is 9px at 160 and
    33px at 600 — so fixed pixel bounds found no ticks above ~300 dpi and then
    rejected every axis for having none."""
    _, mask, _ = _render_mask(tmp_path, S.PlotSpec(), dpi=600, name="hi")
    xa, ya = A.find_axes(mask)
    assert xa is not None and ya is not None
    assert xa.n_ticks >= 3


def test_a_wrong_grid_does_not_absorb_the_real_ticks():
    """CAUGHT LIVE. With a tolerance proportional to the step, a chain seeded
    on a frame corner drifted 21 px per interval and still fit inside 18% of a
    128 px step — swallowing the true 107 px grid into a bogus one."""
    true_ticks = [(159.2, 9), (266.4, 9), (373.6, 9), (480.9, 9), (588.1, 9)]
    corner = [(137.5, 9)]
    got = A._largest_regular_subset(corner + true_ticks, tol=2.0)
    assert [round(p, 1) for p, _ in got] == [159.2, 266.4, 373.6, 480.9, 588.1]


def test_irregular_marks_are_rejected():
    """The trace touching the axis makes short perpendicular runs that look
    like inward ticks one at a time."""
    marks = [(100.0, 5), (150.0, 5), (200.0, 5), (250.0, 5), (263.0, 5), (271.0, 5)]
    got = A._largest_regular_subset(marks, tol=2.0)
    assert [p for p, _ in got] == [100.0, 150.0, 200.0, 250.0]


def test_a_clean_calibration_is_accurate_and_accepted():
    px = [100.0, 200.0, 300.0, 400.0]
    vals = [200.0, 400.0, 600.0, 800.0]
    cal = A.calibrate(px, vals)
    assert cal.ok and cal.scale == "linear"
    assert cal.max_residual_px < 0.01
    assert float(cal.to_value(250.0)) == pytest.approx(500.0)


def test_a_nonlinear_axis_is_refused_not_guessed():
    """A figure whose ticks do not fit a line is a figure we do not
    understand."""
    px = [100.0, 200.0, 300.0, 400.0]
    vals = [200.0, 400.0, 900.0, 800.0]
    cal = A.calibrate(px, vals, allow_log=False)
    assert not cal.ok
    assert cal.max_residual_px > A.MAX_RESIDUAL_PX


def test_a_log_axis_is_detected():
    px = [100.0, 200.0, 300.0, 400.0]
    vals = [10.0, 100.0, 1000.0, 10000.0]
    cal = A.calibrate(px, vals)
    assert cal.scale == "log" and cal.ok
    assert float(cal.to_value(250.0)) == pytest.approx(316.23, rel=0.01)


def test_a_linear_axis_is_never_called_log():
    """Asymmetric on purpose: a false log call corrupts every value, a missed
    one is only a rejection."""
    px = [100.0, 200.0, 300.0, 400.0, 500.0]
    vals = [200.0, 400.0, 600.0, 800.0, 1000.0]
    assert A.calibrate(px, vals).scale == "linear"


def test_a_missing_interior_tick_does_not_shift_every_pair():
    """CAUGHT LIVE. Taking the first n of each is only right when the missing
    marks are at the END. A thick trace hid one interior tick, ordinal
    truncation shifted every pair, and the result was 99 nm wrong at a 0.43 px
    residual."""
    ticks = [100.0, 200.0, 400.0, 500.0]  # the 300 px tick was obscured
    values = [200.0, 300.0, 400.0, 500.0, 600.0]
    t, v, _margin = A.match_labels_to_ticks(ticks, values)
    cal = A.calibrate(t, v)
    assert cal.max_residual_px < 1.0


def test_an_ambiguous_alignment_is_refused():
    """CAUGHT LIVE, and the most dangerous shape there is: three collinear
    points always fit a line. A thick trace hid five of eight ticks, the best
    of six alignments won on noise, and the calibration came out 6,343 nm off
    while reporting a 0.28 px residual."""
    ticks = [100.0, 200.0, 300.0]
    values = [200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0, 900.0]
    t, v, margin = A.match_labels_to_ticks(ticks, values)
    cal = A.calibrate(t, v, alignment_margin=margin)
    assert cal.ambiguous and not cal.ok


def test_an_unambiguous_partial_match_is_still_accepted():
    """Refusal must not swallow the honest case: an evenly-spaced detection
    that matches one window far better than any other is usable."""
    ticks = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]
    values = [200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0]
    t, v, margin = A.match_labels_to_ticks(ticks, values)
    cal = A.calibrate(t, v, alignment_margin=margin)
    assert cal.n_points == 6 and cal.max_residual_px < 1.0


def test_calibration_of_a_rendered_figure_is_subpixel(tmp_path):
    """The end-to-end property, against exact truth."""
    gt, mask, k = _render_mask(tmp_path, S.PlotSpec())
    xa, _ = A.find_axes(mask)
    t, v, margin = A.match_labels_to_ticks(xa.ticks_px, [val for val, _ in gt.x_ticks])
    cal = A.calibrate(t, v, alignment_margin=margin)
    assert cal.ok
    unit_per_px = gt.nm_per_px(160)
    worst = max(abs(float(cal.to_value(px * k)) - val) for val, px in gt.x_ticks)
    assert worst < 1.5 * unit_per_px


def test_a_y_axis_is_not_calibrated_upside_down():
    """CAUGHT LIVE on all ten rendered y-axes at once. Page y grows DOWNWARD
    while the plotted quantity grows upward, so sorting both sequences
    ascending mirrors the axis. Evenly spaced ticks fit a straight line just
    as well reversed, so the wrong answer arrived with a 0.31 px residual and
    a clean bill of health — the count guard cannot see it either, because the
    counts match."""
    ticks_px = [50.0, 150.0, 250.0, 350.0]  # top of the plot to the bottom
    values = [1000.0, 2000.0, 3000.0, 4000.0]
    t, v, margin = A.match_labels_to_ticks(ticks_px, values, descending=True)
    cal = A.calibrate(t, v, alignment_margin=margin)
    assert cal.ok
    # The TOPMOST pixel must carry the LARGEST value.
    assert float(cal.to_value(50.0)) > float(cal.to_value(350.0))
    assert float(cal.to_value(50.0)) == pytest.approx(4000.0)


def test_a_rendered_y_axis_reads_the_right_way_up(tmp_path):
    gt, mask, k = _render_mask(tmp_path, S.PlotSpec())
    _, ya = A.find_axes(mask)
    t, v, margin = A.match_labels_to_ticks(
        ya.ticks_px, [val for val, _ in gt.y_ticks], descending=True
    )
    cal = A.calibrate(t, v, alignment_margin=margin)
    assert cal.ok
    worst = max(abs(float(cal.to_value(py * k)) - val) for val, py in gt.y_ticks)
    assert worst < 0.01 * abs(gt.y_domain[1] - gt.y_domain[0])


def test_a_blank_image_yields_no_axes():
    assert A.find_axes(np.zeros((50, 50), dtype=bool)) == (None, None)


def test_a_tick_centre_includes_the_half_pixel():
    """CAUGHT LIVE. A tick occupying columns a..b covers [a, b+1), so its
    centre is (a+b)/2 + 0.5. The trace's samples are already reported at
    column centres, so omitting the half here left the calibration's pixel
    origin half a pixel away from the trace's own — putting every extracted
    position ~0.5 px to the right. At 160 dpi correcting it took median
    position error from 0.611 px to 0.203."""
    mask = np.zeros((40, 60), dtype=bool)
    mask[20, 5:55] = True  # the axis line
    for c in (10, 20, 30, 40):  # single-column ticks below it
        mask[21:25, c] = True
    marks = A._marks_on_side(mask, 20, (5, 54), "x", +1, 2, 12, 2)
    assert [m[0] for m in marks] == [10.5, 20.5, 30.5, 40.5]


def test_a_multi_column_tick_centres_on_its_span():
    mask = np.zeros((40, 60), dtype=bool)
    mask[20, 5:55] = True
    mask[21:25, 10:13] = True  # a tick three columns wide: 10,11,12
    marks = A._marks_on_side(mask, 20, (5, 54), "x", +1, 2, 12, 2)
    # columns 10..12 cover [10, 13), centre 11.5
    assert marks[0][0] == pytest.approx(11.5)
