"""Hand-ingested PDFs land in exactly the state acquisition leaves behind."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.actions.scholarly_actions import paper_key  # noqa: E402
from tools.ingest_local_pdf import build_record  # noqa: E402

META = dict(
    title="Fusion of spectral data from multiple handheld analyzers",
    doi="10.21079/11681/38061",
    arxiv="",
    authors=["R. S. Harmon", "R. R. Hark"],
    year=2020,
    venue="ERDC/CRREL CR-20-1",
    aspects=["multi technique paired spectra"],
    license_="public-domain",
    url="https://doi.org/10.21079/11681/38061",
    source="operator hand-off",
    binder=False,
    now="2026-09-16T00:00:00+00:00",
)


def test_new_record_is_acquired_with_the_pdf_on_disk_path():
    key = paper_key(
        {"doi": META["doi"], "arxiv_id": "", "s2_id": "", "title": META["title"]}
    )
    assert key == "doi_10.21079_11681_38061"
    rec = build_record(None, key=key, **META)
    assert rec["status"] == "acquired" and rec["access_status"] == "oa_pdf"
    assert rec["pdf_path"] == f"pdfs/{key}.pdf"
    assert rec["oa_pdf_url"] == META["url"] and rec["oa_attempted"] == [META["url"]]
    assert (
        rec["retrieval_method"] == "manual"
        and rec["ingest_source"] == "operator hand-off"
    )
    assert (
        rec["license"] == "public-domain" and rec["source_aspects"] == META["aspects"]
    )
    assert rec["authors"] == META["authors"] and rec["year"] == 2020
    assert "binder" not in rec and "review_status" not in rec


def test_existing_row_is_merged_not_replaced():
    base = {
        "paper_key": "doi_10.21079_11681_38061",
        "status": "candidate",
        "access_status": "oa_unresolved",
        "title": "Older title spelling",
        "source_aspects": ["LIBS mineral spectra"],
        "oa_pdf_urls": ["https://old/location.pdf"],
        "oa_attempted": ["https://old/location.pdf"],
        "tags": [{"aspect": "x"}],
        "review_status": "denied",
    }
    rec = build_record(base, key=base["paper_key"], **dict(META, binder=True))
    # Acquisition fields flip; everything the record already knew survives.
    assert rec["status"] == "acquired" and rec["access_status"] == "oa_pdf"
    assert rec["title"] == "Older title spelling"
    assert rec["tags"] == [{"aspect": "x"}] and rec["review_status"] == "denied"
    assert rec["source_aspects"] == [
        "LIBS mineral spectra",
        "multi technique paired spectra",
    ]
    assert (
        rec["oa_pdf_urls"][0] == META["url"]
        and "https://old/location.pdf" in rec["oa_pdf_urls"]
    )
    assert rec["binder"] is True
