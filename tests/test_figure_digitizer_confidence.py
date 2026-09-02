"""The banked detection curve must reproduce what it was fitted on.

A frozen model is a claim about a dataset. These tests hold it to the
recorded held-out behaviour on a shipped 2,000-row stratified sample, and pin
the properties a consumer relies on: monotone in separation and intensity,
the closed-form d50 agrees with the curve, and triage refuses what the span
sweep showed to be hopeless.
"""

from __future__ import annotations

import json
import math
import os

import numpy as np

from tools.figure_digitizer import confidence as CF

FIX = os.path.join(os.path.dirname(__file__), "data", "figdig_detect_fixture.json")


def _rows():
    return json.load(open(FIX))


def test_the_fixture_reproduces_the_recorded_discrimination_and_calibration():
    """A frozen model is a claim about a dataset. On the shipped 2,000-row
    stratified sample the model must discriminate (held-out AUC was 0.71-0.88
    by span, 0.83 pooled) and be calibrated (within 0.03 per decile on the
    full set). A cell-level R2 is NOT tested here on purpose: the fixture has
    a median of 5 rows per cell, so cell means are sampling noise -- a first
    version of this test failed at R2 0.45 for exactly that reason."""
    from sklearn.metrics import roc_auc_score

    rows = [
        r for r in _rows() if r["sep"] and math.isfinite(r["sep"]) and r["inten"] > 0
    ]
    p = np.array([CF.p_detect(r["sep"], r["inten"], r["grid"], r["pen"]) for r in rows])
    y = np.array([int(r["rec"]) for r in rows])
    assert roc_auc_score(y, p) > 0.80
    for lo, hi in ((0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 1.01)):
        sel = (p >= lo) & (p < hi)
        assert sel.sum() > 50
        assert abs(p[sel].mean() - y[sel].mean()) < 0.08, (lo, hi)


def test_calibration_holds_at_the_extremes():
    rows = [
        r for r in _rows() if r["sep"] and math.isfinite(r["sep"]) and r["inten"] > 0
    ]
    p = np.array([CF.p_detect(r["sep"], r["inten"], r["grid"], r["pen"]) for r in rows])
    y = np.array([int(r["rec"]) for r in rows])
    lo, hi = p < 0.1, p > 0.7
    assert y[lo].mean() < 0.15, "confident 'lost' peaks are being found"
    assert y[hi].mean() > 0.65, "confident 'found' peaks are being lost"


def test_monotone_in_separation_and_intensity():
    g, pen = 0.1, 0.3
    ps = [CF.p_detect(s, 0.05, g, pen) for s in (0.1, 0.3, 1.0, 3.0)]
    assert ps == sorted(ps)
    pi = [CF.p_detect(0.5, i, g, pen) for i in (0.01, 0.03, 0.1, 0.5)]
    assert pi == sorted(pi)


def test_d50_is_where_the_curve_crosses_one_half():
    for g, pen, i in ((0.1, 0.3, 0.05), (0.03, 0.06, 0.02), (0.8, 4.0, 0.1)):
        d = CF.d50(g, pen, i)
        assert abs(CF.p_detect(d, i, g, pen) - 0.5) < 1e-6


def test_d50_matches_the_recorded_closed_form():
    """At median intensity d50_px ~ 5.2 + 0.62 * stroke_px (frozen model; the
    dev doc's earlier 5.3 + 0.48 came from a collinear 6-term fit with the
    same held-out performance and a different extrapolated summary)."""
    for stroke_px, want in ((2, 6.4), (3, 7.3), (5, 8.7), (8, 10.1)):
        g = 0.1
        got = CF.d50(g, stroke_px * g) / g
        assert abs(got - want) < 0.3, f"stroke {stroke_px}px: d50 {got:.1f}px vs {want}"


def test_pen_alone_is_not_the_limit():
    """The bench's central finding: grid outranks pen. Halving the grid at a
    fixed pen (in data units) must raise recovery."""
    seps = [0.3, 0.5, 0.8]
    coarse = CF.expected_recovery(seps, None, grid=0.2, pen=0.6)
    fine = CF.expected_recovery(seps, None, grid=0.1, pen=0.6)
    assert fine > coarse


def test_triage_discriminates_and_refuses_to_guess_without_a_basis():
    """CAUGHT: a first version scaled the nominal separation to 3 x d50 and
    returned the SAME 0.78 recovery for a zoom, a 100 nm window and a survey
    -- a verdict that could not fail. Geometry-only triage must use an
    absolute prior from the adapter, and with no prior and no line list it
    must say so."""
    from tools.figure_digitizer.adapters.libs import triage_libs

    zoom = triage_libs(0.0268, 0.086)  # 20 nm @160dpi: sweep ~0.9
    mid = triage_libs(0.1339, 0.429)  # 100 nm: sweep ~0.5
    survey = triage_libs(0.8036, 2.571)  # 600 nm: sweep <0.15
    recs = [
        zoom["expected_recovery"],
        mid["expected_recovery"],
        survey["expected_recovery"],
    ]
    assert recs[0] > recs[1] > recs[2], f"triage is not discriminating: {recs}"
    assert zoom["verdict"] == "digitize"
    assert survey["verdict"] == "refuse"
    assert mid["verdict"] != "digitize"
    assert zoom["basis"] == "geometry_only" and zoom["model"]["n"] == CF.FIT_N
    bare = CF.triage(0.8036, 2.571)
    assert bare["verdict"] == "no_basis" and bare["expected_recovery"] is None


def test_a_line_list_beats_geometry():
    """With the figure's own peaks the estimate uses them, and a sparse list
    on a coarse figure can still be digitisable."""
    sparse = [5.0, 6.0, 4.0]  # nm apart on a survey
    t = CF.triage(0.8036, 2.571, seps=sparse, intensities=[0.2, 0.3, 0.2])
    assert (
        t["expected_recovery"]
        > CF.triage(0.8036, 2.571, nominal_sep=0.327)["expected_recovery"]
    )
    assert t["basis"] == "line_list" and t["expected_recovery"] > 0.6


def test_degenerate_inputs_do_not_raise():
    assert CF.p_detect(0.0, 0.05, 0.1, 0.3) == 0.0
    assert CF.p_detect(0.5, 0.05, 0.0, 0.3) == 0.0
    assert CF.d50(0.0, 0.3) == math.inf
    assert CF.expected_recovery([], None, 0.1, 0.3) == 0.0
    assert CF.neighbour_separations([5.0]) == [math.inf]
    assert CF.neighbour_separations([1.0, 1.5, 3.0]) == [0.5, 0.5, 1.5]
