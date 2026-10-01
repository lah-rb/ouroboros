"""The closed shelf enters a training build only when named, and is marked when it does.

tools/closed_shelf.py keeps owned, not-openly-licensed books in their own
workspace root. The stage-1 renderer reads that root ONLY with --closed-root,
labels its books binder_markdown (packs pack_prose) with closed_material=True,
and refuses an open build over a docs dir that once took closed material.
"""

import json

import pytest

import emit
from corpus_stage1 import (
    CLOSED_MARKER,
    closed_guard,
    merged_databank,
    render_packs,
    render_papers,
)

KEY = "book_spectroscopic_methods_in_mineralogy_and_geology_hawthorne"


def _shelf(tmp_path):
    root = tmp_path / "ouroboros-closed"
    (root / "databank" / "markdown").mkdir(parents=True)
    (root / "databank" / "dataset").mkdir(parents=True)
    (root / CLOSED_MARKER).write_text("closed")
    (root / "databank" / "markdown" / f"{KEY}.md").write_text(
        "Calcite shows its v1 carbonate band at 1086 cm-1.\n" * 30
    )
    row = {
        "paper_key": KEY,
        "title": "Spectroscopic Methods In Mineralogy And Geology Hawthorne",
        "record_kind": "book",
        "binder": True,
        "distribution": "closed",
        "license": "all-rights-reserved (owned copy)",
        "review_status": "accepted",
        "review_summary": "Preapproved closed-shelf book.",
        "md_path": f"databank/markdown/{KEY}.md",
    }
    (root / "databank" / "papers.jsonl").write_text(json.dumps(row) + "\n")
    (root / "databank" / "dataset" / f"{KEY}.json").write_text(
        json.dumps(
            {
                "paper_key": KEY,
                "license": row["license"],
                "review": {"status": "accepted", "summary": "Raman of carbonates"},
                "data": {"raman_peak_wavenumber_cm-1": [1086]},
            }
        )
    )
    return str(root)


def test_the_shelf_renders_as_marked_binder_books(tmp_path):
    root = _shelf(tmp_path)
    docs, _ = render_papers(merged_databank(root), corpus=root, closed=True)
    (doc,) = docs
    assert doc["doc_id"] == f"closed:{KEY}"
    assert doc["source"] == "binder_markdown"
    assert doc["closed_material"] is True
    assert doc["license"] == "all-rights-reserved (owned copy)"
    (pack,) = render_packs(root, closed=True)
    assert pack["closed_material"] is True and pack["source"] == "pack_prose"
    assert pack["val"] == doc["val"], "a book and its pack land on the same side"


def test_open_rendering_never_marks_a_doc_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(emit, "binder_keys", lambda *a, **k: set())
    md = tmp_path / "p.md"
    md.write_text("Quartz Raman band at 465 cm-1.\n" * 20)
    db = {"p": {"paper_key": "p", "review_status": "accepted", "md_path": str(md)}}
    docs, _ = render_papers(db)
    assert docs and all(d["closed_material"] is False for d in docs)


def test_an_open_build_refuses_a_docs_dir_that_took_closed_material(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "papers.jsonl").write_text("")
    closed_guard(str(docs), None)  # clean: passes
    (docs / "closed_binder.jsonl").write_text("")
    with pytest.raises(SystemExit, match="closed-shelf docs"):
        closed_guard(str(docs), None)


def test_closed_root_must_be_a_real_shelf(tmp_path):
    with pytest.raises(SystemExit, match="not a closed shelf"):
        closed_guard(str(tmp_path), str(tmp_path))
    closed_guard(str(tmp_path / "docs"), _shelf(tmp_path))
