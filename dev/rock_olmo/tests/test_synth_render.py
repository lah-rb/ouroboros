"""The filler: numbers survive, held-out framings never train, group scoping,
record shape, determinism -- on an in-memory bank and facts."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
)

from agent.actions.synth_gate import bank_row  # noqa: E402
from facts import Fact  # noqa: E402
import synth_export as se  # noqa: E402
import synth_render as sr  # noqa: E402
from test_synth_export import FACTS, IMA, LIBS, RAMAN_REL  # noqa: E402
from test_templates import STRUCT  # noqa: E402

TEMPLATES = [
    (
        "raman_bands",
        "measurement_report",
        "forward",
        "statement",
        "Field note, {instrument_class} unit{laser}: {species} gave picked peaks at",
        " {peak_list}; {how}, {tol}.",
    ),
    (
        "raman_bands",
        "measurement_report",
        "forward",
        "statement",
        "Bench log for {sp_f}: peaks",
        " {peak_list} (calibrated on {calibration}).",
    ),
    (
        "raman_bands",
        "identification",
        "backward",
        "statement",
        "Picked peaks {peak_list}{laser}; the phase is",
        " {species}{also}.",
    ),
    (
        "raman_bands",
        "catalogue_entry",
        "forward",
        "statement",
        "{archive} card {sample}: {species}, bands",
        " {bands}.",
    ),
    (
        "raman_bands",
        "band_neighbourhood",
        "forward",
        "statement",
        "Near {species} in strongest-band terms sit {neighbours}; its full list is",
        " {bands}.",
    ),
    (
        "raman_bands",
        "cross_instrument",
        "forward",
        "statement",
        "{species} by two probes: Raman {bands}; LIBS",
        " {libs_lines} at {libs_T} K.",
    ),
    (
        "raman_bands",
        "provenance",
        "forward",
        "statement",
        "Specimen {sample} from {locality} ({archive}), {species}:",
        " {bands}.",
    ),
    (
        "formula",
        "definition",
        "forward",
        "statement",
        "Glossary: {species} —",
        " {formula}.",
    ),
    (
        "formula",
        "definition",
        "backward",
        "question",
        "Q: Which mineral has the formula {formula}?",
        " A: {species}.",
    ),
    (
        "structure",
        "structure_card",
        "forward",
        "statement",
        "{species} crystallises",
        " {clauses}.{tail}",
    ),
    (
        "structure",
        "group_membership",
        "forward",
        "statement",
        "Among the {group} minerals {species} sits beside {neighbours}; it",
        " {clauses}.",
    ),
    (
        "libs_lines",
        "measurement_report",
        "forward",
        "statement",
        "LIBS at {libs_T} K ({window}), {species}:",
        " {libs_lines}.",
    ),
    (
        "libs_lines",
        "identification",
        "backward",
        "statement",
        "Emission lines {libs_lines}; the sample is",
        " {species}.",
    ),
]


def _bank():
    rows = []
    for i, (kind, fam, d, form, p, c) in enumerate(TEMPLATES):
        row = bank_row(
            {
                "prompt": p,
                "completion": c,
                "direction": d,
                "form": form,
                "structure": f"s{i}",
            },
            kind,
            fam,
            {"model": "test"},
        )
        rows.append(row)
    return rows


def _graph():
    facts = list(FACTS) + [
        Fact(
            "structure:Coesite",
            "structure",
            "Coesite",
            "SiO2",
            {"crystal_system": "Trigonal", "strunz_class": "oxide"},
            {},
            "mindat",
        ),
        Fact(
            "raman:canonical:Coesite",
            "raman_bands",
            "Coesite",
            "SiO2",
            {
                "bands_cm1": [116.0, 269.0, 466.0],
                "rel": [0.3, 0.4, 1.0],
                "tech": "Raman",
                "sample_id": "R070565",
                "laser_nm": "532",
            },
            {"source": "RRUFF", "derivation": "peak_pick"},
            "RRUFF",
        ),
    ]
    return sr.Graph(
        facts, {"doi_x": "Raman study of silica polymorphs"}, {"Quartz": ["doi_x"]}
    )


def _spec():
    return se.build_spec(FACTS, IMA)


def _run(groups, **kw):
    spec = _spec()
    idx = sr.index_bank(_bank(), 0)  # no holdout split here
    out = []
    st = sr.render(
        idx,
        spec,
        _graph(),
        groups,
        exposures=kw.get("exposures", 3),
        relational=kw.get("relational", 2),
        variants=kw.get("variants", 2),
        sink=out.append,
    )
    return st, out


def test_numbers_survive_and_records_are_shaped():
    st, docs = _run({"T_RL": ["Quartz"], "T_L": [], "T_R": [], "C": ["Coesite"]})
    assert docs and st.numbers_failed == 0
    for d in docs:
        assert set(d) == {
            "doc_id",
            "source",
            "text",
            "license",
            "provenance",
            "val",
            "max_repeats",
            "tokens_est",
        }
        assert d["source"].startswith("synthetic/") and d["val"] is False
        pv = d["provenance"]
        assert (
            pv["group"] == "T_RL"
            and pv["species"] == "Quartz"
            and pv["variant"] in (0, 1)
        )
        assert "{" not in d["text"] and "}" not in d["text"]
    kinds = {d["provenance"]["kind"] for d in docs}
    assert {"formula", "structure", "raman_bands", "libs_lines"} <= kinds
    # controls render nothing
    assert not any(d["provenance"]["species"] == "Coesite" for d in docs)
    # a measured peak list carries intensities and the archive list stays canonical
    rep = [
        d
        for d in docs
        if d["provenance"]["family"] == "measurement_report"
        and d["provenance"]["kind"] == "raman_bands"
    ]
    assert rep and all("(1.00)" in d["text"] for d in rep)
    cat = [d for d in docs if d["provenance"]["family"] == "catalogue_entry"]
    assert cat and all("464.8" in d["text"] for d in cat)
    cross = [d for d in docs if d["provenance"]["family"] == "cross_instrument"]
    assert cross and all("nm" in d["text"] for d in cross)
    prov = [d for d in docs if d["provenance"]["family"] == "provenance"]
    assert prov == [] or all("RRUFF" in d["text"] for d in prov)


def test_group_scoping():
    st, docs = _run({"T_RL": [], "T_L": ["Quartz"], "T_R": [], "C": []})
    kinds = {d["provenance"]["kind"] for d in docs}
    assert "libs_lines" in kinds and "raman_bands" not in kinds
    assert {"formula", "structure"} <= kinds
    st2, docs2 = _run({"T_RL": [], "T_L": [], "T_R": ["Quartz"], "C": []})
    kinds2 = {d["provenance"]["kind"] for d in docs2}
    assert "raman_bands" in kinds2 and "libs_lines" not in kinds2
    assert not any(d["provenance"]["family"] == "cross_instrument" for d in docs2)


def test_holdout_split_never_trains_and_is_deterministic():
    spec = _spec()
    rows = _bank()
    idx = sr.index_bank(rows, 2)  # ~half held out
    held = {
        r["template_id"]
        for split in ("holdout",)
        for k in idx[split].values()
        for d in k.values()
        for f in d.values()
        for r in f
    }
    assert held and len(held) < len(rows)
    out = []
    sr.render(
        idx,
        spec,
        _graph(),
        {"T_RL": ["Quartz"], "T_L": [], "T_R": [], "C": []},
        exposures=3,
        relational=2,
        variants=1,
        sink=out.append,
    )
    train = [d for d in out if not d["val"]]
    hold = [d for d in out if d["val"]]
    assert train and hold
    assert not any(d["provenance"]["template_id"] in held for d in train)
    assert all(d["provenance"]["template_id"] in held for d in hold)
    assert all(d["source"] == "synthetic/holdout" for d in hold)
    out2 = []
    sr.render(
        idx,
        spec,
        _graph(),
        {"T_RL": ["Quartz"], "T_L": [], "T_R": [], "C": []},
        exposures=3,
        relational=2,
        variants=1,
        sink=out2.append,
    )
    assert json.dumps(out, sort_keys=True) == json.dumps(out2, sort_keys=True)


def test_variants_change_wording_or_instrument():
    st, docs = _run(
        {"T_RL": [], "T_L": [], "T_R": ["Quartz"], "C": []}, exposures=2, variants=2
    )
    rep = [d for d in docs if d["provenance"]["family"] == "measurement_report"]
    v0 = {d["text"] for d in rep if d["provenance"]["variant"] == 0}
    v1 = {d["text"] for d in rep if d["provenance"]["variant"] == 1}
    assert v0 and v1 and v0 != v1
