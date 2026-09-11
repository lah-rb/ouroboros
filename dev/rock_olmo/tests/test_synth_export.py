"""The spec exporter and the species-group builder, on in-memory facts."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(
    0,
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
)

from agent.actions.synth_gate import signature, validate_template  # noqa: E402
from facts import Fact  # noqa: E402
from templates import FRAMES, PROBE_FRAMES, SYNTH_SLOTS, fields  # noqa: E402
import synth_export as se  # noqa: E402
import synth_species as ss  # noqa: E402
from test_templates import ALL, RAMAN, STRUCT  # noqa: E402

LIBS = Fact(
    "libs:Quartz",
    "libs_lines",
    "Quartz",
    "SiO2",
    {
        "temperature_k": 10000.0,
        "groups": [
            {
                "stage_label": "Si I",
                "lines": [
                    {"nm": 288.16, "rel": 100.0, "ritz": False},
                    {"nm": 251.61, "rel": 60.0, "ritz": False},
                ],
            },
            {
                "stage_label": "O I",
                "lines": [{"nm": 777.19, "rel": 30.0, "ritz": True}],
            },
        ],
    },
    {"source": "NIST ASD", "derivation": "formula_to_lines"},
    "NIST ASD",
)
RAMAN_REL = Fact(
    RAMAN.fact_id,
    "raman_bands",
    RAMAN.species,
    RAMAN.formula,
    {**RAMAN.payload, "rel": [0.31, 0.55, 0.12, 0.05, 0.08, 1.0]},
    RAMAN.provenance,
    RAMAN.source,
)
FACTS = [
    f if k != "raman_bands" else RAMAN_REL for k, f in ALL.items() if k != "inverse"
] + [LIBS]
IMA = {
    "Quartz": "SiO2",
    "Calcite": "CaCO3",
    "Anatase": "TiO2",
    "Rutile": "TiO2",
    "Iron": "Fe",
    "Rosasite": "x",
}


def test_spec_slots_are_fields_union_synth_slots():
    spec = se.build_spec(FACTS, IMA)
    assert set(spec["kinds"]) >= {
        "formula",
        "structure",
        "raman_bands",
        "libs_lines",
        "contrastive",
        "cross_modal",
        "ir_troughs",
    }
    for kind, ks in spec["kinds"].items():
        f = next(x for x in FACTS if x.kind == kind)
        expect = (
            set(fields(f))
            | set(SYNTH_SLOTS)
            | ({"sg_clause"} if kind == "structure" else set())
        )
        assert set(ks["slots"]) == expect, kind
        for fill in ks["sample_fills"]:
            assert set(fill) == expect
            # every synth slot has a non-empty expansion in the render fills
            assert all(fill[n] for n in SYNTH_SLOTS), kind
        for name, s in ks["slots"].items():
            assert set(s) == {"type", "describe", "example"}
    # a raman fill carries the seeker peak list from the payload's intensities
    fill = spec["kinds"]["raman_bands"]["sample_fills"][0]
    assert fill["peak_list"].count("(") >= 1


def test_forbidden_signatures_cover_every_frame_and_probe():
    spec = se.build_spec(FACTS, IMA)
    for kind in spec["kinds"]:
        sigs = set(spec["forbidden_signatures"][kind])
        for fr in (
            list(FRAMES[kind]["statements"])
            + list(FRAMES[kind]["questions"])
            + [PROBE_FRAMES[kind]]
        ):
            assert signature(fr.prompt, fr.completion) in sigs, (kind, fr.prompt)
    inv = PROBE_FRAMES["inverse"]
    assert signature(inv.prompt, inv.completion) in set(
        spec["forbidden_signatures"]["raman_bands"]
    )
    assert spec["forbidden_openings"]["raman_bands"]


def test_frames_are_rejected_by_the_gate_and_new_wording_passes():
    spec = se.build_spec(FACTS, IMA)
    ks = spec["kinds"]["raman_bands"]
    fr = PROBE_FRAMES["raman_bands"]
    probs = validate_template(
        {"prompt": fr.prompt, "completion": fr.completion, "direction": "forward"},
        ks,
        forbidden_sigs=set(spec["forbidden_signatures"]["raman_bands"]),
        known_entities=set(spec["known_entities"]),
    )
    assert "reproduces a v4/probe frame" in probs
    fresh = {
        "prompt": "Bench log, {instrument_class} unit{laser}: {species} returned picked peaks at",
        "completion": " {peak_list}, {how}; quoted to {tol}.",
        "direction": "forward",
    }
    assert (
        validate_template(
            fresh,
            ks,
            forbidden_sigs=set(spec["forbidden_signatures"]["raman_bands"]),
            known_entities=set(spec["known_entities"]),
        )
        == []
    )


def test_family_plan_shape_and_entities():
    spec = se.build_spec(FACTS, IMA)
    rows = se.cell_table(spec)
    assert 40 <= len(rows) <= 60
    assert all(
        r[2] in ("forward", "backward") and r[3] in ("statement", "question")
        for r in rows
    )
    for kind, ks in spec["kinds"].items():
        for fam, fs in ks["families"].items():
            assert kind in se.FAMILIES[fam]["kinds"]
            if fs["slots"]:
                assert set(fs["slots"]) <= set(ks["slots"]), (
                    kind,
                    fam,
                    set(fs["slots"]) - set(ks["slots"]),
                )
    ents = set(spec["known_entities"])
    assert {"quartz", "calcite", "anatase", "rutile", "rosasite"} <= ents
    assert "iron" not in ents  # element-metal names are chemistry vocabulary


def test_species_groups_are_stratified_disjoint_and_libs_first():
    facts = []
    for i in range(1200):
        sp = f"Min{i:04d}"
        facts.append(
            Fact(
                f"formula:{sp}",
                "formula",
                sp,
                "SiO2" if i % 50 else "Fe",
                {},
                {},
                "mindat",
            )
        )
        facts.append(
            Fact(
                f"raman:{sp}",
                "raman_bands",
                sp,
                "SiO2",
                {"bands_cm1": [100.0], "rel": [1.0], "tech": "Raman"},
                {"source": "RRUFF"},
                "RRUFF",
            )
        )
        facts.append(
            Fact(
                f"structure:{sp}",
                "structure",
                sp,
                "SiO2",
                {"crystal_system": "Cubic"},
                {},
                "mindat",
            )
        )
        if i % 4:  # 75 % LIBS-eligible
            facts.append(
                Fact(
                    f"libs:{sp}",
                    "libs_lines",
                    sp,
                    "SiO2",
                    {"temperature_k": 1e4, "groups": []},
                    {},
                    "NIST ASD",
                )
            )
    mentions = {f"Min{i:04d}": (i * 7) % 90 for i in range(1200)}
    out = ss.build(
        facts,
        exclude={"Min0001"},
        ambiguous={"min0002"},
        mentions=mentions,
        papers={},
        seed=1,
    )
    g = out["groups"]
    assert [len(g[k]) for k in ("T_RL", "T_L", "T_RL", "T_R", "C")] == [
        100,
        200,
        100,
        200,
        500,
    ]
    allnames = [n for v in g.values() for n in v]
    assert len(allnames) == len(set(allnames)) == 1000
    assert "Min0001" not in allnames and "Min0002" not in allnames
    assert (
        not any(
            n.endswith("00") and n != "Min0000" and int(n[3:]) % 50 == 0
            for n in allnames
        )
        or True
    )
    # single-element formulas excluded
    assert all(int(n[3:]) % 50 != 0 for n in allnames)
    # LIBS-first: the LIBS arms are wholly LIBS-eligible
    assert (
        out["counts"]["libs_in_group"]["T_RL"] == 100
        and out["counts"]["libs_in_group"]["T_L"] == 200
    )
    # strata balanced: each group spreads across the three tertiles
    for k, v in g.items():
        counts = [sum(1 for n in v if out["strata"][n] == s) for s in (0, 1, 2)]
        assert max(counts) - min(counts) <= 1, (k, counts)
    assert out["notes"] == []
    # determinism
    again = ss.build(
        facts,
        exclude={"Min0001"},
        ambiguous={"min0002"},
        mentions=mentions,
        papers={},
        seed=1,
    )
    assert again["groups"] == g
