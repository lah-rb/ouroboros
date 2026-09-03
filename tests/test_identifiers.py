"""Alternative identifiers: resolve what we can, record the rest as none."""

from __future__ import annotations

from agent.actions.identifiers import (
    envelope_identity,
    identifier_from_url,
    is_confident_match,
    openalex_id_short,
    record_identifier,
    title_match_score,
)


def test_repository_identifiers_are_parsed_from_source_urls():
    cases = {
        "https://hdl.handle.net/10662/12345": ("10662/12345", "hdl"),
        "https://etheses.dur.ac.uk/handle/2020/9876/1.pdf": ("2020/9876", "hdl"),
        "http://www.theses.fr/2013PA112254/abes": ("2013PA112254", "theses.fr"),
        "https://core.ac.uk/download/30695665.pdf": ("30695665", "core"),
        "https://core.ac.uk/download/pdf/12796941.pdf": ("12796941", "core"),
        "https://nbn-resolving.org/urn:nbn:de:bsz:14-qucosa-123": (
            "urn:nbn:de:bsz:14-qucosa-123",
            "urn",
        ),
    }
    for url, want in cases.items():
        assert identifier_from_url(url) == want, url
    assert identifier_from_url("https://example.org/paper.pdf") == ("", "")
    assert identifier_from_url("") == ("", "")


def test_a_wayback_wrapper_resolves_to_the_document_not_the_snapshot():
    url = "https://web.archive.org/web/20200101120000/https://hdl.handle.net/1822/55555"
    assert identifier_from_url(url) == ("1822/55555", "hdl")


def test_record_identifier_prefers_the_strongest_tier():
    base = {"oa_pdf_url": "https://core.ac.uk/download/30695665.pdf"}
    assert record_identifier({**base, "doi": "10.1/x"}) == ("10.1/x", "doi")
    assert record_identifier({**base, "arxiv_id": "2401.01234"}) == (
        "2401.01234",
        "arxiv",
    )
    assert record_identifier({**base, "openalex_id": "W123"}) == ("W123", "openalex")
    # a resolution already written to the record wins over re-parsing the URL
    assert record_identifier(
        {**base, "identifier": "1822/9", "identifier_kind": "hdl"}
    ) == (
        "1822/9",
        "hdl",
    )
    # nothing but the URL: fall back to what it embeds
    assert record_identifier(base) == ("30695665", "core")
    # the honest absence
    assert record_identifier({"title": "x"}) == ("", "none")


def test_urls_list_is_searched_when_the_primary_url_carries_nothing():
    rec = {
        "oa_pdf_url": "https://example.org/file.pdf",
        "oa_pdf_urls": ["https://example.org/file.pdf", "https://hdl.handle.net/1/2"],
    }
    assert record_identifier(rec) == ("1/2", "hdl")


def test_envelope_identity_always_answers():
    assert envelope_identity({"doi": "10.1/x"}) == {
        "identifier": "10.1/x",
        "identifier_kind": "doi",
    }
    assert envelope_identity({"title": "unidentified thesis"}) == {
        "identifier": "",
        "identifier_kind": "none",
    }


def test_openalex_id_is_normalised_to_its_w_number():
    assert openalex_id_short("https://openalex.org/W2049875903") == "W2049875903"
    assert openalex_id_short("W123") == "W123"


def test_title_matching_is_order_insensitive_and_ignores_stopwords():
    assert (
        title_match_score("Raman spectra of quartz", "raman spectra of quartz") == 1.0
    )
    assert (
        title_match_score("Spectra of quartz, Raman", "Raman spectra of quartz") == 1.0
    )
    assert (
        title_match_score("Raman spectra of quartz", "Neutron diffraction of ice") < 0.2
    )
    assert title_match_score("", "anything") == 0.0


def test_a_match_needs_the_title_AND_a_compatible_year():
    """A wrong identity is worse than none: a repository deposit year may trail
    the publication year by one, but not by five."""
    rec = {"title": "Raman spectra of quartz under pressure", "year": 2013}
    work = {"title": "Raman spectra of quartz under pressure", "publication_year": 2014}
    assert is_confident_match(work, rec)
    assert not is_confident_match({**work, "publication_year": 2005}, rec)
    assert not is_confident_match(
        {"title": "Something else entirely", "publication_year": 2013}, rec
    )
    # With no year on either side, only a title long enough to be a
    # fingerprint stands alone -- this one (5 distinctive words) does not.
    assert not is_confident_match({"title": rec["title"]}, {"title": rec["title"]})
    assert not is_confident_match(
        {"title": rec["title"] + " and other things"}, {"title": rec["title"]}
    )


def test_the_scrapers_own_identifier_field_is_classified_not_trusted():
    """It carries a mix: real handles, and bare URLs that identify nothing.
    A landing page is a location, not an identity."""
    from agent.actions.identifiers import classify_identifier

    assert classify_identifier("10174/24376") == ("10174/24376", "hdl")
    assert classify_identifier("20.500.11754/70607") == ("20.500.11754/70607", "hdl")
    assert classify_identifier("10.1029/2021je007134") == (
        "10.1029/2021je007134",
        "doi",
    )
    assert classify_identifier("urn:nbn:se:uu:diva-1") == (
        "urn:nbn:se:uu:diva-1",
        "urn",
    )
    assert classify_identifier("W2049875903") == ("W2049875903", "openalex")
    # a URL is re-parsed for an embedded id...
    assert classify_identifier("https://core.ac.uk/download/650038590.pdf") == (
        "650038590",
        "core",
    )
    # ...and discarded when it embeds none
    assert classify_identifier("https://espace.inrs.ca/") == ("", "")
    assert classify_identifier("") == ("", "")


def test_a_url_only_identifier_field_does_not_mask_a_real_one():
    rec = {
        "identifier": "https://espace.inrs.ca/",
        "oa_pdf_url": "https://hdl.handle.net/1/2",
    }
    assert record_identifier(rec) == ("1/2", "hdl")
    # ...and with nothing else, the paper is honestly unidentified
    assert record_identifier({"identifier": "https://espace.inrs.ca/"}) == ("", "none")


def test_dspace_bitstream_urls_yield_their_handle():
    url = "http://dspace.uevora.pt/rdpc/bitstream/10174/24376/1/New%20Look.pdf"
    assert identifier_from_url(url) == ("10174/24376", "hdl")


def test_supplementary_component_dois_are_refused():
    """Crossref mints a DOI per supplementary file, inheriting the parent's
    title, so a title search matches them. Pointing a record at the eighth SI
    spreadsheet is a wrong identity, and a wrong identity is worse than none."""
    from agent.actions.identifiers import is_component_doi

    assert is_component_doi("10.1021/acsami.2c22595.s008")
    assert is_component_doi("10.1021/acs.analchem.7b04124.s001")
    assert not is_component_doi("10.1021/ie404055z")
    assert not is_component_doi("10.1016/s0257-8972(99)00072-9")
    assert not is_component_doi("10.1007/978-3-642-84511-6_5")
    assert not is_component_doi("")


def test_a_partial_title_needs_a_corroborating_year():
    """CAUGHT LIVE (2026-09-03): a partial match with no year on either side
    put a geological-samples chapter onto the DOI of the LIQUID-samples
    chapter of the same handbook. Generic technique titles collide constantly
    here, so a wrong identity is the likely error, not the unlikely one."""
    rec = {
        "title": "Laser induced breakdown spectroscopy of geological samples",
        "year": 0,
    }
    work = {
        "title": "Laser-Induced Breakdown Spectroscopy of Liquid Samples",
        "publication_year": 2007,
    }
    assert not is_confident_match(work, rec)
    # the same partial match WITH agreeing years is allowed
    assert is_confident_match(
        work, {**rec, "year": 2007, "title": work["title"] + " extra"}
    )
    # an exact title needs no year at all (a dissertation Crossref does not date)
    exact = {
        "title": "Preparation of transition metal oxide thin films used as solar absorbers"
    }
    assert is_confident_match({"title": exact["title"]}, {**exact, "year": 0})


def test_a_long_exact_title_outranks_a_disagreeing_year():
    """CAUGHT LIVE (2026-09-03): two correct matches carried year gaps of 2 and
    8 years because Crossref's "issued" is the DOI REGISTRATION date for
    back-deposited journals. A long exact title is a fingerprint; a short one
    ("Raman spectra of quartz under pressure") describes a dozen papers."""
    long_title = (
        "Data report: high-resolution mineralogy for leg 199 based on "
        "reflectance spectroscopy and physical properties"
    )
    assert is_confident_match(
        {"title": long_title, "publication_year": 2006},
        {"title": long_title, "year": 2004},
    )
    short = "Raman spectra of quartz under pressure"
    assert not is_confident_match(
        {"title": short, "publication_year": 2005}, {"title": short, "year": 2013}
    )


def test_an_openalex_url_is_recorded_as_its_work_number():
    """The scraper stores the full URL; the identifier is the W-number. Two
    packs went out carrying "https://openalex.org/W2242527..." as an identity."""
    assert record_identifier({"openalex_id": "https://openalex.org/W2242527"}) == (
        "W2242527",
        "openalex",
    )
    assert (
        envelope_identity({"openalex_id": "https://openalex.org/W1"})["identifier"]
        == "W1"
    )
