"""Reference alignment: wavelength calibration, not identification.

The distinction is the whole point. ONE offset estimated from a reference and
applied to EVERY peak leaves the spectrum's relative structure untouched and
stays falsifiable — a mismatched reference moves everything and the
disagreement shows. Snapping each peak to its nearest catalogue value would
manufacture agreement instead, which is what `interconnect` forbids.
"""

from __future__ import annotations

import pytest

from tools.figure_digitizer import peaks as PK
from tools.figure_digitizer.adapters import libs as LB


def _peaks(positions):
    return [
        PK.Peak(
            position=p,
            relative_intensity=1.0,
            prominence=1.0,
            fwhm=None,
            position_uncertainty=0.0,
            index=i,
        )
        for i, p in enumerate(positions)
    ]


def test_a_real_offset_is_corrected():
    found = _peaks([391.0, 500.0, 600.0])  # everything 2.366 nm low
    al = PK.align_to_reference(found, [393.366], resolvable=1.87, source="fallback")
    assert al.applied
    assert al.shift == pytest.approx(2.366, abs=1e-6)
    moved = PK.apply_alignment(found, al)
    assert [p.position for p in moved] == pytest.approx([393.366, 502.366, 602.366])


def test_every_peak_moves_by_the_same_amount():
    """Relative structure must survive: this is calibration, not snapping."""
    found = _peaks([391.0, 500.0, 600.0])
    al = PK.align_to_reference(found, [393.366], resolvable=1.87)
    moved = PK.apply_alignment(found, al)
    before = [
        b - a
        for a, b in zip(
            [p.position for p in found][:-1], [p.position for p in found][1:]
        )
    ]
    after = [
        b - a
        for a, b in zip(
            [p.position for p in moved][:-1], [p.position for p in moved][1:]
        )
    ]
    assert before == pytest.approx(after)


def test_a_well_calibrated_axis_is_left_alone():
    """CAUGHT LIVE. The offset estimated from one reference carries sd ~0.64 px
    of its own, so correcting a bias that is already near zero injects more
    noise than it removes — median position error went 0.183 nm to 0.343
    before this dead-band existed."""
    found = _peaks([393.30, 500.0, 600.0])  # 0.066 nm out, far inside the noise
    al = PK.align_to_reference(found, [393.366], resolvable=1.87)
    assert not al.applied
    assert "below" in al.reason


def test_an_implausibly_large_shift_is_refused():
    # Inside the 3-width search window (5.6 nm) but implying a shift beyond
    # the 2-width cap (3.74 nm). Further out and it is simply not a candidate.
    found = _peaks([388.8, 500.0])
    al = PK.align_to_reference(found, [393.366], resolvable=1.87)
    assert not al.applied
    assert "exceeds" in al.reason


def test_no_candidate_near_the_reference_is_refused():
    al = PK.align_to_reference(_peaks([500.0, 600.0]), [393.366], resolvable=1.87)
    assert not al.applied
    assert al.reason == "no peak near any reference"


def test_references_that_disagree_are_refused():
    """Two independent references must tell the same story. When they do not,
    at least one matched a neighbouring line rather than the line it names —
    which is exactly how a large axis error fails."""
    # Both references find a candidate inside their window, but no single
    # linear map explains both: the implied stretch is far from unity.
    found = _peaks([391.0, 592.0])
    al = PK.align_to_reference(found, [393.366, 589.0], resolvable=1.87)
    assert not al.applied
    # Caught by the SCALE bound rather than by comparing raw shifts. Comparing
    # shifts before fitting was tried and removed: a genuine scale error IS
    # different shifts at different references, so that test rejected exactly
    # the case the joint fit exists to handle.
    assert "scale" in al.reason


def test_references_closer_than_the_search_window_are_not_independent():
    """CAUGHT LIVE. Ca II H and K are 3.481 nm apart, inside the search window
    at survey resolution, so both lock onto the SAME peak and then appear to
    disagree by exactly their own separation. Every cell refused with
    'references disagree by 3.4810' until the near pair was pruned."""
    found = _peaks([391.0, 500.0])
    al = PK.align_to_reference(
        found, list(LB.FALLBACK_REFERENCES_NM), resolvable=1.87, source="fallback"
    )
    assert al.applied, al.reason
    assert "single reference" in al.reason


def test_a_refused_alignment_moves_nothing():
    found = _peaks([500.0, 600.0])
    al = PK.align_to_reference(found, [393.366], resolvable=1.87)
    assert [p.position for p in PK.apply_alignment(found, al)] == [500.0, 600.0]


# ── The LIBS adapter ──────────────────────────────────────────────────


def test_printed_labels_are_preferred_over_the_fallback():
    refs, src = LB.position_references(["Ca II 393.37", "Fe I 404.6 nm"])
    assert src == "figure_label"
    assert refs == [393.37, 404.6]


def test_the_fallback_is_ca_ii():
    refs, src = LB.position_references(None)
    assert src == "fallback_ca_ii"
    assert refs[0] == pytest.approx(LB.CA_II_K_NM)


def test_implausible_label_numbers_are_ignored():
    """A figure number or a concentration is not a wavelength."""
    assert LB.references_from_labels(["Figure 3", "0.387 wt%", "2024"]) == []


def test_labels_parse_with_or_without_species_and_unit():
    got = LB.references_from_labels(["393.37", "Ca II 393.37 nm", "Fe I 404.6"])
    assert 393.37 in got and 404.6 in got


# ── Multi-reference: slope as well as offset ──────────────────────────


def test_two_references_correct_a_scale_error():
    """The error a single reference is blind to. An axis whose nm-per-pixel is
    wrong gets pinned correctly at the reference and drifts away from it; no
    amount of translating fixes that."""
    # truth 300 and 700; measured with a small stretch, inside the move cap
    found = _peaks([301.0, 702.0, 500.0])
    al = PK.align_to_reference(found, [300.0, 700.0], resolvable=1.87)
    assert al.applied
    assert al.scale != 1.0
    moved = {round(p.position, 3) for p in PK.apply_alignment(found, al)}
    assert 300.0 in moved and 700.0 in moved


def test_a_single_reference_reports_offset_only():
    found = _peaks([391.0, 600.0])
    al = PK.align_to_reference(found, [393.366], resolvable=1.87)
    assert al.applied
    assert al.scale == 1.0
    assert "offset only" in al.reason


def test_a_correct_axis_is_left_alone_with_many_references():
    """The dead-band is judged on how far the correction actually MOVES the
    spectrum, not on the offset term — a scale fit with a near-zero intercept
    still moves the ends a long way."""
    found = _peaks([300.02, 500.0, 699.98])
    al = PK.align_to_reference(found, [300.0, 700.0], resolvable=1.87)
    assert not al.applied
    assert "below" in al.reason


def test_an_absurd_fitted_scale_is_refused():
    """With exactly two references the fit is EXACT and its residual is
    identically zero, so the scale bound is the only guard between a
    mismatched pair and a confident wrong answer."""
    found = _peaks([300.0, 320.0])
    al = PK.align_to_reference(found, [300.0, 322.0], resolvable=1.87)
    assert not al.applied
    assert "scale" in al.reason


def test_three_references_that_do_not_fit_one_line_are_refused():
    """With three references the fit is over-determined, so a single bad match
    shows as a residual rather than passing silently."""
    found = _peaks([301.0, 495.0, 701.0])  # the middle one is 5 nm out
    al = PK.align_to_reference(found, [300.0, 500.0, 700.0], resolvable=1.87)
    assert not al.applied
    assert "residual" in al.reason


def test_the_alignment_records_how_many_references_supported_it():
    found = _peaks([301.0, 701.0])
    al = PK.align_to_reference(found, [300.0, 700.0], resolvable=1.87)
    d = al.as_dict("nm")
    assert d["n_references"] == 2
    assert "scale" in d and "max_residual" in d
