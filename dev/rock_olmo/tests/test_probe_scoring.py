"""probe_recall's scorers and prompt builders (pure)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from probe_scoring import (  # noqa: E402
    cross_modal_prompt,
    identification_prompt,
    libs_inverse_prompt,
    score,
    strongest_lines,
)

ITEM = {
    "species": "Quartz",
    "formula": "SiO2",
    "system": "trigonal",
    "bands": [128.0, 206.3, 464.8],
    "libs_top3": [288.16, 251.61, 777.19],
}


def test_scorers():
    assert score("formula", ITEM, " Si O2 is the formula")
    assert not score("formula", ITEM, " SiO3")
    assert score("crystal_system", ITEM, "Trigonal, space group P3_221")
    assert score("bands", ITEM, " 127.5, 210.0, 465 cm-1")
    assert not score("bands", ITEM, " 127.5 and 900 cm-1")
    assert score("libs_lines", ITEM, "Si I at 288.10 nm (100), 251.70 nm (60)")
    assert not score("libs_lines", ITEM, "Si I at 288.5 nm and 250.0 nm")
    for t in ("inverse", "identification", "libs_inverse", "cross_modal"):
        assert score(t, ITEM, " quartz (SiO2).") and not score(t, ITEM, " coesite")
    assert not score("unknown_task", ITEM, "anything")


def test_prompts_and_strongest_lines():
    groups = [
        {
            "stage_label": "Si I",
            "lines": [{"nm": 288.16, "rel": 100.0}, {"nm": 251.61, "rel": 60.0}],
        },
        {
            "stage_label": "O I",
            "lines": [{"nm": 777.19, "rel": 30.0}, {"nm": 844.6, "rel": 5.0}],
        },
    ]
    assert strongest_lines(groups) == [288.16, 251.61, 777.19]
    p = identification_prompt([(128.0, 0.31), (464.8, 1.0)], "532")
    assert p.startswith("Raman peak list (532 nm excitation; 2 peaks") and p.endswith(
        "\nPhase:"
    )
    assert "128.0 (0.31), 464.8 (1.00)" in p
    assert "excitation unknown" in identification_prompt([(464.8, 1.0)], "")
    assert (
        libs_inverse_prompt([288.16, 251.61])
        == "LIBS emission lines at 288.16, 251.61 nm point to the mineral"
    )
    cm = cross_modal_prompt([464.8, 206.3, 128.0], [288.16, 251.61, 777.19])
    assert cm.startswith(
        "Raman bands at 464.8, 206.3, 128 cm-1; LIBS lines at 288.16"
    ) and cm.endswith("the mineral is")
