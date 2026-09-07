"""Template library: breadth, determinism, verbatim numbers, v3 frame 0."""

import interconnect as ic
from facts import Fact
from templates import (
    FRAMES,
    MIN_QUESTIONS,
    MIN_STATEMENTS,
    PROBE_FRAMES,
    fields,
    pick_frames,
    render,
    render_kind,
)

RAMAN = Fact(
    "raman:canonical:Quartz",
    "raman_bands",
    "Quartz",
    "SiO2",
    {
        "bands_cm1": [128.0, 206.3, 264.1, 355.7, 401.2, 464.8],
        "tech": "Raman",
        "sample_id": "R040031",
        "laser_nm": "532",
    },
    {"source": "RRUFF", "derivation": "peak_pick", "sample_id": "R040031"},
    "RRUFF",
)
STRUCT = Fact(
    "structure:Quartz",
    "structure",
    "Quartz",
    "SiO2",
    {
        "crystal_system": "Trigonal",
        "space_group": "P3_221",
        "cell": {"a": 4.913, "c": 5.405},
        "strunz_class": "oxide",
        "density_calc": 2.66,
        "hardness_max": 7.0,
        "coordination": {"Si": 4.0},
        "shortest_bond_a": 1.605,
        "shortest_bond_pair": "Si-O",
    },
    {"source": "mindat+AMCSD"},
    "mindat+AMCSD",
)
ALL = {
    "formula": Fact(
        "formula:Quartz", "formula", "Quartz", "SiO2", {}, {"source": "IMA"}, "mindat"
    ),
    "raman_bands": RAMAN,
    "inverse": RAMAN,
    "ir_troughs": Fact(
        "ir:x",
        "ir_troughs",
        "Calcite",
        "CaCO3",
        {
            "troughs_um": [6.51, 11.4, 14.1],
            "tech": "thermal infrared",
            "sample_id": "TS-1",
        },
        {"source": "ECOSTRESS", "derivation": "trough_pick"},
        "ECOSTRESS",
    ),
    "cross_modal": Fact(
        "xmodal:Q",
        "cross_modal",
        "Quartz",
        "SiO2",
        {
            "modalities": {
                "Raman": {"positions": [128.0, 464.8], "unit": "cm-1"},
                "thermal infrared": {"positions": [8.62, 9.2], "unit": "um"},
            }
        },
        {},
        "x",
    ),
    "contrastive": Fact(
        "c:1",
        "contrastive",
        ["Anatase", "Rutile"],
        "TiO2",
        {
            "members": [
                {"species": "Anatase", "bands_cm1": [144.0, 397.0, 516.0]},
                {"species": "Rutile", "bands_cm1": [143.0, 447.0, 612.0]},
            ]
        },
        {},
        "RRUFF",
    ),
    "structure": STRUCT,
    "polymorph": Fact(
        "p:1",
        "polymorph",
        ["Anatase", "Rutile"],
        "TiO2",
        {
            "members": [
                {
                    "species": "Anatase",
                    "crystal_system": "Tetragonal",
                    "space_group": "I4_1/amd",
                    "bands_cm1": [144.0, 397.0],
                },
                {
                    "species": "Rutile",
                    "crystal_system": "Tetragonal",
                    "space_group": "P4_2/mnm",
                    "bands_cm1": [143.0, 447.0],
                },
            ]
        },
        {},
        "x",
    ),
    "computed": Fact(
        "w:1",
        "computed",
        "Zircon",
        "ZrSiO4",
        {
            "modes": [
                {"cm1": 364.0, "irrep": "Eg", "rel": 0.4},
                {"cm1": 1012.0, "irrep": "B1g", "rel": 1.0},
            ],
            "strongest_cm1": 1012.0,
            "space_group": "I4_1/amd",
            "n_modes": 36,
        },
        {"source": "WURM"},
        "WURM",
    ),
    "libs_lines": Fact(
        "l:1",
        "libs_lines",
        "Aegirine",
        "NaFeSi2O6",
        {
            "temperature_k": 10000.0,
            "groups": [
                {
                    "stage_label": "Fe I",
                    "lines": [
                        {"nm": 248.33, "rel": 100.0, "ritz": False},
                        {"nm": 248.81, "rel": 68.1, "ritz": True},
                    ],
                }
            ],
        },
        {},
        "NIST",
    ),
    "libs_temperature": Fact(
        "lt:1",
        "libs_temperature",
        "Aegirine",
        "NaFeSi2O6",
        {
            "t_cool": 8000.0,
            "t_hot": 12000.0,
            "cool": [{"stage_label": "Fe I", "lines": [{"nm": 248.33, "rel": 100.0}]}],
            "hot": [{"stage_label": "Fe II", "lines": [{"nm": 259.94, "rel": 100.0}]}],
        },
        {},
        "NIST",
    ),
    "corroboration": Fact(
        "corr:1",
        "corroboration",
        "Actinolite",
        "x",
        {
            "reported": 672.0,
            "reference": 672.1,
            "tech": "Raman",
            "citation": "Characterization of wall paintings",
            "ref_sample": "R120010",
        },
        {"reference": {"source": "RRUFF", "derivation": "peak_pick"}},
        "x",
    ),
    "ice_bandlist": Fact(
        "ice:1",
        "ice_bandlist",
        "Cyanogen",
        "",
        {
            "phase": "Cyanogen",
            "classification": ["molecular solid", "nitrile"],
            "range_cm1": [40, 3300],
            "waveband": "FIR/MIR",
            "temperature_k": "60.0",
        },
        {},
        "SSHADE",
    ),
    "pack_fact": Fact(
        "pack:1",
        "pack_fact",
        [],
        "",
        {
            "title": "Calcium sulfate veins at Gale crater",
            "key": "sample count",
            "value": "12",
            "paper_key": "x",
        },
        {},
        "corpus",
    ),
}


def test_every_kind_has_enough_distinct_frames_and_a_probe_frame():
    for kind, fr in FRAMES.items():
        assert len(fr["statements"]) >= MIN_STATEMENTS, kind
        assert len(fr["questions"]) >= MIN_QUESTIONS, kind
        texts = [f.prompt + f.completion for f in fr["statements"] + fr["questions"]]
        assert len(set(texts)) == len(texts), kind
        assert (
            kind in PROBE_FRAMES
            and PROBE_FRAMES[kind] not in fr["statements"] + fr["questions"]
        ), kind


def test_every_frame_renders_and_keeps_every_number_verbatim():
    for kind, fact in ALL.items():
        f = fields(fact)
        if kind == "structure":
            f["sg_clause"] = " (space group P3_221)"
        for frame in (
            FRAMES[kind]["statements"]
            + FRAMES[kind]["questions"]
            + [PROBE_FRAMES[kind]]
        ):
            out = frame.render(f)
            assert out["text"] == out["prompt"] + out["completion"]
            assert out["completion"].strip(), (kind, frame)
            if kind in ("raman_bands", "ir_troughs"):
                key = "bands_cm1" if kind == "raman_bands" else "troughs_um"
                for v in fact.payload[key][:4]:
                    assert f"{float(v):g}" in out["text"], (kind, frame, v)


def test_frame_zero_reproduces_v3_wording():
    fwd = ic.view_forward(
        "Quartz",
        "SiO2",
        "Raman",
        [{"position_cm-1": v} for v in RAMAN.payload["bands_cm1"]],
        RAMAN.provenance,
    )["text"]
    assert render(RAMAN, FRAMES["raman_bands"]["statements"][0])["text"] == fwd
    inv = ic.view_inverse(
        "Quartz",
        "SiO2",
        "Raman",
        [{"position_cm-1": v} for v in RAMAN.payload["bands_cm1"]],
        RAMAN.provenance,
    )["text"]
    assert (
        render_kind(RAMAN, "inverse", FRAMES["inverse"]["statements"][0])["text"] == inv
    )
    st = ic.view_structure(
        "Quartz",
        "SiO2",
        {
            "crystal_system": "Trigonal",
            "a": 4.913,
            "c": 5.405,
            "strunz_class": "oxide",
            "density_calc": 2.66,
            "hardness_max": 7.0,
        },
        {
            "space_group_symbol": "P3_221",
            "coordination": {"Si": 4.0},
            "shortest_bond_a": 1.605,
            "shortest_bond_pair": "Si-O",
        },
    )["text"]
    assert render(STRUCT, FRAMES["structure"]["statements"][0])["text"] == st


def test_pick_frames_is_deterministic_distinct_and_fact_specific():
    a = pick_frames("raman:canonical:Quartz", "raman_bands", 4)
    b = pick_frames("raman:canonical:Quartz", "raman_bands", 4)
    c = pick_frames("raman:canonical:Calcite", "raman_bands", 4)
    assert a == b and len({i for i, _ in a}) == 4
    assert [i for i, _ in a] != [i for i, _ in c]
    assert all(
        fr in FRAMES["raman_bands"]["statements"]
        for _, fr in pick_frames("x", "raman_bands", 3, questions=False)
    )


def test_derived_positions_always_carry_provenance():
    for frame in (
        FRAMES["raman_bands"]["statements"] + FRAMES["raman_bands"]["questions"]
    ):
        assert "{how}" in frame.prompt + frame.completion
    for frame in FRAMES["ir_troughs"]["statements"] + FRAMES["ir_troughs"]["questions"]:
        assert "{how}" in frame.prompt + frame.completion
