"""ROD reader and the stage-1 prose renderings of reference records."""

import os

from reference_layer import (
    ROD_DIR,
    ecostress_prose,
    iter_rod,
    parse_rod_file,
    rod_prose,
    rruff_prose,
)

ROD_TEXT = """#\\#CIF_2.0
data_1000000
loop_
_publ_author_name
'El Mendili, Y.'
'Abdelouas, A.'
_publ_section_title
;
 Insight into the mechanism of carbon steel corrosion under aerobic and
 anaerobic conditions
;
_journal_name_full               'Physical Chemistry and Chemical Physics'
_journal_year                    2013
_chemical_compound_source        'Corrosion product'
_chemical_formula_sum            'Fe3 O4'
_chemical_name_mineral           Magnetite
_space_group_crystal_system      cubic
_space_group_name_H-M_alt        'F d -3 m :1'
_raman_measurement_device.company 'HORIBA Jobin Yvon'
_raman_measurement_device.model  T64000
_raman_measurement_device.excitation_laser_wavelength 514
_raman_measurement_device.resolution 3
_rod_database.code               1000000
loop_
_raman_spectrum.raman_shift
_raman_spectrum.intensity
91.5 1155
96.3 1139
101.1 1208
"""


def test_parse_rod_lifts_metadata_text_blocks_and_spectrum():
    rec = parse_rod_file(ROD_TEXT)
    assert rec["species"] == "Magnetite" and rec["formula"] == "Fe3 O4"
    assert rec["title"].startswith("Insight into the mechanism")
    assert rec["laser_nm"] == "514" and rec["device"] == "HORIBA Jobin Yvon T64000"
    assert rec["spectrum"] == [(91.5, 1155.0), (96.3, 1139.0), (101.1, 1208.0)]
    assert rec["rod_id"] == "1000000"


def test_rod_prose_states_facts_and_never_a_band_position():
    text = rod_prose(parse_rod_file(ROD_TEXT))
    assert "Magnetite (Fe3 O4)" in text and "entry 1000000" in text
    assert "514 nm excitation" in text and "cubic" in text
    assert "91.5" not in text  # spectra are derived downstream, not quoted


def test_rruff_prose_carries_id_locality_laser_and_chemistry():
    rec = {
        "species": "Actinolite",
        "rruff_id": "R120010",
        "ideal_formula": "Ca2(Mg4.5-2.5Fe2+0.5-2.5)Si8O22(OH)2",
        "measured_formula": "Ca1.9Mg4Fe1Si8O22(OH)2",
        "locality": "Ontario, Canada",
        "laser_nm": "532",
        "cell": {
            "a": 9.89,
            "b": 18.2,
            "c": 5.31,
            "beta": 104.6,
            "crystal_system": "monoclinic",
        },
        "confirmation": "The identification of this mineral is confirmed by X-ray diffraction",
        "description": "",
    }
    text = rruff_prose(rec)
    assert "R120010" in text and "Ontario, Canada" in text and "532 nm" in text
    assert (
        "monoclinic" in text
        and "beta = 104.6°" in text
        and "confirmed by X-ray diffraction." in text
    )


def test_ecostress_prose():
    rec = {
        "species": "Albite",
        "full_name": "Albite NaAlSi3O8",
        "mineral_class": "Silicate",
        "subclass": "Tectosilicate",
        "particle_size": "Solid",
        "sample_no": "TS-4A",
        "wavelength_range": "TIR",
        "origin": "Sample from Ward's Natural Science",
    }
    text = ecostress_prose(rec)
    assert (
        "silicate (tectosilicate)" in text
        and "thermal infrared" in text
        and "TS-4A" in text
    )


def test_iter_rod_reads_the_mirror_if_present():
    if not os.path.isdir(ROD_DIR):
        return
    recs = list(iter_rod(limit=5))
    assert recs and all(r["species"] and len(r["spectrum"]) >= 32 for r in recs)
