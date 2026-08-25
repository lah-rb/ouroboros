"""Guards for the structural layer.

Every test here is a bug that actually occurred while building it. The
two that matter most are the space-group provenance test and the WURM
join test: both protect against a WRONG FACT THAT LOOKS RIGHT, which is
the failure mode this corpus cannot tolerate — a grounded-looking
sentence asserting something no source says.
"""

from __future__ import annotations

import pytest

import interconnect as ic
import reference_layer as rl
import wurm_layer as wl

# --------------------------------------------------------------------------
# mindat: the field that is not what it looks like
# --------------------------------------------------------------------------


def test_mindat_spacegroup_is_never_exposed_as_an_ita_number():
    """mindat's `spacegroup` column is a mindat-internal id, NOT the
    International Tables number — Quartz is ITA 152 and mindat says 89.
    The loader must not surface it under a name any view could mistake
    for the real thing, and no view may print it."""
    rec = rl.load_mindat_structure.__doc__
    assert rec is not None
    st = {
        "crystal_system": "Trigonal",
        "mindat_spacegroup_id": 89,
        "a": 4.9133,
        "c": 5.4053,
        "strunz_class": "oxide",
    }
    v = ic.view_structure("Quartz", "SiO2", st, None)
    assert (
        "89" not in v["text"]
    ), "mindat's internal id leaked into prose as a space group"
    assert (
        "space group" not in v["text"]
    ), "no space group may be claimed when only mindat metadata is available"


def test_view_structure_names_the_space_group_only_from_the_cif():
    st = {"crystal_system": "Tetragonal"}
    v = ic.view_structure("Rutile", "TiO2", st, {"space_group_symbol": "P4_2/mnm"})
    assert "space group P4_2/mnm" in v["text"]


def test_absent_cell_edges_are_omitted_not_invented():
    """mindat omits b for tetragonal and both b and c for cubic rather
    than repeating a. A placeholder would assert a measurement nobody
    made."""
    v = ic.view_structure(
        "Anatase",
        "TiO2",
        {"crystal_system": "Tetragonal", "a": 3.7845, "c": 9.5143},
        None,
    )
    assert "a = 3.784" in v["text"] and "c = 9.514" in v["text"]
    assert "b =" not in v["text"]


def test_non_integer_coordination_is_labelled_as_a_mean():
    """Coordination numbers are integers PER SITE. A cation in two
    distinct environments averages to e.g. 6.75, and printing "Ca in
    6.75-fold coordination" states something that does not exist."""
    cf = {"coordination": {"Ca": 6.75, "Si": 4.0}, "space_group_symbol": "P-1"}
    v = ic.view_structure(
        "Anorthite", "CaAl2Si2O8", {"crystal_system": "Triclinic"}, cf
    )
    assert "6.75-fold coordination on average across sites" in v["text"]
    assert "Si in 4-fold coordination" in v["text"]


# --------------------------------------------------------------------------
# polymorph view: the contrast must be real
# --------------------------------------------------------------------------


def _member(name, sysname, sg, peaks=((100.0,), (200.0,))):
    return {
        "species": name,
        "peaks": [{"position_cm-1": p[0]} for p in peaks],
        "structure": {"crystal_system": sysname},
        "cif": {"space_group_symbol": sg},
    }


def test_polymorph_view_requires_structures_that_actually_differ():
    """Two entries of one mineral under variant names would otherwise be
    presented as a contrast, teaching a distinction that does not exist."""
    same = [
        _member("Foo", "Tetragonal", "P4_2/mnm"),
        _member("Foo variant", "Tetragonal", "P4_2/mnm"),
    ]
    assert ic.view_polymorph("TiO2", same) == {}


def test_polymorph_view_fires_on_a_genuine_pair_and_names_the_reason():
    pair = [
        _member("Anatase", "Tetragonal", "I4_1/amd"),
        _member("Rutile", "Tetragonal", "P4_2/mnm"),
    ]
    v = ic.view_polymorph("TiO2", pair)
    assert v["view"] == "polymorph"
    assert "I4_1/amd" in v["text"] and "P4_2/mnm" in v["text"]
    assert "no chemical analysis can separate them" in v["text"]


def test_polymorph_view_is_silent_without_structure():
    bare = [
        {"species": "A", "peaks": [{"position_cm-1": 100.0}]},
        {"species": "B", "peaks": [{"position_cm-1": 200.0}]},
    ]
    assert ic.view_polymorph("X", bare) == {}


# --------------------------------------------------------------------------
# WURM: the join is the dangerous part
# --------------------------------------------------------------------------


def test_clean_strips_cdata_and_double_escaping():
    """Formulas arrive double-escaped and CDATA-wrapped; untreated they
    emitted "Reidite (<![CDATA[ZrSiO4]]>)"."""
    assert wl._clean("<![CDATA[ZrSiO4]]>") == "ZrSiO4"
    assert wl._clean("Ca&amp;lt;sub&amp;gt;2&amp;lt;/sub&amp;gt;MgSi") == "Ca2MgSi"


def test_intensity_regex_handles_negative_exponents():
    """The first pattern excluded '-' from the exponent, so 3.555E-5
    matched as '3.555E' and float() raised, killing the parse."""
    inten, _ = wl.parse_html_intensities(
        "no_itot_rel['0'] = 3.55504181746E-5; no_itot_rel['1'] = 100.0;"
    )
    assert inten == {0: pytest.approx(3.55504181746e-5), 1: pytest.approx(100.0)}


def test_tbd_is_not_treated_as_a_symmetry_label():
    """WURM writes TBD where a mode's irrep is unassigned. Taken at face
    value it teaches 'TBD' as an irreducible representation."""
    xml = (
        "<HEADER><name>TESTITE</name><formula>AB</formula></HEADER>"
        "<mode><mode_no>1</mode_no><char>TBD</char><freqTO>404.0</freqTO></mode>"
    )
    rec = wl.parse_xml(xml)
    assert rec["species"] == "Testite"
    assert rec["modes"][0]["freq_cm-1"] == 404.0


def test_computed_view_collapses_degenerate_modes():
    """E and T modes appear once per component at the SAME frequency and
    label; listing them twice reads as two bands where there is one."""
    rec = {
        "species": "Zircon",
        "formula": "ZrSiO4",
        "space_group_symbol": "I4_1/amd",
        "bands": [
            {"position_cm-1": 364.0, "irrep": "Eg", "intensity_rel": 41.8},
            {"position_cm-1": 364.0, "irrep": "Eg", "intensity_rel": 40.4},
            {"position_cm-1": 1012.0, "irrep": "B1g", "intensity_rel": 100.0},
        ],
    }
    v = ic.view_computed(rec)
    assert v["text"].count("364") == 1


def test_computed_view_flags_that_calculated_positions_are_offset():
    """Ab-initio frequencies are systematically shifted from measured
    ones. Presenting them as measurements would teach wrong positions as
    fact — the corroboration views compare against RRUFF, so the two must
    not be conflated."""
    rec = {
        "species": "Zircon",
        "formula": "ZrSiO4",
        "space_group_symbol": "I4_1/amd",
        "bands": [{"position_cm-1": 1012.0, "irrep": "B1g", "intensity_rel": 100.0}],
    }
    v = ic.view_computed(rec)
    assert "calculation" in v["text"].lower()
    assert "offset" in v["text"].lower()
    assert v["provenance"]["source"] == "WURM"


def test_computed_view_needs_intensities():
    rec = {
        "species": "X",
        "formula": "Y",
        "bands": [{"position_cm-1": 100.0, "irrep": "Ag", "intensity_rel": None}],
    }
    assert ic.view_computed(rec) == {}


# --------------------------------------------------------------------------
# corpus-backed: skipped when the reference layer is not on this machine
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "species,element,cn",
    [
        ("Quartz", "Si", 4),
        ("Pyrite", "Fe", 6),
        ("Rutile", "Ti", 6),
        ("Calcite", "Ca", 6),
        ("Forsterite", "Mg", 6),
    ],
)
def test_extracted_coordination_matches_textbook(species, element, cn):
    """The extractor is only trustworthy if its coordination numbers are
    right, and they are checkable. A missed symmetry operation yields too
    few atoms and every CN comes out low with no error raised."""
    cifs = rl.load_cif_features()
    if not cifs:
        pytest.skip("cif_features.json not built on this machine")
    rec = cifs.get(species.lower())
    if not rec:
        pytest.skip(f"{species} absent from AMCSD extraction")
    got = (rec.get("coordination") or {}).get(element)
    assert got is not None, f"{element} not resolved in {species}"
    assert abs(got - cn) <= 0.51, f"{species} {element}: got {got}, textbook {cn}"
