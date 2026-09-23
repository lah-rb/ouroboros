"""§22g granular blank policy: stripped records show only their cues (never system or
laser), every blank reconstructs its record, the pairs cover the chained probe, and the
budget fitter lands on the budget."""

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
