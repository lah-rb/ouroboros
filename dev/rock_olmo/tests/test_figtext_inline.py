"""figtext_inline: the filters that decide which VLM readings train."""

from figtext_inline import (
    InlineStats,
    inline_figtext,
    is_junk,
    select_figs,
    strip_prompt_echo,
)

KEY = "doi_10.1000_x"
MD = (
    "# A paper\n\nIntro text about quartz.\n\n"
    f'<img src="../figures/{KEY}/fig_00.png" alt="">\n\nFigure 1. Raman spectra.\n\n'
    f'<img src="../figures/{KEY}/fig_01.png">\n\nMore prose.\n'
)


def test_prompt_echo_is_stripped_to_the_answer():
    text = "Describe the figure's DATA content … no preamble.\n\nThe plot shows bands at 464 cm-1."
    clean, stripped, dropped = strip_prompt_echo(text)
    assert clean == "The plot shows bands at 464 cm-1." and stripped and not dropped


def test_echo_without_an_answer_is_dropped():
    text = "CONTEXT ONLY — caption\n… no preamble.\n\nCONTEXT ONLY — again"
    clean, stripped, dropped = strip_prompt_echo(text)
    assert dropped and clean == ""


def test_junk_matches_identity_zone_only_and_ignores_maps():
    assert is_junk(
        "I cannot see a figure in the image; it is the first page of the paper."
    )
    assert is_junk("A journal logo with the publisher name.")
    assert not is_junk(
        "A geological map of the Gale crater region showing sample sites."
    )
    body = (
        "Raman spectrum of quartz with bands at 128, 206, 464 cm-1. " * 6
        + "The inset is a journal logo."
    )
    assert not is_junk(body)  # the junk word sits past the identity zone


def test_anchored_readings_come_first_and_caption_is_never_passed():
    figs = [
        {
            "fig": "fig_09.png",
            "caption": "orphan cap",
            "figtext": "Orphan reading of a spectrum.",
        },
        {
            "fig": "fig_01.png",
            "caption": "cap 1",
            "figtext": "Second anchored reading.",
        },
        {"fig": "fig_00.png", "caption": "cap 0", "figtext": "First anchored reading."},
    ]
    st = InlineStats()
    chosen = select_figs(MD, KEY, figs, cap_ratio=10.0, stats=st)
    assert [c["fig"] for c in chosen] == ["fig_00.png", "fig_01.png", "fig_09.png"]
    assert all(set(c) == {"fig", "figtext"} for c in chosen)
    assert st.anchored == 2 and st.orphans == 1 and st.inlined == 3


def test_cap_limits_figtext_relative_to_the_paper():
    figs = [{"fig": f"fig_{i:02d}.png", "figtext": "x" * 400} for i in range(20)]
    st = InlineStats()
    chosen = select_figs(MD, KEY, figs, cap_ratio=1.0, stats=st)
    assert 0 < len(chosen) < 20 and st.capped == 20 - len(chosen)


def test_inline_places_reading_in_anchor_paragraph_and_orphans_at_end():
    sidecar = {
        "paper_key": KEY,
        "figs": [
            {"fig": "fig_00.png", "figtext": "Bands at 464 cm-1."},
            {"fig": "fig_07.png", "figtext": "An unanchored reading."},
            {
                "fig": "fig_01.png",
                "figtext": "Describe the figure's DATA content no preamble.\n\nRuns at 77 K.",
            },
            {"fig": "fig_02.png", "figtext": "I cannot see a figure in the image."},
        ],
    }
    doc, st = inline_figtext(MD, KEY, sidecar)
    first = doc.split("\n\n")
    idx = next(i for i, p in enumerate(first) if "fig_00.png" in p and "<img" in p)
    # build_curator_doc appends the reading as the paragraph directly after
    # the anchor's paragraph (para + "\n\n" + block).
    assert first[idx + 1].startswith(
        "> [FIGURE fig_00.png — VLM reading]: Bands at 464 cm-1."
    )
    assert "Runs at 77 K." in doc and "no preamble" not in doc
    assert "## Unanchored figures" in doc and doc.index(
        "An unanchored reading"
    ) > doc.index("More prose")
    assert st.junk == 1 and st.echo_stripped == 1 and st.inlined == 3


def test_no_sidecar_is_identity():
    doc, st = inline_figtext(MD, KEY, None)
    assert doc == MD and st.inlined == 0
