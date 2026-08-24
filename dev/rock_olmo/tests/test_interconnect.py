"""Interconnected views: coverage, provenance honesty, and framing."""

from interconnect import VIEWS, build_views, view_contrastive, view_corroboration

_REC = {
    "species": "Antigorite",
    "formula": "Mg3Si2O5(OH)4",
    "modalities": {
        "Raman": [{"position_cm-1": 375.2}, {"position_cm-1": 683.1}],
        "thermal infrared": [{"position_cm-1": 1080.0}, {"position_cm-1": 960.0}],
    },
    "provenance": {
        "Raman": {"source": "RRUFF", "derivation": "peak_pick", "sample_id": "R070228"},
        "thermal infrared": {"source": "ECOSTRESS", "sample_id": "antigorite_1"},
    },
}


def test_each_modality_yields_both_directions():
    views = [v["view"] for v in build_views(_REC)]
    assert views.count("forward") == 2
    assert views.count("inverse") == 2


def test_cross_modal_requires_two_techniques():
    views = {v["view"] for v in build_views(_REC)}
    assert "cross_modal" in views
    one = dict(_REC, modalities={"Raman": _REC["modalities"]["Raman"]})
    assert "cross_modal" not in {v["view"] for v in build_views(one)}


def test_a_derived_peak_position_says_so():
    """RRUFF publishes SPECTRA, not peak lists. Presenting our own
    peak-pick as "the RRUFF value" would fabricate provenance."""
    fwd = [v for v in build_views(_REC) if v["view"] == "forward"]
    raman = next(v for v in fwd if "Raman" in v["text"])
    assert "peak-picked" in raman["text"]
    tir = next(v for v in fwd if "thermal" in v["text"])
    assert "catalogued by" in tir["text"]


def test_contrastive_needs_at_least_two_members():
    assert (
        view_contrastive(
            "SiO2", [{"species": "Quartz", "peaks": [{"position_cm-1": 464}]}]
        )
        == {}
    )
    got = view_contrastive(
        "SiO2",
        [
            {"species": "Quartz", "peaks": [{"position_cm-1": 464}]},
            {"species": "Cristobalite", "peaks": [{"position_cm-1": 416}]},
        ],
    )
    assert got["view"] == "contrastive"
    assert "cannot be told apart by chemistry alone" in got["text"]


# ── reported vs reference framing (operator ruling) ───────────────────
def _corr(reported, reference):
    return view_corroboration(
        "Antigorite",
        reported,
        reference,
        "Raman",
        {"citation": "A study"},
        {"source": "RRUFF", "derivation": "peak_pick"},
    )["text"]


def test_a_sub_unit_gap_reads_as_reporting_precision():
    assert "within the precision" in _corr(375, 375.2)


def test_a_few_wavenumber_gap_names_both_possibilities():
    """Neither an uncertainty budget nor reference authority is asserted."""
    text = _corr(371, 375.2)
    assert "may be reporting precision or a genuine shift" in text


def test_a_large_gap_offers_physical_causes_not_a_verdict():
    text = _corr(340, 375.2)
    assert "calibration" in text and "polytype" in text
    assert "wrong" not in text.lower() and "error" not in text.lower()


def test_an_exact_match_is_stated_plainly():
    assert "matching the reference position exactly" in _corr(375.2, 375.2)


def test_the_reference_is_never_called_authoritative():
    """The reference grounds; it does not adjudicate."""
    for pair in ((375, 375.2), (371, 375.2), (340, 375.2)):
        text = _corr(*pair).lower()
        assert "authoritative" not in text
        assert "incorrect" not in text


# ── degradation ──────────────────────────────────────────────────────
def test_a_thin_record_still_produces_views():
    thin = {
        "species": "Halite",
        "formula": "NaCl",
        "modalities": {"Raman": [{"position_cm-1": 200.0}]},
        "provenance": {"source": "RRUFF"},
    }
    views = {v["view"] for v in build_views(thin)}
    assert views == {"forward", "inverse"}


def test_no_species_yields_nothing():
    assert build_views({"formula": "SiO2"}) == []


def test_empty_peaks_produce_no_view():
    assert (
        build_views({"species": "X", "formula": "Y", "modalities": {"Raman": []}}) == []
    )


def test_all_view_names_are_declared():
    produced = {
        v["view"]
        for v in build_views(
            dict(
                _REC,
                siblings=[
                    {"species": "Antigorite", "peaks": [{"position_cm-1": 375.2}]},
                    {"species": "Lizardite", "peaks": [{"position_cm-1": 389.0}]},
                ],
                reported=[
                    {
                        "reported": 375,
                        "reference": 375.2,
                        "technique": "Raman",
                        "paper": {"citation": "X"},
                        "ref": {"source": "RRUFF"},
                    }
                ],
            )
        )
    }
    assert produced <= set(VIEWS)
    assert produced == set(VIEWS)
