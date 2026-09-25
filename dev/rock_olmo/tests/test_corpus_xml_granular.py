"""§22g granular blank policy: stripped records show only their cues (never system or
laser), every blank reconstructs its record, the pairs cover the chained probe, and the
budget fitter lands on the budget."""

import collections
import random

import corpus_xml as cx
import corpus_xml_granular as cg


def _rec():
    return cx.XmlRecord("Gypsum", "CaSO4·2H2O", "monoclinic", [1008, 493, 414, 1140], "532", [422.67, 656.29, 777.19, 921.29])


def test_stripped_record_shows_only_the_cues():
    r = _rec()
    t = cx.stripped_record(r, {"bands"}, "name")
    assert t == '<mineral species="\x00">\n<raman><top>1008</top><next>493</next><next>414</next><next>1140</next></raman>\n</mineral>'
    t = cx.stripped_record(r, {"bands2"}, "name")
    assert "<top>1008</top><next>493</next></raman>" in t and "414" not in t
    t = cx.stripped_record(r, {"name", "formula", "top"}, "line", libs_first=True)
    assert t.split("\n")[1] == "<libs><line>\x00</line></libs>" and t.split("\n")[2] == "<raman><top>1008</top></raman>"
    t = cx.stripped_record(r, {"formula"}, "name", formula_first=True)
    assert t.startswith('<mineral formula="CaSO4·2H2O" species="\x00">')
    for cues, target in cg.PAIRS:
        t = cx.stripped_record(r, set(cues), target)
        assert "system=" not in t and "laser" not in t and t.count(cx.BLANK) == 1


def test_every_granular_row_reconstructs_and_pairs_cover_the_probe():
    r = _rec()
    rows = cg.granular_rows(r, val=False)
    kinds = {x["kind"] for x in rows}
    assert len(kinds) == len(cg.PAIRS) == 19
    for x in rows:
        assert x["prompt"].endswith("<|fim_middle|>") and x["completion"] in ("Gypsum", "CaSO4·2H2O", "1008", "422.67")
    # both block orders only when both blocks are present; both FIM orders always
    both = [x for x in rows if x["kind"] == "g:bands+lines>name"]
    assert {x["perm"] for x in both} == {"raman_first", "libs_first"} and {x["order"] for x in both} == {"psm", "spm"}
    assert {x["perm"] for x in rows if x["kind"] == "g:bands>name"} == {"raman_first"}
    assert len(rows) == 5 * 2 * 2 + 14 * 2  # 5 pairs carry both blocks


def test_fit_to_budget_lands_on_the_budget_and_repeats_only_when_short():
    rows = [{"i": i} for i in range(100)]
    toks = [50] * 100
    idx, used = cg.fit_to_budget(rows, toks, 2500, random.Random(1))
    assert 2500 - 64 <= used <= 2500 and len(set(idx)) == len(idx)
    idx, used = cg.fit_to_budget(rows, toks, 7500, random.Random(1))
    assert 7500 - 64 <= used <= 7500 and len(set(idx)) == 100 and len(idx) > 100


def test_train_pairs_are_the_spectra_to_identity_pairs_at_eight_exposures():
    kinds = {cg.pair_kind(c, t) for c, t in cg.TRAIN_PAIRS}
    assert kinds == {
        "g:formula>name", "g:bands>name", "g:bands1>name", "g:bands2>name", "g:bands3>name",
        "g:lines>name", "g:bands+lines>name", "g:bands>formula", "g:lines>formula", "g:bands+lines>formula",
    }
    rows = cg.granular_rows(_rec(), val=False, pairs=cg.TRAIN_PAIRS, exposures=cg.EXPOSURES)
    import collections
    per = collections.Counter(r["kind"] for r in rows)
    assert set(per.values()) == {cg.EXPOSURES} and len(per) == 10
    assert len({r["ex_id"] for r in rows}) == len(rows)  # copies carry distinct ids


def test_resolution_arm_jitters_rounds_and_labels_the_grid():
    import corpus_xml_resolution as cr

    r = _rec()
    t = cx.stripped_record(r, {"bands"}, "name", raman_resolution=5)
    assert '<raman resolution_cm1="5"><top>1008</top>' in t
    rows = cr.rows_for(r, val=False)
    per = collections.Counter(x["kind"] for x in rows)
    assert per["g:bands>name"] == 16 and per["g:bands+lines>name"] == 16 and per["g:bands>formula"] == 8
    assert per["g:formula>name"] == 8 and per["g:lines>name"] == 8
    for x in rows:
        if "grid" in x:
            assert all(v % x["grid"] == 0 for v in x["bands_shown"])
            assert f'resolution_cm1="{x["grid"]}"' in x["prompt"] and "laser" not in x["prompt"]
            assert x["completion"] in ("Gypsum", "CaSO4·2H2O")
    # deterministic, and distinct draws per exposure
    again = cr.rows_for(r, val=False)
    assert [x["prompt"] for x in rows] == [x["prompt"] for x in again]
    b2n = [x for x in rows if x["kind"] == "g:bands>name"]
    assert len({tuple(x["bands_shown"]) for x in b2n}) > 1
    assert cr.quantize(1008, 5) == 1010 and cr.quantize(1007.4, 5) == 1005 and cr.quantize(493, 10) == 490


def test_digit_rendering_and_line_jitter_arm():
    import corpus_xml_resolution as cr

    r = _rec()
    assert cx.digit_str(493) == "4 9 3" and cx.digit_str("422.67") == "4 2 2 . 6 7"
    t = cx.stripped_record(r, {"bands", "lines"}, "name", raman_resolution=5, digits=True)
    assert "<top>1 0 0 8</top><next>4 9 3</next>" in t and "<line>4 2 2 . 6 7</line>" in t
    rows = cr.rows_for(r, val=False, digits=True, jitter_libs=True)
    per = collections.Counter(x["kind"] for x in rows)
    assert per["g:bands>name"] == 16 and per["g:lines>name"] == 8 and per["g:formula>name"] == 8
    base = cr.rows_for(r, val=False)  # §22h
    same_bands = {x["ex_id"]: x["bands_shown"] for x in base if x.get("grid")}
    for x in rows:
        if x.get("grid"):
            assert x["bands_shown"] == same_bands[x["ex_id"]]  # band draws identical to §22h
        if "lines" in x["kind"].split(">")[0].split("+"):
            assert x["lines_shown"] != r.libs and all(abs(a - b) <= 0.101 for a, b in zip(x["lines_shown"], r.libs))
            assert "4 2 2" in x["prompt"] or "4 2 3" in x["prompt"] or "4 2 1" in x["prompt"]
    assert all("1008" not in x["prompt"] for x in rows if x.get("grid"))


def test_budget_arm_exposures_spec():
    import corpus_xml_resolution as cr

    r = _rec()
    spec = cr.parse_exposures("bands>name=64,bands+lines>name=16,formula>name=8")
    assert spec == {"g:bands>name": 64, "g:bands+lines>name": 16, "g:formula>name": 8}
    rows = cr.rows_for(r, val=False, digits=True, jitter_libs=True, exposures=spec)
    assert collections.Counter(x["kind"] for x in rows) == spec  # every other pair dropped
    d = {x["ex_id"]: x for x in cr.rows_for(r, val=False, digits=True, jitter_libs=True)}  # §22i
    shared = [x for x in rows if x["ex_id"] in d]
    assert len([x for x in shared if x["kind"] == "g:bands>name"]) == 16  # draws 0-15 are §22i's
    assert all(x["prompt"] == d[x["ex_id"]]["prompt"] for x in shared if x.get("grid"))
    assert len({tuple(x["bands_shown"]) for x in rows if x["kind"] == "g:bands>name"}) > 32
    v = cr.rows_for(r, val=True, digits=True, jitter_libs=True, exposures=spec)
    assert collections.Counter(x["kind"] for x in v) == {"g:bands>name": 2, "g:bands+lines>name": 2, "g:formula>name": 2}
    try:
        cr.parse_exposures("bands>top=4")
        raise AssertionError("an untrained pair must be refused")
    except SystemExit:
        pass


def _spectrum():
    return [(1008.0, 1.0), (493.0, 0.6), (414.0, 0.5), (1140.0, 0.45), (620.0, 0.3), (670.0, 0.25), (1135.0, 0.2), (180.0, 0.1)]


def test_representation_arm_rows():
    import corpus_xml_variation as cv

    r = _rec()
    pool = [300.0, 850.0, 1320.0]
    rows = cv.rows_for(r, _spectrum(), pool, val=False)
    assert collections.Counter(x["kind"] for x in rows) == {"g:bands>name": 64, "g:bands+lines>name": 16, "g:formula>name": 8, "g:name>formula": 8}
    drawn = [x for x in rows if x.get("k")]
    assert len(drawn) == 80
    for x in drawn:
        b = x["bands_shown"]
        assert b == sorted(set(b)) and 1 <= len(b) <= x["k"] <= 8 and all(v % x["grid"] == 0 for v in b)
        assert "<band>" in x["prompt"] and "<top>" not in x["prompt"] and f'resolution_cm1="{x["grid"]}"' in x["prompt"]
        assert x["prompt"].count("<band>") == len(b)
    assert len({tuple(x["bands_shown"]) for x in drawn if x["kind"] == "g:bands>name"}) > 40  # draws differ
    assert any(len(x["bands_shown"]) > 4 for x in drawn) and {x["k"] for x in drawn} <= set(range(4, 9))
    lines = [x for x in drawn if x["kind"] == "g:bands+lines>name"]
    assert all(x["lines_shown"] != r.libs for x in lines) and all("<libs>" in x["prompt"] for x in lines)
    nf = [x for x in rows if x["kind"] == "g:name>formula"]
    assert all("<raman" not in x["prompt"] and x["completion"] == r.formula for x in nf)
    assert rows == cv.rows_for(r, _spectrum(), pool, val=False)  # deterministic
    v = cv.rows_for(r, _spectrum(), pool, val=True)
    assert collections.Counter(x["kind"] for x in v) == {"g:bands>name": 2, "g:bands+lines>name": 2, "g:formula>name": 2, "g:name>formula": 2}


def test_representation_draws_reproduce_the_real_statistics():
    import os

    import pytest

    import corpus_xml_variation as cv

    if not os.path.exists(cv.SPECTRA):
        pytest.skip("spectra_full.json not built")
    rep = cv.calibration_check(draws=2)
    real, sim = rep["real"], rep["simulated"]
    assert rep["species"] >= 600
    assert abs(sim["top1"] - real["top1"]) < 0.06 and abs(sim["set4"] - real["set4"]) < 0.06
    assert abs(sim["present"] - real["present"]) < 0.03 and abs(sim["extra4"] - real["extra4"]) < 0.04
