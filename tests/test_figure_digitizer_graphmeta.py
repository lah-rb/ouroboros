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
