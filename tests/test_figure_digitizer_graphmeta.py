"""The figtext prefilter: cheap, and wrong in the cheap direction.

A false positive costs one CV pass on a figure that then fails axis
detection. A false negative silently drops a real spectrum from the corpus
and nothing downstream can notice. So the filter is deliberately permissive,
and the tests here mostly pin the ways it must NOT be strict.
"""

from __future__ import annotations

import json

from tools.figure_digitizer import graphmeta as G


def _corpus(tmp_path, key: str, figs: list[dict]) -> str:
    d = tmp_path / "databank" / "figtext"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{key}.json").write_text(
        json.dumps({"paper_key": key, "figs": figs}), encoding="utf-8"
    )
    return str(tmp_path)


def test_a_libs_spectrum_plot_is_a_candidate(tmp_path):
    root = _corpus(
        tmp_path,
        "p",
        [
            {
                "fig": "fig_01.png",
                "caption": "Fig. 2 LIBS spectrum of the sample",
                "figtext": (
                    "A line plot. The x-axis shows wavelength in nm from 380 "
                    "to 450 with tick marks every 10 nm. The y-axis shows "
                    "intensity in arbitrary units. Several sharp peaks."
                ),
            }
        ],
    )
    (h,) = G.hints(root, "p")
    assert h.is_candidate
    assert h.technique == "libs"
    assert h.reject_reason is None


def test_a_micrograph_is_not_a_candidate(tmp_path):
    root = _corpus(
        tmp_path,
        "p",
        [
            {
                "fig": "fig_00.png",
                "caption": "Fig. 1 SEM image of the grain surface",
                "figtext": (
                    "An SEM image of a mineral grain at 500x. A scale bar "
                    "reads 20 nm. No axes are present."
                ),
            }
        ],
    )
    (h,) = G.hints(root, "p")
    assert not h.is_candidate
    assert h.reject_reason == "not_a_plot"


def test_a_spectrum_is_not_killed_by_a_micrograph_mentioned_in_passing(tmp_path):
    """CAUGHT LIVE, on 1,929 corpus figures. Vetoing on the whole description
    let one passing word decide: 'LIBS/ChemCam targets displaying Ca-sulfate
    signature (solid spectra)' was rejected because 'micrograph' appeared
    later in the same paragraph. Only the opening states what the image IS."""
    root = _corpus(
        tmp_path,
        "p",
        [
            {
                "fig": "fig_03.png",
                "caption": "Figure 4. LIBS spectra of three targets",
                "figtext": (
                    "The figure plots emission intensity against wavelength "
                    "in nm, with the x-axis running 200-900 and tick marks "
                    "labelled every 100. The regions sampled are the same "
                    "ones shown in the micrograph of Figure 2, and a "
                    "photograph of the setup appears in Figure 1."
                ),
            }
        ],
    )
    (h,) = G.hints(root, "p")
    assert h.is_candidate, "a passing mention must not veto a real spectrum plot"
    assert h.technique == "libs"


def test_techniques_are_told_apart(tmp_path):
    root = _corpus(
        tmp_path,
        "p",
        [
            {
                "fig": "a.png",
                "caption": "Raman spectrum",
                "figtext": "Intensity plotted against Raman shift (cm-1); the "
                "x-axis is labelled with tick marks from 200 to 1200.",
            },
            {
                "fig": "b.png",
                "caption": "XRD pattern",
                "figtext": "Counts plotted against 2 theta, with the x-axis "
                "carrying tick marks and several sharp peaks.",
            },
        ],
    )
    got = {h.fig: h.technique for h in G.hints(root, "p")}
    assert got == {"a.png": "raman", "b.png": "xrd"}


def test_a_plot_without_a_spectral_unit_is_refused(tmp_path):
    """A bar chart of concentrations is a plot, but nothing here can
    calibrate it into a spectrum."""
    root = _corpus(
        tmp_path,
        "p",
        [
            {
                "fig": "c.png",
                "caption": "Fig. 5 Sample composition",
                "figtext": "A bar chart. The y-axis shows intensity in counts "
                "and the x-axis lists sample names with tick marks.",
            }
        ],
    )
    (h,) = G.hints(root, "p")
    assert not h.is_candidate
    assert h.reject_reason == "no_spectral_unit"


def test_a_paper_with_no_figtext_yields_nothing(tmp_path):
    assert G.hints(str(tmp_path), "absent") == []


# ── The structured ask ────────────────────────────────────────────────


def test_a_json_reply_is_parsed_even_when_wrapped():
    """Models fence their JSON or chat around it. The object is what matters."""
    reply = (
        "Sure, here is the result:\n```json\n"
        '{"is_plot": true, "x_unit": "nm", "x_tick_labels": [200, 400, 600],'
        ' "y_tick_labels": [0, 1, 2], "y_direction": "up"}\n```\nHope that helps!'
    )
    got = G.parse_structured(reply)
    assert got["is_plot"] is True
    assert got["x_unit"] == "nm"
    assert got["x_tick_labels"] == [200.0, 400.0, 600.0]


def test_labels_that_are_strings_or_carry_a_unicode_minus_still_parse():
    reply = '{"x_tick_labels": ["200", "−100", "1,000", "n/a"], "y_tick_labels": []}'
    got = G.parse_structured(reply)
    assert got["x_tick_labels"] == [200.0, -100.0, 1000.0]


def test_an_unparseable_reply_is_a_result_not_a_crash():
    assert G.parse_structured("I cannot read this figure.") is None
    assert G.parse_structured("") is None
    assert G.parse_structured("{not json}") is None


def test_the_prompt_states_no_bare_count():
    """A number the task can contradict outranks the prose beside it: the
    model optimises the number instead of the job. The ask must never tell it
    how many ticks to find."""
    import re as _re

    # No digit-bearing quantity phrases like "5 to 9" or "at least 3".
    assert not _re.search(r"\b\d+\s*(?:-|–|to)\s*\d+\b", G.STRUCTURED_PROMPT)
    assert not _re.search(
        r"\b(?:at least|at most|exactly|about)\s+\d+", G.STRUCTURED_PROMPT
    )


def test_the_ask_requests_the_exponent_and_the_axis_direction():
    """Both are silent corruptions if missed: a dropped 1e4 multiplier is a
    10,000x error, and a mis-read direction inverts the whole axis."""
    assert "y_exponent" in G.STRUCTURED_PROMPT
    assert "y_direction" in G.STRUCTURED_PROMPT
    assert "axes_terminate_at_range" in G.STRUCTURED_PROMPT
