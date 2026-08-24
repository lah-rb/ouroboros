"""Canonical training-form schema: units, ranges, and what must NOT move."""

import pytest

from training_form import canonicalize, convert, split_key


# ── key splitting ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "key,expected",
    [
        # qualifier INSIDE the unit — the 342-use xrd pair
        ("xrd_2theta_min_deg", ("xrd_2theta", "min", "deg")),
        # qualifier OUTSIDE the unit — the 310-use ftir pair
        ("ftir_spectral_range_cm-1_min", ("ftir_spectral_range", "min", "cm-1")),
        ("particle_size_nm_min", ("particle_size", "min", "nm")),
        ("laser_repetition_rate_mhz", ("laser_repetition_rate", "", "mhz")),
        ("composition_formula", ("composition_formula", "", "")),
    ],
)
def test_split_key_handles_both_qualifier_orders(key, expected):
    assert split_key(key) == expected


# ── unit conversion ──────────────────────────────────────────────────
def test_conversion_reaches_the_canonical_unit():
    assert convert(3.551, "mhz") == 3551000
    assert convert(2, "um") == 2000
    assert convert(2, "h") == 7200


def test_identity_units_pass_through_unconverted():
    """deg/percent/K have no sensible canonical target here."""
    assert convert(90, "deg") == 90
    assert convert(25, "c") == 25


def test_unit_variants_converge_without_losing_either_value():
    """A paper may state both; keeping only the last one loses a
    measured value, which is what the first implementation did."""
    out = canonicalize({"particle_size_nm": 44, "particle_size_um": 2})
    assert out["particle_size_nm"] == [44, 2000]
    packed = {p["key"] for p in out["particle_size_nm_as_packed"]}
    assert packed == {"particle_size_nm", "particle_size_um"}


def test_the_verbatim_packed_value_is_always_retained():
    """Grounding lives on the pack; the training form must never be the
    only copy of what the paper actually printed."""
    out = canonicalize({"laser_repetition_rate_mhz": 3.551})
    assert out["laser_repetition_rate_hz"] == 3551000
    assert out["laser_repetition_rate_hz_as_packed"][0]["value"] == 3.551


# ── range folding ────────────────────────────────────────────────────
def test_min_max_pair_folds_into_one_range_key():
    out = canonicalize(
        {"ftir_spectral_range_cm-1_min": 400, "ftir_spectral_range_cm-1_max": 4000}
    )
    assert out["ftir_scan_range_cm-1"] == {"min": 400, "max": 4000, "unit": "cm-1"}
    assert "ftir_spectral_range_cm-1_min" not in out


def test_scan_range_never_collides_with_peak_positions():
    """xrd_2theta_min/max_deg is the SCAN WINDOW; xrd_2theta_deg is a
    measured PEAK. Folding the pair onto the peak name would merge two
    different physical claims."""
    out = canonicalize(
        {
            "xrd_2theta_min_deg": 10,
            "xrd_2theta_max_deg": 90,
            "xrd_2theta_deg": [{"two_theta_deg": 24.5, "phase": "rutile"}],
        }
    )
    assert out["xrd_scan_range_2theta_deg"] == {"min": 10, "max": 90, "unit": "deg"}
    assert out["xrd_2theta_deg"] == [{"two_theta_deg": 24.5, "phase": "rutile"}]


# ── what must NOT be touched ─────────────────────────────────────────
def test_peak_tables_pass_through_intact():
    peaks = [{"peak_cm-1": 1082, "assignment": "CO3 stretch", "sample": "dawsonite"}]
    out = canonicalize({"ftir_peak_wavenumber_cm-1": peaks})
    assert out["ftir_peak_wavenumber_cm-1"] == peaks


def test_bespoke_singleton_keys_are_left_alone():
    """93% of the vocabulary is one-off domain specificity. Forcing it
    into a schema loses more than it gains."""
    k = "olivine_required_billion_tonnes_per_year_sequester_all_anthropogenic_co2"
    out = canonicalize({k: 4.2, "composition_formula": "Mg2SiO4"})
    assert out[k] == 4.2
    assert out["composition_formula"] == "Mg2SiO4"


def test_empty_and_non_numeric_survive():
    assert canonicalize({}) == {}
    out = canonicalize({"synthesis_method": "solvothermal", "sample_count": 12})
    assert out["synthesis_method"] == "solvothermal"


def test_a_mean_only_key_keeps_its_value():
    """Regression: reading only the bare slot emitted an empty list for
    any key whose sole qualifier was _mean, destroying the measurement
    (17 artifacts, found by a leaf-count audit over the corpus)."""
    rows = [{"thickness_mm": 0.5, "mean_mpa": 316.8}]
    out = canonicalize({"biaxial_flexural_strength_mean_mpa": rows})
    assert out["biaxial_flexural_strength_mpa_avg"] == rows


def test_mean_and_range_coexist_without_either_being_lost():
    out = canonicalize(
        {
            "particle_size_nm_min": 10,
            "particle_size_nm_max": 90,
            "particle_size_nm_mean": 44,
        }
    )
    assert out["particle_size_nm"] == {"min": 10, "max": 90, "avg": 44, "unit": "nm"}


def test_a_bare_value_never_clobbers_its_own_folded_range():
    """Regression: with no rename to separate them, writing the bare
    value under the range's key destroyed min AND max."""
    out = canonicalize(
        {
            "tube_wall_thickness_um_min": 515,
            "tube_wall_thickness_um_max": 550,
            "tube_wall_thickness_mm": 0.7,
        }
    )
    got = out["tube_wall_thickness_nm"]
    assert got["min"] == 515000 and got["max"] == 550000
    assert got["value"] == 700000


def test_unconvertible_values_keep_their_original_unit_label():
    """Regression: "0.5 x 0.5" (mm) was re-labelled _nm without being
    converted — a unit claim the value does not support."""
    out = canonicalize({"beam_profile_mm": "0.5 x 0.5"})
    assert "beam_profile_mm" in out
    assert "beam_profile_nm" not in out
