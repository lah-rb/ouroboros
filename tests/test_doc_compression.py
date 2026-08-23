"""doc_compression: the ladder, its invariants, and the wiring seam."""

import pytest

from agent.actions.doc_compression import (
    LADDER,
    compress_rung,
    fig_tag_count,
    html_tables_to_pipes,
    speedread,
)

_STYLED_TABLE = (
    "<table border=1 style='margin: auto;'><tr>"
    "<td style='text-align: center; word-wrap: break-word;'>Bt</td>"
    "<td style='text-align: center; word-wrap: break-word;'>Ch</td></tr>"
    "<tr><td style='text-align: center;'>1.2</td>"
    "<td style='text-align: center;'>3.4</td></tr></table>"
)

_PROSE = " ".join(
    f"Sentence number {i} carries a measured value of {i}.{i} eV." for i in range(12)
)

_DOC = f"""# Introduction

{_PROSE}

{_PROSE}

{_PROSE}

## Data

{_STYLED_TABLE}

![](databank/figures/key/fig_00.png)

Paragraph anchoring a figure <img src="fig_01.png"> stays whole. {_PROSE}

# References

""" + "\n\n".join(
    f"Author {i}, Title {i}, Journal, v. {i}, p. {i}-{i+9}." for i in range(12)
)


def test_table_conversion_is_lossless_on_cells():
    out = html_tables_to_pipes(_STYLED_TABLE)
    assert "<table" not in out and "style=" not in out
    for cell in ("Bt", "Ch", "1.2", "3.4"):
        assert cell in out
    assert len(out) < len(_STYLED_TABLE) / 2


def test_every_rung_preserves_figure_anchors():
    n = fig_tag_count(_DOC)
    assert n == 2
    for rung in LADDER:
        assert fig_tag_count(compress_rung(_DOC, rung)) == n, rung


def test_figure_anchored_paragraph_is_never_squeezed():
    out = compress_rung(_DOC, "full")
    assert 'Paragraph anchoring a figure <img src="fig_01.png"> stays whole.' in out


def test_ladder_is_monotone_and_marks_elisions():
    sizes = [len(compress_rung(_DOC, r)) for r in LADDER]
    assert sizes[0] >= sizes[1] >= sizes[2] >= sizes[3]
    assert sizes[3] < sizes[0]
    assert "elided" in compress_rung(_DOC, "full")


def test_first_and_last_section_paragraphs_survive_whole():
    out = speedread(_DOC, gentle=False)
    # first prose paragraph of Introduction kept verbatim
    assert _PROSE in out


def test_short_entry_runs_collapse_with_count():
    out = compress_rung(_DOC, "full")
    assert "similar short entries elided" in out
    # edges of the run survive
    assert "Author 0, Title 0" in out
    assert "Author 11, Title 11" in out


def test_tables_and_headings_survive_full():
    out = compress_rung(_DOC, "full")
    assert "# Introduction" in out and "## Data" in out and "# References" in out
    assert "| Bt | Ch |" in out


@pytest.mark.asyncio
async def test_build_doc_for_compresses_only_past_budget(tmp_path):
    from agent.actions import curation_actions as ca

    class _FC:
        def __init__(self, content):
            self.exists = content is not None
            self.content = content or ""

    class _Fx:
        working_directory = str(tmp_path)

        async def read_file(self, path):
            if path.endswith(".en.md"):
                return _FC(None)
            if path.endswith(f"{ca.FIGTEXT_DIR}/k.json"):
                return _FC(None)
            return _FC(_DOC)

    fx = _Fx()
    raw = await ca._build_doc_for(fx, "k", budget_chars=10**9)
    assert ca._DOC_FORMS["k"] == "raw"
    assert _PROSE in raw
    small = await ca._build_doc_for(fx, "k", budget_chars=len(raw) - 1)
    assert ca._DOC_FORMS["k"] in ("tables", "gentle", "full")
    assert len(small) < len(raw)
    assert fig_tag_count(small) == fig_tag_count(raw)
    ca._DOC_FORMS.pop("k", None)


def test_figure_tags_inside_table_cells_survive_conversion():
    # Corpus-found (2026-08-23): 57 pending docs anchor figures inside
    # HTML table cells; the cell cleaner must strip styling, not anchors.
    table = (
        "<table><tr><td style='x'>caption</td>"
        "<td style='x'><img src=\"databank/figures/k/fig_03.png\"></td></tr></table>"
    )
    out = html_tables_to_pipes(table)
    assert '<img src="databank/figures/k/fig_03.png">' in out
    assert "style=" not in out
    assert fig_tag_count(out) == fig_tag_count(table)
