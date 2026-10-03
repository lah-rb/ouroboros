"""dev/manual_intake/intake.py: list parsing, first-page matching, host inference,
ranking order, HTML fragment extraction and the rows it books."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "intake",
    Path(__file__).resolve().parents[1] / "dev" / "manual_intake" / "intake.py",
)
intake = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(intake)


def _papers():
    rows = [
        (
            "doi_10.1002_jrs.4417",
            "10.1002/jrs.4417",
            "Recent advances in linear and nonlinear Raman spectroscopy. Part VII",
        ),
        (
            "doi_10.1002_jrs.4000",
            "10.1002/jrs.4000",
            "Recent advances in linear and nonlinear Raman spectroscopy. Part VI",
        ),
        ("title_x", "", "X線散乱で調べるPMN-30%PT正方晶相の90度ドメイン境界"),
        (
            "doi_10.1002_jrs.5214",
            "10.1002/jrs.5214",
            "Comparison of seven portable Raman spectrometers: beryl as a case study",
        ),
        ("doi_generic", "10.1/g", "Raman spectroscopy of minerals"),
    ]
    return {k: {"paper_key": k, "doi": d, "title": t} for k, d, t in rows}


def test_parse_list_reads_positions_and_keys(tmp_path):
    md = tmp_path / "l.md"
    md.write_text(
        "# x\n\n2. Open each link.\n\n1. [A title](https://s)\n   Venue · 2013 · doi 10.1/a · `doi_10.1_a`\n\n"
        "2. [B](https://s)\n   venue unknown · no DOI · `title_b`\n\n**3. [C](https://s)\n   x · `title_c`\n\n"
        "~~4. [D](https://s)~~\n   x · `title_d`\n\n| 1 | PDF | host |\n"
    )
    assert intake.parse_list(md) == [
        (1, "doi_10.1_a"),
        (2, "title_b"),
        (3, "title_c"),
        (4, "title_d"),
    ]


def test_longest_title_wins_and_cjk_matches_across_spaces():
    idx = intake.TitleIndex(_papers())
    page = "Review DOI 10.1002/jrs.4417 Recent advances in linear and\nnonlinear Raman spectroscopy. Part VII† Laurence Nafie"
    key, why = intake.decide(idx.candidates(page), {"doi_10.1002_jrs.4417"})
    assert key == "doi_10.1002_jrs.4417" and why == "doi+title"
    page = "Recent advances in linear and nonlinear Raman spectroscopy. Part VII (no DOI on this page)"
    assert (
        intake.decide(
            idx.candidates(page), {"doi_10.1002_jrs.4417", "doi_10.1002_jrs.4000"}
        )[0]
        == "doi_10.1002_jrs.4417"
    )
    jp = "日本物理学会 15pK107-5 X 線散乱で調べる PMN-30%PT 正方晶相の 90 度ドメイン境界 量研"
    assert (
        intake.decide(intake.TitleIndex(_papers()).candidates(jp), {"title_x"})[0]
        == "title_x"
    )


def test_unlisted_matches_need_a_doi_or_a_long_title():
    idx = intake.TitleIndex(_papers())
    stray = "A paper about Raman spectroscopy of minerals in general, with nothing else matching."
    key, why = intake.decide(idx.candidates(stray), set())
    assert key is None and "weak match" in why
    beryl = "DOI 10.1002/jrs.5214 Comparison of seven portable Raman spectrometers: beryl as a case study Jan Jehlička"
    assert intake.decide(idx.candidates(beryl), set())[0] == "doi_10.1002_jrs.5214"
    assert (
        intake.decide(idx.candidates("nothing relevant here at all"), set())[0] is None
    )


def test_title_word_coverage_never_passes_for_a_verbatim_match():
    papers = {
        "doi_10.1016_j.proche.2013.03.007": {
            "paper_key": "doi_10.1016_j.proche.2013.03.007",
            "doi": "10.1016/j.proche.2013.03.007",
            "title": "Pigments and Mixtures Identification by Visible Reflectance Spectroscopy",
        }
    }
    idx = intake.TitleIndex(papers)
    rembrandt = (
        "npj heritage science https://doi.org/10.1038/s40494-025-01572-7 Identification of yellow lake pigments in paintings "
        "by Rembrandt and Vermeer. Mixtures of pigments were studied with visible reflectance spectroscopy."
    )
    c = idx.candidates(rembrandt)
    assert c and not c[0]["contained"] and c[0]["score"] < 1.0
    key, why = intake.decide(c, set())  # unlisted: word coverage alone is not enough
    assert key is None and "weak match" in why
    assert intake.decide(c, {"doi_10.1016_j.proche.2013.03.007"}) == (
        "doi_10.1016_j.proche.2013.03.007",
        "title-words",
    )


def test_holds_itself_catches_the_wrong_document():
    rec = _papers()["doi_10.1002_jrs.5214"]
    assert not intake.holds_itself(
        rec,
        "Regulating closed pore structure enables improved sodium storage for hard carbon",
    )
    assert intake.holds_itself(
        rec, "Comparison of seven portable Raman\nspectrometers: beryl as a case study"
    )


@pytest.mark.parametrize(
    "name,text,host",
    [
        ("Comparison_of_seven_portable_Raman_spect.pdf", "", "academia.edu"),
        ("1-s2.0-S1878929310000058-main.pdf", "", "publisher:elsevier"),
        (
            "Water Resources Research - 2012 - Robinet - Effects of mineral.pdf",
            "",
            "publisher:wiley",
        ),
        ("applsci-14-05755-v3.pdf", "", "publisher:mdpi"),
        (
            "bitstream_117241.pdf",
            "To cite this version: Basile Radisson",
            "repository:hal",
        ),
        (
            "paper.pdf",
            "See discussions at https://www.researchgate.net/publication/123",
            "researchgate",
        ),
        ("74.1_2577.pdf", "", "unknown"),
    ],
)
def test_infer_host(name, text, host):
    rec = {
        "title": "Comparison of seven portable Raman spectrometers: beryl as a case study"
    }
    assert intake.infer_host(Path(name), text, rec) == host


def test_english_heuristic_and_failure_class():
    assert intake.is_english(
        {"title": "Recent advances in linear and nonlinear Raman spectroscopy"}
    )
    assert not intake.is_english(
        {"title": "Stabilité des écoulements de canal à densité variable"}
    )
    assert not intake.is_english({"title": "显微共聚焦拉曼光谱对微量血迹的分析及成像"})
    assert not intake.is_english({"title": "Anything", "language": "de"})
    assert (
        intake.failure_class({"failure_reason": "HTTP 403 (tried 1 location(s))"})
        == "bot wall"
    )
    assert (
        intake.failure_class(
            {"failure_reason": "response is text/html, not a document"}
        )
        == "bot wall"
    )
    assert (
        intake.failure_class({"failure_reason": "HTTP 404 (tried 1 location(s))"})
        == "dead link"
    )
    assert (
        intake.failure_class({"failure_reason": "[Errno -2] Name or service not known"})
        == "other"
    )


def test_ranking_orders_tier_then_english_then_bot_wall():
    exact = [{"aspect": "a", "relevance": "exact"}] * 3
    reviewed = [
        {
            "paper_key": f"r{i}",
            "review_status": "accepted" if i % 10 < 9 else "denied",
            "tags": exact,
            "title": "Raman mineral spectra",
        }
        for i in range(100)
    ] + [
        {
            "paper_key": f"d{i}",
            "review_status": "denied" if i % 10 < 8 else "accepted",
            "tags": [],
            "title": "Unrelated topic",
        }
        for i in range(100)
    ]
    pool = [
        {
            "paper_key": "low_en_wall",
            "tags": [],
            "title": "Unrelated topic of the day",
            "failure_reason": "HTTP 403",
        },
        {
            "paper_key": "high_de_wall",
            "tags": exact,
            "title": "Raman Spektren der Minerale und die Analyse",
            "failure_reason": "HTTP 403",
        },
        {
            "paper_key": "high_en_dead",
            "tags": exact,
            "title": "Raman spectra of minerals",
            "failure_reason": "HTTP 404",
        },
        {
            "paper_key": "high_en_wall",
            "tags": exact,
            "title": "Raman spectra of the minerals",
            "failure_reason": "HTTP 403",
        },
    ]
    papers = {r["paper_key"]: r for r in reviewed + pool}
    items, fit = intake.rank_pool(papers, pool)
    assert [it["rec"]["paper_key"] for it in items] == [
        "high_en_wall",
        "high_en_dead",
        "high_de_wall",
        "low_en_wall",
    ]
    assert (
        fit["auc"] > 0.8 and items[0]["tier"] == "high" and items[-1]["tier"] == "low"
    )


def test_paper_row_replacement_clears_the_old_verdict_and_tags_asn_copies():
    rec = {
        "paper_key": "k",
        "status": "cataloged",
        "access_status": "oa_pdf",
        "pdf_path": "pdfs/k.pdf",
        "license": "",
        "review_status": "denied",
        "deny_category": "corpus_fit",
        "figtext_status": "figtext_done",
        "title": "T",
    }
    p = {
        "action": "REPLACE",
        "kind": "pdf",
        "host": "academia.edu",
        "file": Path("x.pdf"),
        "sha256": "h",
        "match": "doi+title",
        "pos": None,
    }
    row = intake.paper_row(rec, p, "unlisted", "now")
    assert (
        "review_status" not in row
        and "deny_category" not in row
        and "figtext_status" not in row
    )
    assert (
        row["status"] == "cataloged"
        and row["access_status"] == "oa_pdf"
        and row["license"] == "restricted-asn"
    )
    assert (
        row["retrieval_method"] == "operator_browser"
        and row["manual_intake"]["replaced_wrong_document"]
    )
    new = intake.paper_row(
        {
            **rec,
            "license": "cc-by",
            "status": "candidate",
            "access_status": "oa_unresolved",
            "pdf_path": "",
        },
        {**p, "action": "NEW-HTML", "kind": "html"},
        "l.md",
        "now",
    )
    assert (
        new["license"] == "cc-by"
        and new["status"] == "acquired"
        and new["access_status"] == "oa_html"
    )
    assert (
        new["review_status"] == "denied"
    )  # NEW never touches a verdict; only REPLACE clears one


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_html_fragment_takes_the_content_container_not_the_menus():
    body = "Lorem ipsum spectra. " * 100
    page = (
        "<html><body><div class='nav'><p>Home Programme Sessions SSHADE-BandList search</p></div>"
        "<div class='abstract'><h1>SSHADE-BandList, the new database of spectroscopy band lists of solids</h1>"
        f"<div class='authors'>B. Schmitt</div><div class='content'><p>{body}</p></div></div>"
        "<div class='footer'><p>Imprint Data protection</p></div></body></html>"
    )
    ast = json.loads(
        subprocess.run(
            ["pandoc", "-f", "html", "-t", "json"],
            input=page,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    blocks = intake.html_fragment(
        ast, "SSHADE-BandList, the new database of spectroscopy band lists of solids"
    )
    text = intake._stringify(blocks)
    assert "B. Schmitt" in text and "Lorem ipsum" in text
    assert "Imprint" not in text and "Programme" not in text


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_embedded_badges_and_their_empty_links_are_dropped():
    page = (
        "<p><b>B. Schmitt</b><a href='https://orcid.org/1'><img src='data:image/svg+xml;utf8,%3Csvg%3E'></a>, "
        "M. Furrer <img src='fig.png' alt='Figure 1'></p>"
    )
    ast = json.loads(
        subprocess.run(
            ["pandoc", "-f", "html", "-t", "json"],
            input=page,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    doc = {**ast, "blocks": intake._strip_embedded(ast["blocks"])}
    md = subprocess.run(
        ["pandoc", "-f", "json", "-t", "gfm-raw_html"],
        input=json.dumps(doc),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert (
        "data:" not in md
        and "orcid" not in md
        and "fig.png" in md
        and "B. Schmitt" in md
    )


def test_body_match_finds_a_listed_title_past_the_cover_pages(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for text in (
        "Serbian Ceramic Society Conference Book of Abstracts",
        "Editors and publisher",
        "KN 5 Synthesis of Ce/Ru Doped ZnO photocatalysts to the degradation of emerging pollutants. Abstract text.",
    ):
        doc.new_page().insert_text((72, 72), text, fontsize=8)
    f = tmp_path / "book.pdf"
    doc.save(f)
    papers = {
        "k69": {
            "title": "Synthesis of Ce/Ru Doped ZnO photocatalysts to the degradation of emerging pollutants"
        },
        "k1": {"title": "Something else entirely about Raman spectra of carbonates"},
    }
    assert intake.body_match(f, papers, {"k69": 69, "k1": 1}) == (
        "k69",
        "title on page 3",
    )
    assert (
        intake.body_match(f, papers, {"k1": 1})[0] is None
    )  # only LISTED titles count


def test_body_match_refuses_a_paper_that_cites_the_listed_title(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text(
        (72, 72),
        "npj heritage science https://doi.org/10.1038/s40494-025-01572-7 Yellow lakes in Rembrandt",
        fontsize=8,
    )
    doc.new_page().insert_text((72, 72), "Results and discussion", fontsize=8)
    doc.new_page().insert_text(
        (72, 72),
        "References 12. Identification and mapping of ancient pigments in a Roman Egyptian portrait",
        fontsize=8,
    )
    f = tmp_path / "s40494-025-01572-7.pdf"
    doc.save(f)
    papers = {
        "k99": {
            "paper_key": "k99",
            "doi": "10.1186/s40494-019-0348-9",
            "title": "Identification and mapping of ancient pigments in a Roman Egyptian portrait",
        }
    }
    plans = intake.plan_ingest([f], papers, {"k99": 99}, replace=False)
    assert (
        plans[0]["action"] == "UNMATCHED" and "different DOI" in plans[0]["why"]
    )  # cited in the body (page 3)
    doc = pymupdf.open()
    doc.new_page().insert_text(
        (72, 72),
        "npj heritage science https://doi.org/10.1038/s40494-025-01572-7 Yellow lakes in Rembrandt",
        fontsize=8,
    )
    doc.new_page().insert_text(
        (72, 72),
        "12. Identification and mapping of ancient pigments in a Roman Egyptian portrait",
        fontsize=8,
    )
    g = tmp_path / "short.pdf"
    doc.save(g)
    plans = intake.plan_ingest([g], papers, {"k99": 99}, replace=False)
    assert (
        plans[0]["action"] == "UNMATCHED" and "different DOI" in plans[0]["why"]
    )  # cited on page 2


def test_body_match_reads_a_line_numbered_manuscript(tmp_path):
    """2026-10-02: two author manuscripts number every line, the numbers land
    inside the title, and the verbatim match never saw them."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    for i, line in enumerate(
        (
            "Post-Landing Major Element Quantification Using",
            "SuperCam Laser Induced Breakdown Spectroscopy",
            "R.B. Anderson, O. Forni, A. Cousin",
        )
    ):
        page.insert_text((40, 72 + 14 * i), f"{i + 1}", fontsize=8)
        page.insert_text((72, 72 + 14 * i), line, fontsize=8)
    f = tmp_path / "manuscript.pdf"
    doc.save(f)
    papers = {
        "k31": {
            "title": "Post-landing major element quantification using SuperCam laser induced breakdown spectroscopy"
        },
        "k32": {"title": "Post-landing calibration of a different instrument entirely"},
    }
    assert intake.body_match(f, papers, {"k31": 31, "k32": 32}) == (
        "k31",
        "title on page 1 (line numbers ignored)",
    )


def test_filename_match_books_a_file_saved_under_its_listed_title(tmp_path):
    papers = {
        "k52": {
            "title": "Optimisation of fast quantification of fluorine content using handheld laser induced breakdown spectroscopy"
        },
        "k9": {"title": "A short title"},
    }
    f = tmp_path / (
        "Optimisation of fast quantification of fluorine content using handheld laser induced breakdown spectroscopy.pdf"
    )
    assert intake.filename_match(f, papers, {"k52": 52, "k9": 9}) == "k52"
    assert intake.filename_match(f, papers, {"k9": 9}) is None, "only LISTED titles"
    assert (
        intake.filename_match(tmp_path / "A short title.pdf", papers, {"k9": 9}) is None
    )


def test_assign_books_a_file_no_text_check_can_match(tmp_path, monkeypatch):
    """A scan with no text layer: the operator names the record."""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()  # a page with no text at all
    f = tmp_path / "scan.pdf"
    doc.save(f)
    papers = {"k74": {"title": "Archaeometric application of the Raman microprobe"}}
    monkeypatch.setattr(intake, "read_ledger", lambda: [])
    plans = intake.plan_ingest([f], papers, {"k74": 74}, False)
    assert plans[0]["action"] == "UNMATCHED"
    plans = intake.plan_ingest([f], papers, {"k74": 74}, False, {"scan.pdf": "k74"})
    assert plans[0]["key"] == "k74" and plans[0]["match"] == "assigned by the operator"
    assert plans[0]["action"] == "NEW"


def test_hal_id_match_reads_the_cover_sheet():
    papers = {
        "k85": {"oa_pdf_url": "https://theses.hal.science/tel-05326197/document"},
        "k86": {"oa_pdf_urls": ["https://hal.science/hal-04763842v1/document"]},
    }
    cover = "HAL Id: tel-05326197 https://theses.hal.science/tel-05326197v1 Submitted on 22 Oct 2025"
    assert intake.hal_id_match(cover, papers, {"k85": 85, "k86": 86}) == "k85"
    assert (
        intake.hal_id_match("HAL Id: hal-04763842", papers, {"k85": 85, "k86": 86})
        == "k86"
    )
    assert (
        intake.hal_id_match(cover, papers, {"k86": 86}) is None
    ), "only LISTED records"
    assert intake.hal_id_match("HAL Id: tel-0532619", papers, {"k85": 85}) is None


def test_compact_folds_accents():
    assert intake.compact("Greffage de copolymères antibactériens") == intake.compact(
        "GREFFAGE DE COPOLYMERES ANTIBACTERIENS"
    )
    assert intake.compact("ﬁbre") == "fibre", "ligatures still fold"
