"""Curve extraction and peak picking.

The hard part is refusing what is drawn in the same ink as the trace. These
tests are weighted toward the two ways that goes wrong: furniture being
followed as if it were data, and a multi-series figure being flattened into a
spectrum that was never plotted.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image

from tools.figure_digitizer import axes as A
from tools.figure_digitizer import curve as C
from tools.figure_digitizer import peaks as PK
from tools.figure_digitizer import synth as S

_FIXTURE = os.path.join(os.path.dirname(__file__), "data", "libs_sme1_spot1.txt")


def _spectrum():
    """A REAL LIBS spectrum, not a model of one.

    Three synthetic fixtures were tried and each hit a case the corpus does
    not contain — a mathematically exact flat baseline that is genuinely
    ambiguous with a frame rule, and a lone ultra-steep Gaussian whose flanks
    fragment under antialiasing. Tuning the extractor to accommodate them
    regressed it on real figures, twice. So the fixture is an actual
    instrument reading: SME iron-oxide pellet #1, 200-800 nm, 900 samples.
    """
    with open(_FIXTURE, encoding="utf-8") as fh:
        body = [ln for ln in fh if not ln.startswith("#")]
    y = np.array([float(v) for v in "".join(body).split(",")], dtype=float)
    x = np.linspace(200.0, 800.0, len(y))
    return x, y


def _interior(tmp_path, spec, dpi=160, name="p"):
    x, y = _spectrum()
    pdf = str(tmp_path / f"{name}.pdf")
    png = str(tmp_path / f"{name}.png")
    gt = S.render(x, y, spec, pdf)
    S.rasterize(pdf, png, dpi=dpi)
    img = Image.open(png).convert("RGB")
    rgb = np.asarray(img)
    mask = A.ink_mask(np.asarray(img.convert("L"), dtype=float))
    xa, ya = A.find_axes(mask)
    left, right = xa.span_px[0] + 1, xa.span_px[1] - 1
    top, bottom = int(min(ya.span_px)) + 1, xa.position_px - 1
    return gt, mask[top:bottom, left:right], rgb[top:bottom, left:right]


def test_a_trace_is_extracted_and_spans_the_plot(tmp_path):
    _, ink, _ = _interior(tmp_path, S.PlotSpec())
    tr = C.extract_trace(ink)
    assert tr is not None
    assert tr.coverage > 0.95
    assert tr.stroke_px >= 1.0
    assert tr.n_points > 0.9 * ink.shape[1]


def test_a_frame_spine_is_not_returned_as_the_trace(tmp_path):
    """CAUGHT LIVE at 600 dpi. Line thickness scales with dpi, so a fixed 2 px
    interior inset that clears a spine at 160 dpi leaves it standing at 600 —
    where it spans the full width, wins the longest-component test, and the
    trace comes back dead flat along the top of the plot."""
    _, ink, _ = _interior(tmp_path, S.PlotSpec(), dpi=600, name="hi")
    tr = C.extract_trace(ink)
    assert tr is not None
    finite = tr.y_px[np.isfinite(tr.y_px)]
    assert finite.std() > 1.0, "a flat trace means a spine was followed"


def test_annotation_furniture_is_flagged(tmp_path):
    """The highest-risk failure. An arrow physically connects a label box to
    the curve, so the two become ONE component and no leftover remains to
    detect. Following the box tops gave F1 0.13 with four peaks recovered out
    of sixty, at an intensity error of 0.72 on a 0-1 scale."""
    for spec in (
        S.PlotSpec(annotations=4),
        S.PlotSpec(legend=True),
        S.PlotSpec(annotations=3, legend=True),
    ):
        _, ink, _ = _interior(tmp_path, spec, name="a")
        tr = C.extract_trace(ink)
        assert tr is not None
        assert "annotation_overlap" in tr.flags


def test_a_clean_figure_is_not_flagged(tmp_path):
    """Refusal must not swallow the ordinary case. A dense LIBS survey is
    genuinely solid ink through its peak clusters, which is why detecting
    furniture as a dense BLOB flagged every clean figure too."""
    for spec in (
        S.PlotSpec(),
        S.PlotSpec(gridlines=True),
        S.PlotSpec(stroke_pt=0.5),
        S.PlotSpec(x_range=(390.0, 410.0)),
    ):
        _, ink, _ = _interior(tmp_path, spec, name="c")
        tr = C.extract_trace(ink)
        assert tr is not None
        assert tr.flags == [], f"false annotation flag on {spec}"


def test_multi_series_is_detected_never_flattened(tmp_path):
    """Reading a multi-trace calibration figure as one trace would invent a
    spectrum that was never plotted."""
    for n in (2, 3, 4):
        _, ink, rgb = _interior(tmp_path, S.PlotSpec(n_series=n), name=f"s{n}")
        assert C.count_series(rgb, ink) > 1


def test_a_single_trace_is_one_series(tmp_path):
    for spec in (S.PlotSpec(), S.PlotSpec(gridlines=True), S.PlotSpec(legend=True)):
        _, ink, rgb = _interior(tmp_path, spec, name="one")
        assert C.count_series(rgb, ink) == 1


def test_the_column_centre_carries_the_sample(tmp_path):
    """Column k spans [k, k+1), so its sample sits at k+0.5. Mapping the left
    edge puts every extracted position out by exactly half a pixel."""
    _, ink, _ = _interior(tmp_path, S.PlotSpec())
    tr = C.extract_trace(ink)
    assert tr.x_px[0] == pytest.approx(0.5)


def test_stroke_width_is_measured_where_the_trace_is_flat():
    """On a steep segment the vertical ink run is long because of SLOPE, not
    the pen. Averaging over all columns overestimates the stroke and biases
    every extracted value downward."""
    ink = np.zeros((60, 60), dtype=bool)
    for x in range(60):
        y = 10 if x < 30 else 10 + (x - 30)  # flat, then a 45-degree ramp
        ink[y : y + 2, x] = True
    top = np.array([float(np.nonzero(ink[:, x])[0].min()) for x in range(60)])
    assert C._stroke_width(ink, top) == pytest.approx(2.0)


def test_relative_intensity_asserts_no_continuum():
    """Phase 1 normalises; it does not subtract a baseline. Where y=0 sits is
    the axis calibration's business, and what the pedestal means is a physical
    modelling choice this layer must not make."""
    y = np.array([2.0, 5.0, 2.0, 8.0, 2.0])
    got = C.relative_intensity(y)
    assert got.min() == 0.0 and got.max() == 1.0
    # The pedestal is preserved as a level, not removed.
    assert got[0] == got[2] == got[4]


def test_peaks_are_found_with_positions_and_widths():
    x = np.linspace(200.0, 800.0, 2000)
    y = np.zeros_like(x)
    for c in (300.0, 500.0, 700.0):
        y += np.exp(-((x - c) ** 2) / (2 * 2.0**2))
    got = PK.pick(x, y)
    assert len(got) == 3
    for p, want in zip(got, (300.0, 500.0, 700.0)):
        assert p.position == pytest.approx(want, abs=0.5)
        assert p.fwhm is not None and p.fwhm > 0


def test_no_top_n_cap_is_applied():
    """A cap is right for a training record; this is an artifact, and a survey
    legitimately carries dozens of lines."""
    x = np.linspace(0.0, 100.0, 4000)
    y = np.zeros_like(x)
    for c in np.arange(2.0, 98.0, 2.0):
        y += np.exp(-((x - c) ** 2) / (2 * 0.15**2))
    assert len(PK.pick(x, y)) > 20


def test_a_flat_trace_yields_no_peaks():
    x = np.linspace(0.0, 10.0, 100)
    assert PK.pick(x, np.ones_like(x)) == []


def test_matching_is_greedy_on_distance_not_ordinal():
    """One spurious detection must not cascade into a chain of wrong pairs."""
    found = PK.pick(
        np.linspace(0.0, 10.0, 1000),
        np.exp(-((np.linspace(0.0, 10.0, 1000) - 5.0) ** 2) / 0.02),
    )
    pairs, uf, ut = PK.match(found, [5.0, 9.0], tolerance=0.3)
    assert len(pairs) == 1 and pairs[0][1] == 5.0
    assert ut == [9.0]


# ── Detection criterion ───────────────────────────────────────────────


def test_detection_is_independent_of_the_brightest_line():
    """THE reason the criterion changed. A range-relative prominence floor is
    hostage to the largest feature on the page: adding one enormous line far
    away raises the bar everywhere and silently deletes small clean peaks
    that have excellent local signal-to-noise. The IUPAC/NIST convention is
    defined against LOCAL background for exactly this reason."""
    x = np.linspace(200.0, 800.0, 6000)
    small = np.zeros_like(x)
    for c in (300.0, 340.0, 380.0):
        small += 0.06 * np.exp(-((x - c) ** 2) / (2 * 0.6**2))

    before = PK.pick(x, small, criterion="snr", stroke_px=2.0)
    # Now add one line twenty times taller, far from the others.
    with_giant = small + 1.2 * np.exp(-((x - 700.0) ** 2) / (2 * 0.6**2))
    after = PK.pick(x, with_giant, criterion="snr", stroke_px=2.0)

    kept = sum(
        1
        for c in (300.0, 340.0, 380.0)
        if any(abs(p.position - c) < 2.0 for p in after)
    )
    assert kept == 3, "a distant bright line must not delete small local peaks"
    assert len(before) >= 3

    # The range-relative rule is what fails here, and that is why it is not
    # the default: the same three peaks fall under 5% of the new range.
    old = PK.pick(x, with_giant, criterion="prominence")
    old_kept = sum(
        1 for c in (300.0, 340.0, 380.0) if any(abs(p.position - c) < 2.0 for p in old)
    )
    assert old_kept < 3, "range-relative rule unexpectedly survived; test is stale"


def test_snr_is_the_default_criterion():
    x = np.linspace(0.0, 100.0, 2000)
    y = 0.02 * np.exp(-((x - 50.0) ** 2) / (2 * 0.5**2))
    assert PK.pick(x, y) == PK.pick(x, y, criterion="snr")


def test_an_unknown_criterion_is_refused():
    import pytest as _pytest

    # The input has to actually VARY, or pick() returns on zero span before
    # it ever reaches the criterion dispatch and the check is unreachable.
    x = np.linspace(0.0, 10.0, 200)
    y = np.exp(-((x - 5.0) ** 2) / 0.5)
    with _pytest.raises(ValueError):
        PK.pick(x, y, criterion="vibes")


def test_a_flat_trace_yields_no_peaks_under_snr():
    x = np.linspace(0.0, 10.0, 400)
    assert PK.pick(x, np.ones_like(x), criterion="snr") == []


def test_a_vacuous_sigma_estimate_must_not_pass_everything():
    """CAUGHT LIVE in the stroke sweep. A thick pen's topmost-ink envelope is
    smooth at the detrend scale, the rolling MAD collapses to ~0, and
    "10 sigma above noise" degenerates into "any local maximum": every
    stroke >= 2pt arm measured sigma == 0.00000 and its 10-sigma recovery
    EQUALLED its thresholdless retention. The quantisation floor is what
    keeps the criterion falsifiable on extracted traces."""
    extent_px = 300.0
    x = np.linspace(0.0, 100.0, 1200)
    # A smooth hump on a flat baseline, quantised to the pixel grid like
    # a real trace...
    y = np.exp(-((x - 80.0) ** 2) / (2 * 8.0**2))
    # ...plus, on the FLAT part, a bump that survives rounding (1.5 px) yet
    # sits well under the ~2.9 px that 10x the quantisation floor demands.
    y += 1.5 * np.exp(-((x - 30.0) ** 2) / (2 * 0.4**2)) / extent_px
    y = np.round(y * extent_px) / extent_px

    floor = PK.trace_noise_floor(extent_px)
    assert floor > 0
    honest = PK.pick(
        x, y, criterion="snr", snr_sigma=10.0, stroke_px=4.0, noise_floor=floor
    )
    assert all(
        abs(p.position - 30.0) > 2.0 for p in honest
    ), "a sub-quantisation bump passed 10 sigma; the floor is not applied"
    assert any(
        abs(p.position - 80.0) < 2.0 for p in honest
    ), "the floor must not swallow a genuinely tall peak"
    # The trap this guards against: with no floor the same wiggle sails
    # through, because sigma on a smooth envelope estimates to ~zero.
    vacuous = PK.pick(x, y, criterion="snr", snr_sigma=10.0, stroke_px=4.0)
    assert any(
        abs(p.position - 30.0) < 2.0 for p in vacuous
    ), "sigma no longer collapses on smooth envelopes; this test is stale"


def test_the_noise_floor_is_zero_for_unquantised_data():
    assert PK.trace_noise_floor(0.0) == 0.0
    assert PK.trace_noise_floor(-3.0) == 0.0
