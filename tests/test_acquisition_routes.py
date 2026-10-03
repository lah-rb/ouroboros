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


def test_a_hal_copy_routes_the_paper_to_the_browser_ahead_of_its_publisher():
    """HAL (operator ruling 2026-10-03) is a REPOSITORY: no DOI prefix of its
    own, and the open copy of papers whose DOI names a walled publisher."""
    from agent.acquisition_routes import repository_for, route_row

    rec = {
        "paper_key": "doi_10.1111_ggr.12577",
        "doi": "10.1111/ggr.12577",
        "oa_pdf_url": "https://hal.science/hal-04763842/document",
    }
    assert publisher_for(rec).name == "wiley"
    assert repository_for(rec).name == "hal" and route_row(rec).name == "hal"
    assert acquirer(rec) == "browser"
    for host in (
        "https://theses.hal.science/tel-01127004",
        "https://tel.archives-ouvertes.fr/tel-00001",
        "https://hal.univ-lille.fr/hal-04467475v1/document",
        "https://hal-lirmm.ccsd.cnrs.fr/lirmm-0001",
    ):
        assert acquirer({"paper_key": "k", "oa_pdf_urls": [host]}) == "browser", host
    # without a HAL link the publisher decides, as before
    assert acquirer({"paper_key": "w", "doi": "10.1111/ggr.1"}) == "operator"
    # two failed browser tries hand it to the operator, as for MDPI
    two = [{"key": "doi_10.1111_ggr.12577", "outcome": "blocked"}] * 2
    assert acquirer(rec, browser_exhausted(two)) == "operator"
    # the crawler keeps trying HAL: it serves plain clients most of the time
    assert not any("hal" in h for h in crawler_walled_hosts())


def test_browser_fetch_finds_the_hal_landing_page_and_document():
    import importlib.util

    spec = importlib.util.spec_from_file_location("bf", "tools/browser_fetch.py")
    bf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bf)
    assert bf.landing_url(
        {"oa_pdf_url": "https://hal.science/hal-04763842/document"}, "hal"
    ) == (
        "https://hal.science/hal-04763842",
        "https://hal.science/hal-04763842/document",
    )
    assert bf.landing_url(
        {"oa_pdf_urls": ["https://theses.hal.science/tel-01127004"]}, "hal"
    ) == (
        "https://theses.hal.science/tel-01127004",
        "https://theses.hal.science/tel-01127004/document",
    )
    assert bf.landing_url({"doi": "10.1/x"}, "hal") == ("", "")
    assert bf.landing_url(
        {"oa_pdf_url": "https://www.mdpi.com/2075-163X/12/1/1/pdf?version=1"}, "mdpi"
    ) == ("https://www.mdpi.com/2075-163X/12/1/1", "")
    assert bf.STRATEGIES["hal"] == "/document"
