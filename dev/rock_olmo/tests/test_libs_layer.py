"""Guards for the LIBS Boltzmann layer.

Each test is a bug that occurred building it. The column-resolution and
air/vacuum tests matter most: both were WRONG FACTS THAT LOOKED RIGHT —
one silently dropped 42 of 92 elements from every LIBS record ever
emitted, the other would have labelled air wavelengths as vacuum.
"""

from __future__ import annotations

import pytest

import interconnect as ic
import libs_layer as L


def test_parse_j_handles_half_integers():
    """Odd-electron systems have half-integer J. float("3/2") raises, and
    skipping those would drop every line of every alkali and most
    transition metals — the elements LIBS is most used on."""
    assert L.parse_j("3") == 3.0
    assert L.parse_j("3/2") == 1.5
    assert L.parse_j("5/2") == 2.5
    assert L.parse_j("") is None
    assert L.parse_j(None) is None


def test_num_strips_catalogue_annotation():
    """NIST marks a theoretical level with brackets and an unknown
    additive constant with '+x'. A bracketed level is still the level."""
    assert L._num("[109610.2232]") == pytest.approx(109610.2232)
    assert L._num("72722.23") == pytest.approx(72722.23)
    assert L._num("1234+x") == pytest.approx(1234.0)
    assert L._num("") is None


def test_column_variants_both_resolve():
    """50 of the 92 ASD files head their wavelength columns `_vac` and 42
    head them `_air`. Resolving one fixed name returned NOTHING for the
    other 42 — including Ca, the element geological LIBS leans on hardest."""
    assert L._first({"obs_wl_air(nm)": "393.37"}, L._WL_OBS) == pytest.approx(393.37)
    assert L._first({"obs_wl_vac(nm)": "288.16"}, L._WL_OBS) == pytest.approx(288.16)
    assert L._first({"nothing": "1"}, L._WL_OBS) is None


@pytest.mark.parametrize(
    "element,stage,expected",
    [
        ("Si", 1, 288.16),
        ("Mg", 1, 285.21),
        ("Na", 1, 588.995),
        ("Ca", 2, 393.37),
        ("K", 1, 766.49),
        ("O", 1, 777.19),
        ("H", 1, 656.28),
        ("Li", 1, 670.79),
        ("Al", 1, 396.15),
    ],
)
def test_known_strong_lines_surface(element, stage, expected):
    """The weighting is checkable: these are lines any LIBS practitioner
    would name, and a correct Boltzmann ranking surfaces them unprompted.

    Ca II is here because it only appears once `_air` columns resolve;
    H because hydrogen has no sp_num column and needs the H-II-cannot-emit
    argument; Li because its usable lines are Ritz-only."""
    lines = L.boltzmann_intensities(element, stage=stage, top=8)
    if not lines:
        pytest.skip(f"NIST ASD not present for {element}")
    wls = [l["wavelength_nm_air"] for l in lines]
    assert (
        min(abs(w - expected) for w in wls) < 0.5
    ), f"{element} {stage}: expected ~{expected}, got {wls}"


def test_hydrogen_is_treated_as_neutral():
    """Hydrogen's sp_num column is empty for all 190 rows. Every hydrogen
    line is necessarily H I — H II is a bare proton with no bound electron
    and cannot emit line radiation. Physics, not an assumption."""
    lines = L.load_asd_full("H")
    if not lines:
        pytest.skip("NIST ASD not present")
    assert all(l["stage"] == 1 for l in lines)


def test_intensities_are_normalised_within_element():
    lines = L.boltzmann_intensities("Si", stage=1, top=5)
    if not lines:
        pytest.skip("NIST ASD not present")
    assert lines[0]["relative_intensity"] == pytest.approx(100.0)
    assert all(l["relative_intensity"] <= 100.0 for l in lines)


def test_higher_levels_gain_with_temperature():
    """The physical content of the temperature view: raising T raises the
    Boltzmann population of higher upper levels, so lines from them gain
    relative strength. If this inverts, the sign of the exponent is wrong."""
    cool = {
        l["wavelength_nm_air"]: l for l in L.boltzmann_intensities("Ca", 8000.0, top=8)
    }
    hot = {
        l["wavelength_nm_air"]: l for l in L.boltzmann_intensities("Ca", 12000.0, top=8)
    }
    if not cool or not hot:
        pytest.skip("NIST ASD not present")
    shared = set(cool) & set(hot)
    ref = max(shared, key=lambda w: cool[w]["relative_intensity"])
    higher = [w for w in shared if cool[w]["e_k_cm"] > cool[ref]["e_k_cm"]]
    if not higher:
        pytest.skip("no higher-level line shared between the two temperatures")
    assert any(
        hot[w]["relative_intensity"] > cool[w]["relative_intensity"] for w in higher
    )


# ── the views ────────────────────────────────────────────────────────


def _groups():
    return [
        {
            "element": "Si",
            "stage": 1,
            "stage_label": "Si I",
            "lines": [
                {
                    "wavelength_nm_air": 288.16,
                    "relative_intensity": 100.0,
                    "wavelength_basis": "observed",
                }
            ],
        },
        {
            "element": "O",
            "stage": 1,
            "stage_label": "O I",
            "lines": [
                {
                    "wavelength_nm_air": 777.19,
                    "relative_intensity": 100.0,
                    "wavelength_basis": "observed",
                }
            ],
        },
    ]


def test_libs_view_refuses_to_rank_across_elements():
    """Relative intensity is only defined within one species. Ranking Si
    against Fe needs partition functions and the Saha ionisation balance,
    which we do not have — so the text must say so rather than let a
    reader infer a cross-element comparison from the numbers."""
    v = ic.view_libs_predicted("Quartz", "SiO2", _groups(), 10000.0)
    assert "WITHIN each element only" in v["text"]
    assert "partition function" in v["text"]


def test_libs_view_states_the_temperature_and_the_air_convention():
    v = ic.view_libs_predicted("Quartz", "SiO2", _groups(), 10000.0)
    assert "10,000 K" in v["text"]
    assert "air" in v["text"].lower()
    assert v["provenance"]["derivation"] == "boltzmann_lte"


def test_ritz_lines_are_flagged_not_silently_mixed():
    g = _groups()
    g[0]["lines"][0]["wavelength_basis"] = "ritz"
    v = ic.view_libs_predicted("Quartz", "SiO2", g, 10000.0)
    assert "*" in v["text"] and "Ritz" in v["text"]


def test_temperature_view_suppressed_when_nothing_moves():
    """A record claiming temperature changes the spectrum, showing two
    identical lists, teaches the opposite of the intended lesson."""
    g = _groups()
    assert ic.view_libs_temperature("Quartz", "SiO2", g, g, 8000.0, 12000.0) == {}


def test_temperature_view_fires_when_intensities_move():
    cool, hot = _groups(), _groups()
    hot[0]["lines"][0]["relative_intensity"] = 42.0
    v = ic.view_libs_temperature("Quartz", "SiO2", cool, hot, 8000.0, 12000.0)
    assert v and "8,000 K" in v["text"] and "12,000 K" in v["text"]
    assert (
        "property of the measurement" in v["text"]
        or "without anything about the mineral changing" in v["text"]
    )
