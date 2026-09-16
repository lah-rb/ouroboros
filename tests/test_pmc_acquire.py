"""The PMC open-data acquisition route: id parsing and candidate selection.

The network halves (bucket listing, download) are the effects layer's job and
are exercised live; these are the pure decisions that pick WHICH papers get a
fetch attempt, where a wrong filter either re-downloads a paper we already
hold or spends attempts on ones already judged.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.pmc_acquire import candidates, load_ids  # noqa: E402


def test_load_ids_accepts_json_arrays_and_jsonl_and_normalises(tmp_path):
    p = tmp_path / "ids.jsonl"
    p.write_text(
        json.dumps(
            [{"doi": "10.1/AAA", "pmc": "123"}, {"doi": "10.2/b", "pmc": "PMC9"}]
        )
        + "\n"
        + json.dumps({"doi": "10.3/c", "pmc": None})
        + "\n"
        + json.dumps({"doi": "10.4/d", "pmc": "none"})
        + "\n"
    )
    ids = load_ids(str(p))
    assert ids == {"10.1/aaa": "PMC123", "10.2/b": "PMC9"}


def _rec(**kw):
    base = {"doi": "10.1/x", "access_status": "oa_unresolved"}
    base.update(kw)
    return base


def test_candidates_selects_only_papers_that_want_a_pdf(tmp_path):
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "pdfs"), exist_ok=True)
    open(os.path.join(base, "pdfs", "have.pdf"), "wb").write(b"%PDF-1.7 ...")
    ids = {f"10.1/{n}": "PMC1" for n in ("want", "have", "acc", "den", "done", "noid")}
    bank = {
        "want": _rec(doi="10.1/want"),
        # properly booked: PDF on disk AND the record says so
        "have": _rec(doi="10.1/have", pdf_path="pdfs/have.pdf", access_status="oa_pdf"),
        "acc": _rec(doi="10.1/acc", review_status="accepted"),
        "den": _rec(doi="10.1/den", review_status="denied"),
        "done": _rec(doi="10.1/done", extraction_status="extracted"),
        "nomatch": _rec(doi="10.1/absent"),
    }
    got = [c["key"] for c in candidates(bank, ids, base)]
    assert got == ["want"], got


def test_candidates_include_closed_papers_and_a_stale_pdf_path(tmp_path):
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "pdfs"), exist_ok=True)
    bank = {
        "closed": _rec(doi="10.1/closed", access_status="closed"),
        # a pdf_path whose file is gone is not a held PDF
        "stale": _rec(doi="10.1/stale", pdf_path="pdfs/missing.pdf"),
    }
    ids = {"10.1/closed": "PMC1", "10.1/stale": "PMC2"}
    assert sorted(c["key"] for c in candidates(bank, ids, base)) == ["closed", "stale"]


def test_a_pdf_on_disk_with_a_lost_booking_is_reselected_for_rebooking(tmp_path):
    base = str(tmp_path)
    os.makedirs(os.path.join(base, "pdfs"), exist_ok=True)
    open(os.path.join(base, "pdfs", "lost.pdf"), "wb").write(b"%PDF-1.7 ...")
    open(os.path.join(base, "pdfs", "booked.pdf"), "wb").write(b"%PDF-1.7 ...")
    bank = {
        # the recover lane overwrote our booking from a stale record
        "lost": _rec(
            doi="10.1/lost", pdf_path="pdfs/lost.pdf", access_status="oa_unresolved"
        ),
        "booked": _rec(
            doi="10.1/booked", pdf_path="pdfs/booked.pdf", access_status="oa_pdf"
        ),
    }
    ids = {"10.1/lost": "PMC1", "10.1/booked": "PMC2"}
    got = candidates(bank, ids, base)
    assert [c["key"] for c in got] == ["lost"]
    assert got[0]["held"] is True  # re-book, no download


def test_pick_article_pdf_never_picks_a_supplement():
    from tools.pmc_acquire import pick_article_pdf

    # the real case: a supplement sorts BEFORE the article alphabetically
    keys = [
        "PMC12188159.1/CHEM-31-e202501203-g001.jpg",
        "PMC12188159.1/CHEM-31-e202501203-s001.pdf",
        "PMC12188159.1/PMC12188159.1.pdf",
        "PMC12188159.1/PMC12188159.1.xml",
    ]
    assert pick_article_pdf(keys) == "PMC12188159.1/PMC12188159.1.pdf"
    # the other supplement spellings seen in the first run
    for supp in ("mmc1.pdf", "11664_2022_9813_MOESM1_ESM.pdf", "Data_Sheet_1.PDF"):
        assert (
            pick_article_pdf([f"PMC1.1/{supp}", "PMC1.1/PMC1.1.pdf"])
            == "PMC1.1/PMC1.1.pdf"
        )
    # newest version wins
    assert (
        pick_article_pdf(["PMC7.1/PMC7.1.pdf", "PMC7.2/PMC7.2.pdf"])
        == "PMC7.2/PMC7.2.pdf"
    )
    # supplement only, or nothing: refuse rather than book the wrong document
    assert pick_article_pdf(["PMC1.1/mmc1.pdf"]) == ""
    assert pick_article_pdf(["PMC1.1/PMC1.1.xml"]) == ""
