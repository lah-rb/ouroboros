"""Supplement children in the stage-1 renderer: label, provenance, val split."""

import corpus_stage1
import emit
from corpus_stage1 import REPEATS, is_val, record, render_papers


def test_record_takes_its_val_split_from_val_key():
    a = record(
        "paper:child",
        "supplement_markdown",
        "x" * 400,
        "cc-by",
        {},
        val_key="paper:parent",
    )
    assert a["val"] == is_val("paper:parent")
    assert a["max_repeats"] == REPEATS["supplement_markdown"] == 1
    # default: the doc_id itself
    b = record("paper:child", "paper_markdown", "x" * 400, "cc-by", {})
    assert b["val"] == is_val("paper:child")


def test_render_papers_labels_a_supplement_child(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "binder_keys", lambda *a, **k: set())
    pmd = tmp_path / "parent.md"
    pmd.write_text("Parent text about calcite at 1085 cm-1.\n" * 20)
    cmd = tmp_path / "child.md"
    cmd.write_text("| band | assignment |\n|---|---|\n| 1085 | v1 CO3 |\n" * 20)
    db = {
        "doi_p": {
            "paper_key": "doi_p",
            "doi": "10.1000/p",
            "review_status": "accepted",
            "md_path": str(pmd),
            "license": "cc-by",
        },
        "doi_p__supp01": {
            "paper_key": "doi_p__supp01",
            "record_kind": "supplement",
            "supplement_of": "doi_p",
            "supplement_form": "si_docx",
            "parent_doi": "10.1000/p",
            "review_status": "accepted",
            "md_path": str(cmd),
            "license": "cc-by",
        },
        "doi_denied__supp01": {
            "paper_key": "doi_denied__supp01",
            "record_kind": "supplement",
            "supplement_of": "doi_denied",
            "review_status": "",
            "md_path": str(cmd),
        },
    }
    docs, stats = render_papers(db)
    by_id = {d["doc_id"]: d for d in docs}
    assert set(by_id) == {"paper:doi_p", "paper:doi_p__supp01"}
    parent, child = by_id["paper:doi_p"], by_id["paper:doi_p__supp01"]
    assert parent["source"] == "paper_markdown"
    assert child["source"] == "supplement_markdown"
    assert child["provenance"]["supplement_of"] == "doi_p"
    assert child["provenance"]["supplement_form"] == "si_docx"
    assert child["provenance"]["identifier"] == "10.1000/p"
    assert child["val"] == parent["val"] == is_val("paper:doi_p")
    assert stats["papers"] == 2 and stats["supplements"] == 1
    assert corpus_stage1.REPEATS["supplement_markdown"] == 1
