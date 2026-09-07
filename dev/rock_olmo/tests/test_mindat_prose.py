"""mindat prose fields -> one paragraph per species."""

import os

from mindat_prose import GEO, iter_paragraphs, paragraph


def test_paragraph_binds_name_to_each_field_and_cleans_html():
    rec = {
        "name": "Abelsonite",
        "description_short": "A nickel <b>porphyrin</b> mineral",
        "aboutname": "Named after Philip H. Abelson, geochemist",
        "occurrence": "In oil shale of the Green River Formation",
        "opticalr": "x",  # too short — skipped
    }
    t = paragraph(rec)
    assert t.startswith(
        "Abelsonite: A nickel porphyrin mineral. Name: Named after Philip H. Abelson, geochemist. Occurrence: In oil shale"
    )
    assert "<b>" not in t and "Optical" not in t


def test_empty_record_yields_nothing():
    assert paragraph({"name": "X", "description_short": "short"}) == ""


def test_iter_reads_the_mirror_if_present():
    if not os.path.exists(GEO):
        return
    it = iter_paragraphs()
    rows = [next(it) for _ in range(5)]
    assert all(name and text.startswith(f"{name}:") for name, text, _ in rows)
