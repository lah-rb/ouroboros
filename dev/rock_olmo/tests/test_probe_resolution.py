"""The §22j probe instruments: the peak-matcher ceiling, field masks, the hundreds-boundary flag."""

import json

import numpy as np
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import corpus_xml as cx  # noqa: E402
import probe_resolution as pr  # noqa: E402

RECS = [
    {"species": "A", "formula": "X", "system": "", "bands": [1008, 493, 415, 1140], "laser_nm": "", "libs": [422.67, 393.37, 396.85, 445.48]},
    {"species": "B", "formula": "Y", "system": "", "bands": [1085, 282, 712, 156], "laser_nm": "", "libs": [422.67, 393.37, 396.85, 445.48]},
    {"species": "C", "formula": "Z", "system": "", "bands": [1086, 283, 713, 157], "laser_nm": "", "libs": [589.00, 589.59, 330.24, 285.30]},
]


def _lib(tmp_path):
    p = tmp_path / "xml_records.json"
    p.write_text(json.dumps({"trained": RECS[:2], "untouched": RECS[2:]}))
    return pr.Library(str(p))


def test_ceiling_matches_exact_splits_ties_and_reads_lines(tmp_path):
    lib = _lib(tmp_path)
    assert lib.top1("A", [1008, 493, 415, 1140]) == 1.0
    assert lib.top1("A", [1010, 496, 411, 1143], grid=1) == 1.0  # within ±5
    assert lib.top1("B", [1085, 282, 712, 156]) == 0.5  # C's bands sit within 1 cm-1: a tie
    assert lib.top1("B", [1085, 282, 712, 156], [422.67, 393.37, 396.85, 445.48]) == 1.0  # the lines break it
    assert lib.top1("A", None, None) == 1 / 3  # nothing shown: chance


def test_crosses_hundreds_boundary():
    assert pr.crosses_100([501, 300], [499, 300], by_index=True)
    assert not pr.crosses_100([497, 300], [499, 300], by_index=True)
    assert pr.crosses_100([501, 820], [499, 300, 1000], by_index=False)  # 820 has no canonical within 15
    assert not pr.crosses_100([820], [499, 300], by_index=False)


def test_masked_variants_keep_the_format_and_blank_one_field(tmp_path):
    lib = _lib(tmp_path)
    rec = cx.XmlRecord(**RECS[0])
    v = {r: (res, x) for r, res, x in pr.variants_for(rec, "bands+lines>name", 5, 1, lib)}
    assert set(v) == {"none", "correct", "wrong", "mask-bands", "mask-lines"}
    assert v["mask-bands"][1].bands == lib.const_bands and v["mask-bands"][1].libs == rec.libs
    assert v["mask-lines"][1].libs == lib.const_lines and v["mask-lines"][1].bands == rec.bands
    assert v["mask-bands"][0] == 5  # masked records keep the correct field
    t = cx.stripped_record(v["mask-bands"][1], {"bands", "lines"}, "name", raman_resolution=5, digits=True)
    assert t.count("<next>") == 3 and t.count("<line>") == 4
    assert set(dict(((r, 0) for r, _, _ in pr.variants_for(rec, "bands>name", 5, 1, lib)))) == {"none", "correct", "wrong", "mask-bands"}
    assert pr.ceiling_for(lib, "A", ("bands",), "mask-bands", v["mask-bands"][1], 5) == 1 / 3


def test_keys_for_pools_synthetic_and_splits_carry():
    row = {"group": "T", "condition": "synthetic·portable", "carry": True}
    assert set(pr.keys_for(row)) == {("T", "synthetic·portable"), ("T", "synthetic·all"),
                                     ("T:crosses-100", "synthetic·portable"), ("T:crosses-100", "synthetic·all")}
    assert pr.keys_for({"group": "T", "condition": "exact", "carry": False}) == [("T", "exact")]
    r = pr.keys_for({"group": "R", "condition": "real·grid5", "carry": False, "set_match": True})
    assert ("R:set-match", "real·grid5") in r and ("R:same-100", "real·grid5") in r


def test_library_keyed_on_strongest_k_with_short_spectra_padded(tmp_path):
    spectra = {
        "A": [(1008.0, 1.0), (493.0, 0.6), (415.0, 0.5), (1140.0, 0.4), (620.0, 0.3), (670.0, 0.2)],
        "B": [(1085.0, 1.0), (282.0, 0.5), (712.0, 0.4), (156.0, 0.3)],  # only four bands: k6/k8 rows NaN-padded
        "C": [(1086.0, 1.0), (283.0, 0.5), (713.0, 0.4), (157.0, 0.3), (1436.0, 0.2), (1749.0, 0.1)],
    }
    p = tmp_path / "xml_records.json"
    p.write_text(json.dumps({"trained": RECS[:2], "untouched": RECS[2:]}))
    lib = pr.Library(str(p), spectra=spectra)
    assert lib.bands_k[6].shape == (3, 6) and np.isnan(lib.bands_k[6][1]).sum() == 2
    assert lib.top1("A", [415, 493, 620, 670, 1008, 1140], k=6) == 1.0
    assert lib.top1("B", [156, 282, 712, 1085], k=6) == 1.0  # B's four bands all matched: F1 1 against its own 4
    assert lib.top1("C", [157, 283, 713, 1086, 1436, 1749], k=6) == 1.0  # C's extra two bands break the B/C tie
    assert len(lib.const_bands_k[6]) <= 6 and lib.const_bands_k[6] == sorted(lib.const_bands_k[6])


def test_forced_variation_draw_is_sorted_grid_rounded_and_seeded():
    peaks = [(1008.0, 1.0), (493.0, 0.6), (415.0, 0.5), (1140.0, 0.4), (620.0, 0.3), (670.0, 0.2), (1135.0, 0.15)]
    pool = [300.0, 850.0]
    for klass, grid in (("lab", 1), ("portable", 5), ("handheld", 10)):
        bands, g = pr.forced_variation_draw("A", peaks, pool, klass, 0)
        assert g == grid and bands == sorted(set(bands)) and 1 <= len(bands) <= pr.SYNTH_K and all(b % grid == 0 for b in bands)
        assert (bands, g) == pr.forced_variation_draw("A", peaks, pool, klass, 0)
    assert pr.forced_variation_draw("A", peaks, pool, "lab", 0) != pr.forced_variation_draw("A", peaks, pool, "lab", 1)
