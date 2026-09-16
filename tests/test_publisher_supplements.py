"""Publisher supplement routes: link extraction and the Elsevier PII path.

The network half is measured live; these pin the decisions that turn a landing
page into download candidates — where a wrong regex either misses the
supplement or sends the fetcher at the article HTML.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.publisher_supplements import (  # noqa: E402
    elsevier_urls,
    publisher_of,
    safe_name,
    supplement_links,
)


def test_publisher_of_maps_registrants():
    assert publisher_of("10.1038/s41598-025-11790-5") == "Nature"
    assert publisher_of("10.1016/j.sab.2014.08.039") == "Elsevier"
    assert publisher_of("10.5555/unknown") == ""
    assert publisher_of("") == ""


def test_supplement_links_finds_the_real_shapes_and_absolutises():
    page = "https://link.springer.com/article/10.1007/s11664-022-09813-2"
    html = """
      <a href="/esm/art%3A10.1007%2Fs11664-022-09813-2/MediaObjects/11664_ESM.pdf">ESM</a>
      <a href="https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002/x&file=y.docx">SI</a>
      <a href="//www.rsc.org/suppdata/d0/ce/paper/c9ce00001a1.pdf">ESI</a>
      <a href="/article/10.3390/s26031076/s1">Supplementary Material</a>
      <a href="/articles/abcd">Related article</a>
    """
    got = supplement_links(html, page)
    assert got[0] == (
        "https://link.springer.com/esm/art%3A10.1007%2Fs11664-022-09813-2/"
        "MediaObjects/11664_ESM.pdf"
    )
    assert "https://www.rsc.org/suppdata/d0/ce/paper/c9ce00001a1.pdf" in got
    assert any(u.endswith("/s1") for u in got)
    assert not any("related" in u or u.endswith("/articles/abcd") for u in got)


def test_elsevier_urls_from_the_linkinghub_pii():
    u = "https://linkinghub.elsevier.com/retrieve/pii/S0584854714002158"
    got = elsevier_urls(u, "")
    assert got[0] == (
        "https://ars.els-cdn.com/content/image/1-s2.0-S0584854714002158-mmc1.pdf"
    )
    assert len(got) == 8 and all("S0584854714002158" in g for g in got)
    # no PII anywhere: no guesses
    assert elsevier_urls("https://doi.org/10.1016/j.x", "") == []
    # the PII can also arrive in the page body
    assert elsevier_urls("https://doi.org/x", '<a href="/pii/S123X">')[0].endswith(
        "mmc1.pdf"
    )


def test_safe_name_strips_paths_and_query_junk():
    assert safe_name("https://host/a/b/41467_2018_7420_MOESM10_ESM.txt") == (
        "41467_2018_7420_MOESM10_ESM.txt"
    )
    assert "/" not in safe_name(
        "https://host/downloadSupplement?doi=10.1/x&file=y.docx"
    )
    assert safe_name("https://host/") == "supplement"


def test_media_and_html_are_not_supplements():
    from tools.publisher_supplements import SKIP_EXT

    for ext in (".mp4", ".mov", ".jpg", ".png", ".gif", ".html"):
        assert ext in SKIP_EXT
    for ext in (".pdf", ".docx", ".xlsx", ".csv", ".zip", ".cif"):
        assert ext not in SKIP_EXT
