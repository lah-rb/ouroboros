"""The truth oracle must not charge an extraction for its own correctness.

A blind audit of 48 extractions (2026-08-14) found the numeric rate
UNCORRELATED with judged document quality — r = +0.02 — because the "truth" it
compared against contained numbers no faithful extraction would ever reproduce:

  * the MARGIN LINE NUMBERS of a line-numbered manuscript, which PyMuPDF's
    block reader merges into the prose line beside them (~40 phantom misses per
    page; 12 of 12 affected papers were failing the gate, median numeric
    0.528 -> 0.977);
  * PUBLISHER FURNITURE — the access stamp injected at download time, the
    DOI/ISSN, the copyright footer. A fixed cost against a small denominator,
    so it sank SHORT faithful papers hardest: one paper judged fully faithful
    lost 47 of its 47 numbers to a download stamp and scored 0.736.

Plus the defect neither rate can see at all, because both are RECALL: a decode
that falls into a loop keeps every number and scores clean.

Full record: dev/EXTRACTION_GATE_CALIBRATION_2026-08-14.md
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

fitz = pytest.importorskip("fitz")

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "pdf_extract",
    "extract_batch.py",
)


def _load_tool():
    spec = importlib.util.spec_from_file_location("_extract_batch_oracle", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


TOOL = _load_tool()

_PROSE = [
    "Raman spectra were collected at 532 nm with a 50x objective.",
    "The band at 1085 cm-1 is assigned to the symmetric stretch.",
    "Samples were annealed at 873 K for 30 min before measurement.",
    "Peak areas were integrated between 1050 and 1120 wavenumbers.",
    "Reported uncertainties are one standard deviation of 12 replicates.",
]


def _make_pdf(tmp_path, *, line_numbers=False, furniture="", name="t.pdf"):
    """A one-page paper, optionally line-numbered, optionally footed."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 100.0
    for i, line in enumerate(_PROSE, start=1):
        if line_numbers:
            # x=30 is the margin: left of the prose column, which starts at 72.
            page.insert_text((30, y), str(i), fontsize=9)
        page.insert_text((72, y), line, fontsize=10)
        y += 24
    if furniture:
        page.insert_text((72, y + 40), furniture, fontsize=8)
    path = os.path.join(str(tmp_path), name)
    doc.save(path)
    doc.close()
    return path


def _numbers(text):
    return TOOL._NUM_RE.findall(TOOL._norm(text))


# ── margin line numbers ───────────────────────────────────────────────


def test_margin_line_numbers_are_not_truth(tmp_path):
    """The line-number column must not enter the truth, and the prose it was
    merged with must survive intact."""
    plain = _make_pdf(tmp_path, name="plain.pdf")
    numbered = _make_pdf(tmp_path, line_numbers=True, name="numbered.pdf")

    t_plain = TOOL._prose_text(fitz.open(plain)[0])
    t_numbered = TOOL._prose_text(fitz.open(numbered)[0])

    # Every sentence still present — we dropped a number, not a line.
    for line in _PROSE:
        assert line.split(" were ")[0][:24] in t_numbered

    # And the two documents now offer the SAME numbers to verify against.
    assert _numbers(t_numbered) == _numbers(t_plain)


def test_line_numbers_no_longer_sink_a_faithful_extraction(tmp_path):
    """End to end: markdown holding every real number, and no line numbers,
    scores 1.00 — the case that was scoring ~0.5 and failing the gate."""
    numbered = _make_pdf(tmp_path, line_numbers=True, name="n.pdf")
    page = fitz.open(numbered)[0]
    truth = TOOL._prose_text(page)
    faithful_md = "\n".join(_PROSE)  # what a correct extraction produces

    hit, total, _, _ = TOOL._verify_page(faithful_md, truth)
    assert total > 0, "nothing checkable means the test proves nothing"
    assert hit == total, f"faithful extraction scored {hit}/{total}"


def test_gutter_detection_refuses_without_a_prose_column(tmp_path):
    """A page that is almost entirely numbers has no column to measure a
    margin against — the leftmost data column would look exactly like a
    line-number gutter. Guessing there would delete the data being verified,
    so the detector must refuse rather than guess.

    Asserted on _gutter_spans directly: _prose_text also runs the older
    short-digit-block filter, and a test that went through it would be
    reporting on that filter instead of this one."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 100.0
    for row in (("1273", "0.48", "12.7"), ("1373", "0.51", "13.9")):
        for x, cell in zip((30, 150, 260), row):
            page.insert_text((x, y), cell, fontsize=10)
        y += 20
    path = os.path.join(str(tmp_path), "table.pdf")
    doc.save(path)
    doc.close()

    assert TOOL._gutter_spans(fitz.open(path)[0]) == set()


def test_gutter_detection_finds_the_margin_column(tmp_path):
    """The positive case, so the refusal above cannot pass vacuously."""
    numbered = _make_pdf(tmp_path, line_numbers=True, name="pos.pdf")
    assert len(TOOL._gutter_spans(fitz.open(numbered)[0])) == len(_PROSE)


@pytest.mark.parametrize("stamp", ["folio", "rotated"])
def test_one_margin_item_cannot_widen_the_column(tmp_path, stamp):
    """2026-10-04: the column ran from the min to the max x of EVERY non-digit
    span, so a single corner folio or a rotated "Accepted Manuscript" stamp
    stretched it to the page edge and let every line number back in — the
    author manuscripts of the 2026-10-03 OCR run failed on exactly that."""
    path = _make_pdf(tmp_path, line_numbers=True, name="s.pdf")
    doc = fitz.open(path)
    page = doc[0]
    if stamp == "folio":
        page.insert_text((8, 40), "Page 1 of 23", fontsize=8)
    else:
        page.insert_text((14, 700), "Accepted Manuscript", fontsize=12, rotate=90)
    doc.saveIncr()
    page = fitz.open(path)[0]
    assert len(TOOL._gutter_spans(page)) >= len(_PROSE)
    # The line numbers are gone; the folio's own "23" is the layout labels'
    # job (_ignored_regions), not the gutter's.
    truth = [n for n in _numbers(TOOL._prose_text(page)) if n != "23"]
    plain = TOOL._prose_text(fitz.open(_make_pdf(tmp_path, name="p.pdf"))[0])
    assert truth == _numbers(plain)


# ── what the text layer flattens or the pipeline drops ────────────────


def test_superscripts_are_not_truth(tmp_path):
    """The text layer flattens 10^16 into "1016"; the markdown says 10^{16}.
    The exponent is not a number the paper printed."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_htmlbox(
        fitz.Rect(72, 100, 500, 140),
        "The density rises to 10<sup>16</sup> cm-3 at 873 K.",
    )
    path = os.path.join(str(tmp_path), "sup.pdf")
    doc.save(path)
    truth = TOOL._prose_text(fitz.open(path)[0])
    assert "1016" not in _numbers(truth)
    assert "873" in _numbers(truth)
    md = "The density rises to $10^{16}$ cm-3 at 873 K."
    hit, total, _, _ = TOOL._verify_page(md + " " * 200, truth)
    assert hit == total > 0


def test_diagonal_watermark_is_not_truth(tmp_path):
    path = _make_pdf(tmp_path, name="w.pdf")
    doc = fitz.open(path)
    doc[0].insert_text(
        (150, 600),
        "Preprint not peer reviewed 2024",
        fontsize=30,
        morph=(fitz.Point(150, 600), fitz.Matrix(45)),
    )
    doc.saveIncr()
    truth = TOOL._norm(TOOL._prose_text(fitz.open(path)[0]))
    assert "preprint" not in truth and "2024" not in truth
    assert "1085" in truth


def test_blocks_the_pipeline_leaves_out_are_not_truth(tmp_path):
    """A footnote the region pipeline drops by its markdown_ignore_labels must
    not be charged as a miss — and nothing outside the excluded block moves."""
    path = _make_pdf(tmp_path, furniture="1 Smith 2019, p. 931; consulted 11 May 2025.")
    page = fitz.open(path)[0]
    full = _numbers(TOOL._prose_text(page))
    assert "931" in full
    foot = [b for b in page.get_text("blocks") if "Smith" in b[4]][0]
    rect = [foot[0] - 1, foot[1] - 1, foot[2] + 1, foot[3] + 1, "footnote"]
    kept = _numbers(TOOL._prose_text(page, exclude=[rect]))
    assert "931" not in kept and "2025" not in kept
    assert [n for n in full if n not in ("2019", "931", "11", "2025")] == kept


def test_ignored_regions_round_trip_with_segment_parts(tmp_path):
    """The sidecar is per document or per book segment; the reader merges."""
    import json as _json

    db = str(tmp_path)
    for rng, page in ((None, 0), ((10, 20), 12), ((20, 30), 25)):
        p = TOOL.ignored_regions_path(db, "k", rng)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            _json.dump({"pages": {str(page): [[1, 2, 3, 4, "number"]]}}, fh)
    other = TOOL.ignored_regions_path(db, "k2")
    with open(other, "w") as fh:
        _json.dump({"pages": {"0": [[9, 9, 9, 9, "header"]]}}, fh)
    got = TOOL.load_ignored_regions(db, "k")
    assert sorted(got) == [0, 12, 25]
    assert TOOL.load_ignored_regions(db, "missing") == {}


def test_ignored_regions_reads_the_pipeline_result_shape():
    class Blk:
        def __init__(self, label, bbox):
            self.label, self.bbox = label, bbox

    res = {
        "parsing_res_list": [
            Blk("text", [0, 0, 160, 160]),
            Blk("footnote", [0, 1600, 320, 1760]),
            Blk("number", [600, 1800, 640, 1820]),
        ]
    }
    got = TOOL._ignored_regions(res, TOOL._IGNORE_LABELS_DEFAULT, 160)
    assert [r[4] for r in got] == ["footnote", "number"]
    assert got[0][:4] == [0.0, 720.0, 144.0, 792.0]  # px at 160 dpi -> pt
    assert TOOL._ignored_regions(object(), TOOL._IGNORE_LABELS_DEFAULT, 160) == []


# ── publisher furniture ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "footer",
    [
        "This content was downloaded from IP address 216.71.8.37 on 11/08/2026",
        "https://doi.org/10.1016/j.heliyon.2019.e01505",
        "ISSN 2405-8440 / (c) 2019 Published by Elsevier Ltd",
        "0168-583X/$ - see front matter (c) 2013 Elsevier B.V. All rights reserved.",
        "Tel.: +351 219946065; fax: +351 219946285",
    ],
)
def test_publisher_furniture_is_not_truth(tmp_path, footer):
    pdf = _make_pdf(tmp_path, furniture=footer, name="f.pdf")
    truth = TOOL._prose_text(fitz.open(pdf)[0])
    assert TOOL._norm(footer)[:30] not in TOOL._norm(truth)
    # The paper's own numbers are untouched.
    assert "1085" in truth and "873" in truth


def test_furniture_filter_does_not_eat_body_prose(tmp_path):
    """The pattern that first exposed this matched a bare 'licence' and
    'email', which a Methods section legitimately uses. Narrow it or it
    deletes real content — silently, and in the direction that inflates the
    score."""
    body = [
        "Data are available under a licence from the depositing institution.",
        "Correspondence and requests for materials should be addressed by email.",
        "The fax machine recorded 42 transmissions during the campaign.",
    ]
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 100.0
    for line in body:
        page.insert_text((72, y), line, fontsize=10)
        y += 24
    path = os.path.join(str(tmp_path), "body.pdf")
    doc.save(path)
    doc.close()

    truth = TOOL._norm(TOOL._prose_text(fitz.open(path)[0]))
    for line in body:
        assert TOOL._norm(line)[:34] in truth, f"body prose deleted: {line}"


# ── degenerate decode ─────────────────────────────────────────────────


def test_max_repeat_words_separates_a_loop_from_a_paper():
    """Measured separation on the audit sample: 19 words was the worst repeat
    across every paper judged fit to train; the two degenerate documents
    scored 675 and 1598, and the worst NON-degenerate defect scored 107."""
    clean = " ".join(_PROSE * 3)  # distinct sentences, some shared words
    assert TOOL._max_repeat_words(clean) < 200

    looped = "NGO-PEG per 100 uL of " * 120
    assert TOOL._max_repeat_words(looped) > 200


def test_max_repeat_words_ignores_an_incidental_echo():
    """Requiring L >= p stops one coincidental match registering as a loop —
    a paper may legitimately repeat a phrase twice."""
    text = (
        "the sample was heated to 873 K and held for 30 min . "
        "the sample was heated to 873 K and held for 30 min . "
        "subsequent analysis used a different protocol entirely ."
    )
    assert TOOL._max_repeat_words(text) < 200


def test_max_repeat_words_survives_a_short_document():
    assert TOOL._max_repeat_words("") == 0
    assert TOOL._max_repeat_words("one two three") == 0
