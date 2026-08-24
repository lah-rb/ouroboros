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


def test_a_gap_is_never_framed_as_the_paper_being_wrong():
    """Superseded the earlier fixed-phrase tests: the tiered policy below
    checks each band, this checks the invariant across all of them."""
    for pair in ((375, 375.2), (375, 377), (375, 382), (375, 400)):
        text = _corr(*pair).lower()
        assert "wrong" not in text and "error" not in text
        assert "incorrect" not in text


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


# ── tolerance policy (measured knee + field convention) ──────────────
from interconnect import (  # noqa: E402
    ACCEPTED_COMPARISON_CM1,
    INSTRUMENT_PRECISION_CM1,
    within_tolerance,
)


def test_tolerance_tiers_are_ordered():
    assert INSTRUMENT_PRECISION_CM1 < ACCEPTED_COMPARISON_CM1


def test_sub_instrument_gap_reads_as_agreement():
    assert "the two agree" in _corr(375.0, 375.4)


def test_a_few_wavenumbers_is_reporting_precision():
    assert "normally reported" in _corr(375, 377)


def test_within_ten_offers_drift_or_a_real_shift():
    text = _corr(375, 382)
    assert "calibration drift" in text and "substitution" in text


def test_beyond_ten_is_named_a_probable_different_mode():
    text = _corr(375, 400)
    assert "different vibrational mode" in text


def test_pairs_beyond_tolerance_are_gated_out_entirely():
    """Not a weak corroboration — probably an unrelated band. Emitting
    it would teach a false equivalence."""
    assert within_tolerance(375, 382)
    assert not within_tolerance(375, 400)
    rec = dict(_REC, reported=[
        {"reported": 375, "reference": 400, "technique": "Raman",
         "paper": {"citation": "X"}, "ref": {"source": "RRUFF"}},
        {"reported": 375, "reference": 377, "technique": "Raman",
         "paper": {"citation": "Y"}, "ref": {"source": "RRUFF"}},
    ])
    corr = [v for v in build_views(rec) if v["view"] == "corroboration"]
    assert len(corr) == 1
    assert "Y" in corr[0]["text"]


# ── derivation honesty across sources ────────────────────────────────
def test_trough_picks_are_not_described_as_catalogued():
    """Regression: checking only "peak_pick" made ECOSTRESS trough
    positions read "as catalogued by ECOSTRESS", asserting the library
    published positions it does not publish."""
    rec = {
        "species": "Ilmenite",
        "formula": "Fe2+Ti4+O3",
        "modalities": {"thermal infrared": [{"position_um": 12.522}]},
        "provenance": {
            "thermal infrared": {"source": "ECOSTRESS", "derivation": "trough_pick"}
        },
    }
    text = next(v for v in build_views(rec) if v["view"] == "forward")["text"]
    assert "absorption minima" in text
    assert "catalogued" not in text


def test_genuinely_catalogued_data_still_says_catalogued():
    rec = {
        "species": "Ilmenite",
        "formula": "Fe2+Ti4+O3",
        "modalities": {"Raman": [{"position_cm-1": 680.0}]},
        "provenance": {"Raman": {"source": "SomeCatalogue"}},
    }
    text = next(v for v in build_views(rec) if v["view"] == "forward")["text"]
    assert "catalogued by SomeCatalogue" in text
