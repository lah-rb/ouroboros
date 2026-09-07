"""Section-bounded pack windows: never cut a section, lose nothing, merge honestly."""

from __future__ import annotations

import pytest

from agent.actions.pack_windows import (
    MergeReport,
    merge_packs,
    split_sections,
    window_sections,
)


def _doc(n_sections: int, words_per: int, lead: str = "Title line\n\n") -> str:
    body = " ".join(["word"] * words_per)
    return lead + "".join(f"## Section {i}\n\n{body}\n\n" for i in range(n_sections))


def test_split_is_lossless_and_ordered():
    doc = _doc(5, 40)
    secs = split_sections(doc)
    assert "".join(secs) == doc
    assert secs[0].startswith(
        "Title line"
    ), "text before the first heading is its own section"
    assert [s.splitlines()[0] for s in secs[1:]] == [
        f"## Section {i}" for i in range(5)
    ]


def test_windows_never_cut_a_section_and_cover_every_section_once():
    # ~40 words ≈ 60 tokens a section; target 200 -> ~3 sections a window.
    doc = _doc(10, 40)
    wins = window_sections(doc, target_tokens=200, max_tokens=400)
    assert len(wins) > 1
    assert (
        "".join(w.text for w in wins) == doc
    ), "windows must tile the document exactly"
    for w in wins:
        # every window starts at a section start (a heading or the lead block)
        assert w.text.startswith("## Section") or w.text.startswith("Title line")
        assert w.tokens <= 200 or w.section_count == 1
    assert [w.index for w in wins] == list(range(len(wins)))


def test_an_oversize_section_is_its_own_window_and_flagged():
    big = "## Huge\n\n" + " ".join(["x"] * 3000) + "\n\n"
    doc = "## A\n\nsmall\n\n" + big + "## B\n\nsmall\n\n"
    wins = window_sections(doc, target_tokens=100, max_tokens=200)
    huge = [w for w in wins if w.first_heading == "Huge"]
    assert len(huge) == 1 and huge[0].oversize and huge[0].section_count == 1
    assert "".join(w.text for w in wins) == doc


def test_a_document_without_headings_is_one_window():
    doc = " ".join(["plain"] * 500)
    wins = window_sections(doc, target_tokens=50, max_tokens=100)
    assert len(wins) == 1 and wins[0].text == doc and wins[0].oversize


def test_bad_sizing_is_rejected():
    with pytest.raises(ValueError):
        window_sections("## a\n\nb", target_tokens=0, max_tokens=10)
    with pytest.raises(ValueError):
        window_sections("## a\n\nb", target_tokens=10, max_tokens=5)


def test_merge_concatenates_lists_dedupes_and_records_scalar_conflicts():
    a = {
        "raman_peak_wavenumber_cm-1": [{"peak_cm-1": 1091}, {"peak_cm-1": 1366}],
        "laser_nm": 532,
        "meta": {"instrument": "Renishaw"},
    }
    b = {
        "raman_peak_wavenumber_cm-1": [{"peak_cm-1": 1366}, {"peak_cm-1": 712}],
        "laser_nm": 785,  # disagreement across windows
        "meta": {"grating": "1800 l/mm"},
        "sample_count": 3,
    }
    rep = MergeReport()
    m = merge_packs([a, b], rep)
    assert m["raman_peak_wavenumber_cm-1"] == [
        {"peak_cm-1": 1091},
        {"peak_cm-1": 1366},
        {"peak_cm-1": 712},
    ], "lists concatenate with exact duplicates dropped, order kept"
    assert m["laser_nm"] == 532, "first value wins"
    assert rep.conflicts == [{"path": "laser_nm", "kept": 532, "dropped": 785}]
    assert m["meta"] == {"instrument": "Renishaw", "grating": "1800 l/mm"}
    assert m["sample_count"] == 3


def test_merge_promotes_a_scalar_that_meets_a_list_and_ignores_none():
    m = merge_packs([{"k": 1, "n": None}, {"k": [2, 3], "n": 5}])
    assert m["k"] == [1, 2, 3]
    assert m["n"] == 5


def test_shape_repair_wraps_grounded_scalars_into_the_registry_shape():
    """CAUGHT LIVE (2026-09-02): a window grounded 678 values at 1.0 and was
    rejected because xrd_2theta_deg came back as bare numbers where the
    registry holds list[object]. Re-wrapping loses nothing and invents nothing."""
    from agent.actions.pack_windows import repair_shapes

    registry = {
        "xrd_2theta_deg": {
            "type": "list[object]",
            "exemplar": '[{"sample": "Calc7", "peak_2theta_deg": 25.8, "assignment": "002"}, {"sa',
        },
        "raman_peak_wavenumber_cm-1": {
            "type": "list[object]",
            "exemplar": [{"polymer": "PE", "peak_cm-1": 1295, "assignment": "CH2"}],
        },
        "laser_nm": {"type": "number", "exemplar": 532},
    }
    data = {
        "xrd_2theta_deg": [25.8, 31.7, "45.5 (w)"],
        "raman_peak_wavenumber_cm-1": [{"peak_cm-1": 1091}, 1366],  # mixed
        "laser_nm": [532, 785],  # number vs list: NOT ours to fix
        "unknown_key": [1, 2],
    }
    fixed, repairs = repair_shapes(data, registry)
    assert fixed["xrd_2theta_deg"] == [
        {"peak_2theta_deg": 25.8},
        {"peak_2theta_deg": 31.7},
        {"peak_2theta_deg": "45.5 (w)"},
    ]
    assert fixed["raman_peak_wavenumber_cm-1"] == [
        {"peak_cm-1": 1091},
        {"peak_cm-1": 1366},
    ]
    assert fixed["laser_nm"] == [
        532,
        785,
    ], "a number/list mismatch is not a re-wrapping"
    assert fixed["unknown_key"] == [1, 2]
    assert sorted(r["key"] for r in repairs) == [
        "raman_peak_wavenumber_cm-1",
        "xrd_2theta_deg",
    ]
    assert data["xrd_2theta_deg"] == [25.8, 31.7, "45.5 (w)"], "input is not mutated"


def test_shape_repair_wraps_a_lone_object_or_scalar_into_a_list():
    from agent.actions.pack_windows import repair_shapes

    registry = {
        "emission_line_nm": {
            "type": "list[object]",
            "exemplar": '[{"wavelength_nm": 311}]',
        }
    }
    fixed, rep = repair_shapes({"emission_line_nm": {"wavelength_nm": 766.5}}, registry)
    assert fixed["emission_line_nm"] == [{"wavelength_nm": 766.5}]
    fixed, rep = repair_shapes({"emission_line_nm": 766.5}, registry)
    assert fixed["emission_line_nm"] == [{"wavelength_nm": 766.5}]
    assert [r["from"] for r in rep] == ["scalar"]


def test_the_exemplar_field_is_chosen_by_affinity_not_position():
    """CAUGHT LIVE (2026-09-03): the exemplar for xrd_peaks_2theta_deg is
    {"sintering_temperature_c": 850, "phase": ..., "peak_2theta_deg": 34.32},
    and taking the first numeric field wrapped 2-theta values as SINTERING
    TEMPERATURES -- a silent corruption, worse than not repairing."""
    from agent.actions.pack_windows import repair_shapes

    registry = {
        "xrd_peaks_2theta_deg": {
            "type": "list[object]",
            "exemplar": '[{"sintering_temperature_c": 850, "phase": "CaH2O2", "peak_2theta_deg": 34.32}]',
        }
    }
    fixed, _ = repair_shapes({"xrd_peaks_2theta_deg": [25.8, 31.7]}, registry)
    assert fixed["xrd_peaks_2theta_deg"] == [
        {"peak_2theta_deg": 25.8},
        {"peak_2theta_deg": 31.7},
    ]


def test_an_unrelatable_exemplar_is_left_alone_rather_than_guessed():
    """Several numeric fields and none belongs to the key: any choice
    mislabels real data, so the repair declines and the gate decides."""
    from agent.actions.pack_windows import repair_shapes

    registry = {
        "mystery_values": {
            "type": "list[object]",
            "exemplar": '[{"pressure_gpa": 2, "duration_h": 5}]',
        }
    }
    fixed, repairs = repair_shapes({"mystery_values": [1, 2]}, registry)
    assert fixed["mystery_values"] == [1, 2] and repairs == []


def test_a_shallow_list_wants_the_bare_value_wrapped_not_boxed():
    """Registry list[string] met by a bare string becomes a one-element list.

    Before 2026-09-04 repair_shapes only handled list[object], so a scalar
    under a shallow list type was left for the gate. The gate accepts it
    (T and list[T] are one slot), but the corpus then held both shapes for
    one key and models coined the plural spelling to escape -- which is how
    19 duplicate key pairs were manufactured.
    """
    from agent.actions.pack_windows import repair_shapes

    registry = {
        "spectrometer_model": {"type": "list[string]", "count": 3},
        "laser_wavelength_nm": {"type": "list[number]", "count": 2},
        "already_a_list": {"type": "list[string]", "count": 1},
        "wrong_element_type": {"type": "list[string]", "count": 1},
        "bare_list": {"type": "list", "count": 1},
    }
    data, repairs = repair_shapes(
        {
            "spectrometer_model": "Renishaw inVia",
            "laser_wavelength_nm": 532,
            "already_a_list": ["Peru"],
            "wrong_element_type": 7,
            "bare_list": "x",
        },
        registry,
    )
    assert data["spectrometer_model"] == ["Renishaw inVia"]
    assert data["laser_wavelength_nm"] == [532]
    assert data["already_a_list"] == ["Peru"], "a list is left alone"
    assert data["bare_list"] == ["x"], "a bare list type takes any single value"
    assert data["wrong_element_type"] == 7, (
        "a number under list[string] is DRIFT, not a shape problem -- left for "
        "the registry check to fail rather than silently coerced"
    )
    assert {(r["key"], r["to"]) for r in repairs} == {
        ("spectrometer_model", "list[string]"),
        ("laser_wavelength_nm", "list[number]"),
        ("bare_list", "list"),
    }


def test_repair_shapes_drops_null_valued_keys():
    """A null is "not reported", not a value in the wrong shape.

    Left in place it is fatal: NoneType matches no registry type and
    _types_compatible cannot rescue it, so one unreported field fails an
    otherwise good window. Live case 2026-09-05: doi_10.2351_1.4792615 lost
    BOTH windows to four null laser/XRD parameters.
    """
    from agent.actions.curation_actions import registry_check
    from agent.actions.pack_windows import repair_shapes

    registry = {
        "laser_pulse_duration_ns": {"type": "number"},
        "xrd_wavelength_angstrom": {"type": "number"},
        "mineral_name": {"type": "string"},
    }
    data = {
        "laser_pulse_duration_ns": None,
        "xrd_wavelength_angstrom": None,
        "mineral_name": "quartz",
    }
    out, repairs = repair_shapes(data, registry)

    assert "laser_pulse_duration_ns" not in out
    assert "xrd_wavelength_angstrom" not in out
    assert out["mineral_name"] == "quartz"  # a real value is untouched
    assert {r["key"] for r in repairs} == {
        "laser_pulse_duration_ns",
        "xrd_wavelength_angstrom",
    }
    assert all(r["to"] == "dropped" for r in repairs)

    # and the repaired data now passes the check the nulls used to fail
    assert registry_check(out, registry)["type_mismatches"] == []


def test_repair_shapes_null_under_a_list_registry_type_is_dropped_not_wrapped():
    """The shallow-list wrap must not turn a null into [None]."""
    from agent.actions.pack_windows import repair_shapes

    registry = {"instrument_model": {"type": "list[string]"}}
    out, repairs = repair_shapes({"instrument_model": None}, registry)
    assert "instrument_model" not in out
    assert repairs == [
        {"key": "instrument_model", "from": "null", "to": "dropped", "n": 0}
    ]
