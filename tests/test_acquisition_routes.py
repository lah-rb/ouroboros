"""The one table of acquisition routes (2026-10-02).

Who fetches a paper the crawler cannot used to be answered in three places --
the crawler's walled hosts, the manual export's DOI-prefix flag and the
browser fetcher's URL pattern -- and the manual lists kept handing the
operator MDPI papers the browser fetcher was already taking. Every consumer
now derives from agent/acquisition_routes.py.
"""

from __future__ import annotations

import json

from agent.acquisition_routes import (
    BROWSER_ATTEMPTS,
    BROWSER_FETCH_LOG,
    acquirer,
    browser_exhausted,
    crawler_walled_hosts,
    publisher_for,
    read_browser_log,
)
from agent.actions import scholarly_actions as SA


def test_the_crawler_walls_exactly_the_measured_hosts():
    assert set(crawler_walled_hosts()) == {
        "sciencedirect.com",
        "onlinelibrary.wiley.com",
        "www.mdpi.com",
        "iopscience.iop.org",
        "pubs.rsc.org",
        "doi.org",
    }
    assert set(SA.WALLED_HOSTS) == set(crawler_walled_hosts())
    assert not SA._is_walled(
        "https://mdpi-res.com/d_attachment/x.pdf"
    ), "MDPI's PDF CDN serves plain clients"


def test_publisher_by_doi_prefix_then_by_stored_host():
    assert publisher_for({"doi": "10.3390/min12010001"}).name == "mdpi"
    assert publisher_for({"doi": "10.1016/j.sab.2024.1"}).name == "elsevier"
    assert (
        publisher_for(
            {"doi": "", "oa_pdf_url": "https://www.mdpi.com/2075-163X/12/1/1/pdf"}
        ).name
        == "mdpi"
    )
    assert (
        publisher_for({"doi": "10.9999/x", "oa_pdf_url": "https://repo.edu/a.pdf"})
        is None
    )


def _mdpi(key="doi_10.3390_x"):
    return {"paper_key": key, "doi": "10.3390/x", "access_status": "oa_unresolved"}


def test_mdpi_is_the_browsers_until_it_fails_twice():
    rec = _mdpi()
    assert acquirer(rec) == "browser"
    one = [{"key": rec["paper_key"], "outcome": "page_error"}]
    assert acquirer(rec, browser_exhausted(one)) == "browser", "one transient retry"
    two = one * BROWSER_ATTEMPTS
    assert acquirer(rec, browser_exhausted(two)) == "operator"
    fixed = two + [{"key": rec["paper_key"], "outcome": "ok"}]
    assert acquirer(rec, browser_exhausted(fixed)) == "browser"


def test_walled_publishers_without_a_browser_ruling_are_the_operators():
    for doi in ("10.1016/a", "10.1002/b", "10.1088/c", "10.1039/d", "10.1007/e"):
        assert acquirer({"paper_key": doi, "doi": doi}) == "operator"
    assert acquirer({"paper_key": "k", "doi": "10.9999/unknown"}) == "operator"


def test_the_browser_log_is_read_from_the_corpus(tmp_path):
    log = tmp_path / BROWSER_FETCH_LOG
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"key": "a", "outcome": "ok"})
        + "\nnot json\n"
        + json.dumps({})
        + "\n"
    )
    assert read_browser_log(tmp_path) == [{"key": "a", "outcome": "ok"}]
    assert read_browser_log(tmp_path / "missing") == []
