"""Supplement marks: forms/routes, misfile detection, child rows, Word rewrite.

The network and pandoc halves run live; these pin the pure decisions — what a
file is, which entries are the parent article, what a child record says, and
how a Word document's image references become the extractor's convention.
"""

from __future__ import annotations

import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.supplement_records import (  # noqa: E402
    child_key_for,
    child_record,
    classify,
    expand_zip,
    extraction_row,
    latin_ratio,
    legacy_pdf_entry,
    mark_entries,
    needs_reconvert,
    next_child_index,
    purge_entries,
    rewrite_images,
)


def test_classify_maps_every_observed_form_to_a_route():
    assert classify("x_MOESM1_ESM.pdf") == ("si_pdf", "document")
    assert classify("mmc1.DOCX") == ("si_docx", "document")
    assert classify("srep05226-s1.doc") == ("si_legacy_doc", "deferred")
    assert classify("Data_file_S3.xlsx") == ("si_table", "data")
    assert classify("MOESM2_ESM.csv") == ("si_table", "data")
    assert classify("j-52-00587-sup1.txt") == ("si_text", "data")
    assert classify("cm2c00522_si_001.cif") == ("si_cif", "structure")
    assert classify("e-79-01155-Isup2.hkl") == ("si_cif_aux", "skip")
    assert classify("41467_2024_44767_MOESM3_ESM.zip") == ("si_zip", "expand")
    assert classify("supp_index.html") == ("si_web", "skip")
    assert classify("41699_2018_48_MOESM2_ESM.avi") == ("si_media", "skip")
    assert classify("no_extension") == ("si_other", "skip")


def test_mark_entries_routes_article_pdfs_by_url_and_by_bytes(tmp_path):
    parent_pdf = tmp_path / "pdfs" / "doi_x.pdf"
    parent_pdf.parent.mkdir()
    parent_pdf.write_bytes(b"%PDF-1.4 the article")
    sdir = tmp_path / "supplements" / "doi_x"
    sdir.mkdir(parents=True)
    (sdir / "s11214-021-00812-z.pdf").write_bytes(b"%PDF-1.4 the article")
    (sdir / "real_MOESM1_ESM.pdf").write_bytes(b"%PDF-1.4 supplement")
    rec = {
        "paper_key": "doi_x",
        "pdf_path": "pdfs/doi_x.pdf",
        "supplements": [
            {
                "name": "by_url.pdf",
                "url": "https://link.springer.com/content/pdf/10.1007/s1.pdf",
            },
            {"name": "s11214-021-00812-z.pdf", "url": "https://x/odd/path.pdf"},
            {"name": "real_MOESM1_ESM.pdf", "url": "https://x/esm/real_MOESM1_ESM.pdf"},
            {"name": "table.xlsx", "url": "https://x/table.xlsx"},
        ],
    }
    assert mark_entries(rec, str(tmp_path)) is True
    routes = [e["route"] for e in rec["supplements"]]
    assert routes == ["article_duplicate", "article_duplicate", "document", "data"]
    assert rec["supplements"][3]["form"] == "si_table"
    # Idempotent, and an existing route survives a re-run without --refresh.
    rec["supplements"][2]["route"] = "deferred"
    assert mark_entries(rec, str(tmp_path)) is False
    assert rec["supplements"][2]["route"] == "deferred"


def test_purge_splits_duplicates_from_the_rest():
    rec = {
        "supplements": [
            {"name": "a.pdf", "route": "article_duplicate"},
            {"name": "b.pdf", "route": "document"},
            {"name": "c.zip", "route": "expanded"},
        ]
    }
    kept, dupes = purge_entries(rec)
    assert [e["name"] for e in kept] == ["b.pdf", "c.zip"]
    assert [e["name"] for e in dupes] == ["a.pdf"]


def test_child_keys_number_from_the_marks_present():
    rec = {"paper_key": "doi_p", "supplements": [{"child_key": "doi_p__supp02"}, {}]}
    assert next_child_index(rec) == 3
    assert child_key_for("doi_p", 3) == "doi_p__supp03"
    assert next_child_index({"paper_key": "doi_p", "supplements": []}) == 1


def test_child_record_inherits_acceptance_and_marks_itself():
    parent = {
        "paper_key": "doi_10.1000_p",
        "doi": "10.1000/p",
        "title": "Parent paper",
        "year": 2020,
        "venue": "J",
        "license": "cc-by",
        "language": "en",
        "binder": True,
        "review_status": "accepted",
        "pack_status": "packed",
    }
    pdf = child_record(
        parent, {"name": "x_MOESM1_ESM.pdf", "form": "si_pdf"}, "doi_10.1000_p__supp01"
    )
    assert pdf["paper_key"] == "doi_10.1000_p__supp01"
    assert (
        pdf["record_kind"] == "supplement" and pdf["supplement_of"] == "doi_10.1000_p"
    )
    assert (
        pdf["review_status"] == "accepted"
        and pdf["curation_method"] == "supplement_inherited"
    )
    assert pdf["pack_status"] == "pack_skipped_supplement"
    assert pdf["access_status"] == "oa_pdf"
    assert pdf["pdf_path"] == "supplements/doi_10.1000_p/x_MOESM1_ESM.pdf"
    assert pdf["parent_doi"] == "10.1000/p" and "doi" not in pdf
    assert pdf["binder"] is True and pdf["license"] == "cc-by"
    assert "Supplementary material" in pdf["title"]
    docx = child_record(
        parent, {"name": "mmc1.docx", "form": "si_docx"}, "doi_10.1000_p__supp02"
    )
    assert docx["access_status"] == "supplement_doc" and docx["pdf_path"] == ""
    assert "dataset_path" not in docx and "figtext_status" not in docx


def test_rewrite_images_uses_the_extractor_convention_and_marks_the_rest():
    md = (
        'Text <img src="/tmp/m/media/image1.png" style="width:6in" /> more\n'
        'vector <img src="/tmp/m/media/image3.emf" />\n'
        '![alt](/tmp/m/media/image2.jpeg "t")\n'
        'again <img src="/tmp/m/media/image1.png"/>\n'
        'odd <img src="/tmp/m/media/image4.svg" />\n'
    )
    out, order = rewrite_images(md, "k__supp01")
    # Rasters first in appearance order, vectors after them: the numbering a
    # first pass (rasters only) produced is preserved on re-conversion.
    assert order == [
        "/tmp/m/media/image1.png",
        "/tmp/m/media/image2.jpeg",
        "/tmp/m/media/image3.emf",
    ]
    assert out.count('<img src="../figures/k__supp01/fig_01.png">') == 2
    assert '<img src="../figures/k__supp01/fig_02.png">' in out
    assert '<img src="../figures/k__supp01/fig_03.png">' in out
    assert "<!-- unconverted figure: image4.svg -->" in out
    assert "style=" not in out and "![" not in out


def test_legacy_pdf_entry_points_back_at_its_source(tmp_path):
    pdf = tmp_path / "supplements" / "doi_p" / "bundle" / "notes.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 rendered")
    e = legacy_pdf_entry({"name": "bundle/notes.doc"}, pdf, "doi_p", str(tmp_path))
    assert e == {
        "name": "bundle/notes.pdf",
        "bytes": 17,
        "from_convert": "bundle/notes.doc",
        "form": "si_pdf",
        "route": "document",
    }


def test_needs_reconvert_targets_dropped_figures_only():
    child = {
        "record_kind": "supplement",
        "supplement_form": "si_docx",
        "figtext_status": "",
    }
    dropped = {
        "extraction_method": "pandoc",
        "extraction_quality": {"figures_dropped": 3},
    }
    clean = {
        "extraction_method": "pandoc",
        "extraction_quality": {"figures_dropped": 0},
    }
    assert needs_reconvert(child, dropped, False) is True
    assert needs_reconvert(child, clean, False) is False
    assert (
        needs_reconvert(dict(child, supplement_form="si_pdf"), dropped, False) is False
    )
    done = dict(child, figtext_status="figtext_done")
    assert needs_reconvert(done, dropped, False) is False
    assert needs_reconvert(done, dropped, True) is True
    assert needs_reconvert({}, dropped, True) is False


def test_extraction_row_is_native_text_and_flags_non_latin():
    row = extraction_row(
        "k__supp01",
        {"figures": 3, "chars": 900, "tables": 12, "skipped": 1, "latin_ratio": 0.99},
    )
    assert row["extraction_status"] == "extracted"
    assert row["md_path"] == "databank/markdown/k__supp01.md"
    assert row["figure_count"] == 3 and row["extraction_method"] == "pandoc"
    assert row["extraction_quality"]["native_text"] is True
    assert row["extraction_quality"]["oracle"] == "none"
    lingual = extraction_row("k__supp02", {"latin_ratio": 0.2})
    assert lingual["extraction_status"] == "extract_lingual"
    assert latin_ratio("Раман спектры") < 0.5 < latin_ratio("Raman spectra 1085 cm-1")


def test_expand_zip_extracts_documents_only_with_safe_names(tmp_path):
    zp = tmp_path / "supplements" / "doi_p" / "bundle.zip"
    zp.parent.mkdir(parents=True)
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("dir/Supp Info (final).pdf", b"%PDF-1.4 a")
        z.writestr("dir/data.xlsx", b"PK data")
        z.writestr("dir/movie.mp4", b"\x00" * 10)
        z.writestr("__MACOSX/._x.pdf", b"junk")
        z.writestr("other/data.xlsx", b"PK data2")
    entries = expand_zip(zp, "doi_p", str(tmp_path))
    names = [e["name"] for e in entries]
    assert names == [
        "bundle/Supp_Info__final_.pdf",
        "bundle/data.xlsx",
        "bundle/data_2.xlsx",
    ]
    assert all(e["from_zip"] == "bundle.zip" for e in entries)
    assert entries[0]["form"] == "si_pdf" and entries[0]["route"] == "document"
    assert (
        tmp_path / "supplements" / "doi_p" / "bundle" / "data_2.xlsx"
    ).read_bytes() == b"PK data2"
    # Re-running is a no-op on disk and returns the same entries.
    assert [e["name"] for e in expand_zip(zp, "doi_p", str(tmp_path))] == names
