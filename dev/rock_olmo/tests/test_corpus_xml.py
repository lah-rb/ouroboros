"""XML records (§22): the schema parses, every blank reconstructs its record,
bands are ranked by intensity, the untouched species never render, and the
plain bundles carry every record once per pass. Stdlib only."""

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

import corpus_xml as cx
from probe_scoring import strongest_bands


def _rec(sp="Calcite", system="trigonal", laser="532"):
    return cx.XmlRecord(sp, "CaCO3", system, [1086, 282, 712, 156], laser, [393.37, 396.85, 422.67, 445.48])


def test_strongest_bands_ranks_by_intensity_with_position_ties():
    assert strongest_bands([156, 282, 712, 1086], [0.1, 0.4, 0.2, 1.0], 4) == [1086, 282, 712, 156]
    assert strongest_bands([100, 200, 300], [0.5, 0.5, 0.9], 2) == [300, 100]
    assert strongest_bands([100, 200, 300], [0.5], 2) == [100, 200]  # misaligned -> by position


def test_record_parses_and_every_permutation_is_distinct_but_equivalent():
    rec = _rec()
    seen = set()
    for perm in list(range(cx.N_PERMS)) + [cx.PROBE_PERM]:
        for variant in cx.VARIANTS:
            text = cx.render_record(rec, perm, variant)
            root = ET.fromstring(text)
            assert root.tag == "mineral" and root.get("species") == "Calcite" and root.get("formula") == "CaCO3"
            assert root.find("raman").find("top").text == "1086"
            assert [n.text for n in root.find("raman").findall("next")] == ["282", "712", "156"]
            assert (root.find("libs") is not None) == (variant == "full")
            assert "\n\n" not in text  # one paragraph for the packer
            seen.add(text)
    assert len(seen) == 5 * 2 - 2  # raman_only ignores block order: perms 0/2 and 1/3 coincide
    # the probe attribute order is never one of the trained orders
    probe = cx.render_record(rec, cx.PROBE_PERM, "full").split("\n")[0]
    assert probe.startswith('<mineral formula="CaCO3" species="Calcite"')
    assert all(cx.render_record(rec, p, "full").split("\n")[0] != probe for p in range(cx.N_PERMS))


def test_system_attribute_is_omitted_when_unknown_and_laser_optional():
    text = cx.render_record(_rec(system="", laser=""), 1, "full")
    assert 'system=' not in text and text.split("\n")[1].startswith("<raman><top>")
    assert cx.kinds_for(_rec(system=""), "full") == ("species", "identity", "formula", "raman", "libs", "top")


def test_every_blank_reconstructs_the_record():
    rec = _rec()
    for perm in list(range(cx.N_PERMS)) + [cx.PROBE_PERM]:
        for variant in cx.VARIANTS:
            text = cx.render_record(rec, perm, variant)
            for kind in cx.kinds_for(rec, variant):
                p, m, s = cx.blank(text, kind)
                assert p + m + s == text and m
                if kind == "species":
                    assert m == "Calcite"
                if kind == "identity":
                    assert "species=" in m and "formula=" in m and "system=" not in m
                if kind == "raman":
                    assert m.startswith("<top>1086</top>") and m.endswith("<next>156</next>")
                if kind == "top":
                    assert m == "1086"
    with pytest.raises(ValueError):
        cx.blank(cx.render_record(rec, 0, "raman_only"), "libs")


def test_fim_example_prompt_ends_at_the_middle_sentinel_and_completion_is_the_blank():
    ex = cx.fim_example(_rec(), "identity", "full", 3, "spm")
    assert ex["prompt"].endswith(cx.fim_wrap("", "", "", "spm")[-len("<|fim_middle|>") :])
    assert ex["prompt"].startswith("<|fim_suffix|>") and ex["completion"] == 'species="Calcite" formula="CaCO3"'
    p, m, s = cx.blank(cx.render_record(_rec(), 3, "full"), "identity")
    assert ex["prompt"] == cx.fim_wrap(p, "", s, "spm")


def test_rows_and_bundles_cover_every_record_and_exclude_the_holdout():
    recs = [_rec(f"Sp{i}") for i in range(20)]
    rows = cx.fim_rows(recs, val=False)
    assert len(rows) == 20 * (7 + 5) * cx.N_PERMS * 2
    assert {r["kind"] for r in rows} == set(cx.KINDS_BY_VARIANT["full"])
    for variant in cx.VARIANTS:
        bs = cx.bundles(recs, variant, val=False)
        assert len(bs) == cx.N_BUNDLES * 3  # 20 records / 8 per bundle -> 3 bundles per pass
        names = [sp for b in bs for sp in b["provenance"]["species"]]
        assert sorted(names) == sorted([r.species for r in recs] * cx.N_BUNDLES)
        assert all(b["text"].startswith(cx.SOURCE_TAG + "\n<mineral") for b in bs)


def test_eligible_records_need_formula_ranked_bands_and_libs():
    def fact(fid, kind, sp, formula="AB", payload=None, canonical=True):
        return SimpleNamespace(fact_id=fid, kind=kind, species=sp, formula=formula, payload=payload or {}, canonical=canonical)

    facts = [
        fact("formula:Good", "formula", "Good"),
        fact("raman:canonical:Good", "raman_bands", "Good", payload={"bands_cm1": [100, 200, 300, 400], "rel": [0.2, 1.0, 0.5, 0.1], "laser_nm": "785"}),
        fact("raman:RRUFF:x", "raman_bands", "Good", payload={"bands_cm1": [1, 2, 3, 4], "rel": [1, 1, 1, 1]}, canonical=False),
        fact("libs:Good", "libs_lines", "Good", payload={"groups": [{"lines": [{"nm": 500.123, "rel": 3}, {"nm": 400.0, "rel": 9}, {"nm": 450.5, "rel": 5}, {"nm": 300.0, "rel": 1}, {"nm": 350.0, "rel": 7}]}]}),
        fact("structure:Good", "structure", "Good", payload={"crystal_system": "Cubic"}),
        fact("formula:Short", "formula", "Short"),
        fact("raman:canonical:Short", "raman_bands", "Short", payload={"bands_cm1": [100, 200, 300], "rel": [1, 1, 1]}),
        fact("libs:Short", "libs_lines", "Short", payload={"groups": []}),
        fact("formula:NoLibs", "formula", "NoLibs"),
        fact("raman:canonical:NoLibs", "raman_bands", "NoLibs", payload={"bands_cm1": [1, 2, 3, 4], "rel": [1, 2, 3, 4]}),
    ]
    recs, stats = cx.eligible_records(facts)
    assert [r.species for r in recs] == ["Good"] and stats == {"eligible": 1, "bands_short_or_unaligned": 1, "no_libs": 1}
    g = recs[0]
    assert g.bands == [200, 300, 100, 400] and g.system == "cubic" and g.laser_nm == "785"
    assert g.libs == [400.0, 350.0, 450.5, 500.12]
