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
