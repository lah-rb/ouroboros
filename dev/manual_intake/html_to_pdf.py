#!/usr/bin/env python3
"""Rebuild an article PDF from a publisher page saved as "Web page, complete".

WHY (2026-10-01). Some manual-intake articles can only be had as the full-text
HTML page (the PDF link is walled). The intake path and OCR expect a PDF, so this
renders the saved page's ARTICLE -- not the site around it -- into a clean PDF
with WeasyPrint: a first page carrying title, authors, journal, DOI and licence
(what intake's first-page verification reads), then the abstract, body, tables,
figures with their captions, and references.

Images come ONLY from the page's saved `<name>_files/` folder. Publishers lazy-load
figures, so a page saved without scrolling holds only the figures that rendered,
and the CDN behind them is bot-walled (Wiley: Cloudflare, for curl and headless
Chromium alike). A figure or equation that was not saved is replaced by a labelled
placeholder naming the DOI, and the run reports the count: re-save the page after
scrolling through every figure and run again to complete it.

Written for Wiley Online Library's markup (citation_* meta tags, an
`article-section__abstract`, an `article__body`, `figure`/`figcaption`,
`article-table-content-wrapper`, `article-section__references`); other
publishers need their own selectors added to BODY_SELECTORS / ABSTRACT_SELECTORS.

    .venv/bin/python dev/manual_intake/html_to_pdf.py "<saved page>.html" [-o out.pdf]
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ABSTRACT_SELECTORS = ["section.article-section__abstract", "div.abstract-group"]
BODY_SELECTORS = ["div.article__body", "section.article-section__full", "article"]
REFS_SELECTORS = ["section.article-section__references", "#references-section"]
DROP_SELECTORS = [
    "script",
    "style",
    "noscript",
    "button",
    "nav",
    "aside",
    "picture > source",
    "a.open-figure-link",
    "a.ppt-figure-link",
    "div.figure-extra",
    ".article-table-content-wrapper + div.article-section__table-footnotes a",
    ".accordion__control",
    ".article-section__references h2 + .references__add",
]

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm;
        @bottom-center { content: counter(page); font-size: 8pt; color: #555; } }
body { font-family: "DejaVu Serif", serif; font-size: 10pt; line-height: 1.38; color: #111; }
h1 { font-size: 16pt; line-height: 1.2; margin: 0 0 6pt; }
h2 { font-size: 12pt; margin: 14pt 0 4pt; }
h3, h4 { font-size: 10.5pt; margin: 10pt 0 3pt; }
.meta { font-size: 9pt; color: #333; margin-bottom: 10pt; }
.meta p { margin: 1pt 0; }
.authors { font-size: 10pt; margin: 0 0 4pt; }
figure { margin: 8pt 0; page-break-inside: avoid; }
figure img { max-width: 100%; max-height: 190mm; display: block; margin: 0 auto; }
figcaption { font-size: 8.5pt; margin-top: 3pt; }
table { border-collapse: collapse; width: 100%; font-size: 8.5pt; margin: 6pt 0; }
th, td { border: 0.5pt solid #777; padding: 2pt 3pt; vertical-align: top; }
img.fallback__image, img[alt="mathematical equation"] { vertical-align: middle; max-height: 14pt; }
.missing { border: 1pt dashed #999; padding: 8pt; font-size: 8.5pt; color: #444;
           text-align: center; margin: 6pt 0; }
.missing-inline { font-size: 8pt; color: #555; }
.refs { font-size: 8.5pt; }
"""


def _meta(soup, name: str) -> list[str]:
    return [
        html.unescape(m.get("content", "")).strip()
        for m in soup.find_all("meta", attrs={"name": name})
        if m.get("content")
    ]


def _first(soup, selectors):
    for sel in selectors:
        hit = soup.select_one(sel)
        if hit is not None:
            return hit
    return None


def _clean(node) -> None:
    for sel in DROP_SELECTORS:
        for el in node.select(sel):
            el.decompose()
    for a in node.find_all("a"):  # links: keep the text, drop the target
        a.unwrap()


def _resolve_images(node, page: Path, files_dir: Path, doi: str, stats: dict) -> None:
    """Point every <img> at a saved file, or replace it with a labelled placeholder."""
    saved = {p.stem: p for p in files_dir.iterdir()} if files_dir.is_dir() else {}
    for img in node.find_all("img"):
        cands = [img.get("src") or "", img.get("data-lg-src") or ""]
        span = img.find_previous_sibling("span", class_="fallback__mathEquation")
        if span is not None:
            cands.append(span.get("data-altimg") or "")
        path = None
        for c in cands:
            if (
                not c
                or c.startswith(("http", "/cms", "data:"))
                and not c.startswith("data:")
            ):
                stem = Path(c.split("?")[0]).stem if c else ""
                if stem in saved:
                    path = saved[stem]
                    break
                continue
            p = (page.parent / c).resolve()
            if p.is_file():
                path = p
                break
            if Path(c).stem in saved:
                path = saved[Path(c).stem]
                break
        is_math = "math" in " ".join(cands) or img.get("alt") == "mathematical equation"
        if path is not None:
            img["src"] = path.as_uri()
            for attr in ("srcset", "data-lg-src", "loading"):
                img.attrs.pop(attr, None)
            stats["math_ok" if is_math else "img_ok"] += 1
            continue
        stats["math_missing" if is_math else "img_missing"] += 1
        repl = BeautifulSoup("", "html.parser").new_tag(
            "span" if is_math else "div",
            attrs={"class": "missing-inline" if is_math else "missing"},
        )
        repl.string = (
            "[equation image not captured in the saved page]"
            if is_math
            else f"[Figure image not captured in the saved page — see https://doi.org/{doi}]"
        )
        img.replace_with(repl)
    for span in node.select("span.fallback__mathEquation"):
        span.decompose()


def build(page: Path, out: Path) -> dict:
    soup = BeautifulSoup(
        page.read_text(encoding="utf-8", errors="replace"), "html.parser"
    )
    files_dir = page.with_name(page.stem + "_files")
    title = (_meta(soup, "citation_title") or [page.stem])[0]
    doi = (_meta(soup, "citation_doi") or [""])[0]
    authors = _meta(soup, "citation_author")
    journal = (_meta(soup, "citation_journal_title") or [""])[0]
    vol = (_meta(soup, "citation_volume") or [""])[0]
    issue = (_meta(soup, "citation_issue") or [""])[0]
    page_no = (_meta(soup, "citation_firstpage") or [""])[0]
    date = (_meta(soup, "citation_publication_date") or [""])[0]
    publisher = (_meta(soup, "citation_publisher") or [""])[0]
    lic = soup.find("a", href=re.compile(r"creativecommons\.org/licenses/"))
    licence = lic["href"] if lic else ""

    stats = {"img_ok": 0, "img_missing": 0, "math_ok": 0, "math_missing": 0}
    parts = []
    for selectors, label in (
        (ABSTRACT_SELECTORS, "abstract"),
        (BODY_SELECTORS, "body"),
        (REFS_SELECTORS, "references"),
    ):
        node = _first(soup, selectors)
        if node is None:
            if label == "body":
                raise SystemExit(
                    f"no article body found ({BODY_SELECTORS}) — add selectors"
                )
            continue
        # Wiley's article__body CONTAINS the abstract section: emitting it on
        # its own as well printed the abstract twice.
        if label == "abstract":
            body = _first(soup, BODY_SELECTORS)
            if body is not None and node in body.descendants:
                continue
        _clean(node)
        _resolve_images(node, page, files_dir, doi, stats)
        parts.append(
            f'<div class="{"refs" if label == "references" else label}">{node}</div>'
        )

    citation = ", ".join(
        x
        for x in (
            journal,
            f"vol. {vol}" if vol else "",
            f"issue {issue}" if issue else "",
            page_no,
            date.replace("/", "-"),
        )
        if x
    )
    head = (
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="authors">{html.escape(", ".join(authors))}</p>'
        '<div class="meta">'
        f"<p>{html.escape(citation)}{' — ' + html.escape(publisher) if publisher else ''}</p>"
        f"<p>DOI: https://doi.org/{html.escape(doi)}</p>"
        + (f"<p>License: {html.escape(licence)}</p>" if licence else "")
        + "<p>Rebuilt from the publisher's full-text HTML page (saved copy); "
        "layout differs from the published PDF.</p></div>"
    )
    doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body>{head}{''.join(parts)}</body></html>"
    )
    from weasyprint import HTML

    HTML(string=doc, base_url=str(page.parent)).write_pdf(str(out))
    return {"title": title, "doi": doi, "licence": licence, **stats}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("page", type=Path)
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()
    page = args.page.resolve()
    out = args.out or page.with_suffix(".pdf")
    info = build(page, out)
    print(f"wrote {out} ({os.path.getsize(out):,} bytes)")
    for k, v in info.items():
        print(f"  {k}: {v}")
    if info["img_missing"] or info["math_missing"]:
        print(
            "  INCOMPLETE: re-save the page after scrolling through every figure "
            "(lazy-loaded images are only saved once rendered), then run again."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
