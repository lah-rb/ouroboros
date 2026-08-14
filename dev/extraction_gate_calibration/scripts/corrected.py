#!/usr/bin/env python3
"""Recompute the quality rates with MARGIN LINE NUMBERS removed from the truth.

`_prose_text` reads PyMuPDF "blocks", and PyMuPDF merges a margin line-number
into the same block as the prose line beside it. On a line-numbered manuscript
(Elsevier accepted-manuscript PDFs, and most preprints) that puts one spurious
integer into the truth PER LINE OF TEXT. Our markdown correctly omits them, so
each one scores as a numeric miss: ~40 phantom misses per page.

Detection is geometric, not sequential: a span that is PURE DIGITS and sits
outside the horizontal span of the page's prose is furniture — a line number or
a page number — never a measurement. Sequentiality is used only to report.
"""

from __future__ import annotations

import json
import re
import statistics
import sys

sys.path.insert(0, "/Users/lah-rb/Repos/ouroboros/tools/pdf_extract")

import fitz  # noqa: E402
from extract_batch import (  # noqa: E402
    _CJK_RE,
    _NUM_RE,
    _PROSE_DIGIT_FRAC,
    _PROSE_MIN_BLOCK,
    _SPAN_WORDS,
    _SPANS_PER_PAGE,
    _figure_regions,
    _norm,
)

_PURE_DIGIT = re.compile(r"\d{1,4}")

# PUBLISHER FURNITURE. Numbers the extractor is RIGHT to drop: the access
# stamp a publisher injects at download time, the DOI/ISSN, licence versions,
# contact numbers, the running volume/page footer. Measured on tier-A
# extractions: these are the ENTIRE miss set on short papers (P26: 47 of 47).
_FURNITURE = re.compile(
    r"downloaded from|ip address|doi\s*:|doi\.org|dx\.doi|creativecommons|"
    r"creative commons|issn|e-?mail|tel\.?:|fax|all rights reserved|"
    r"see front matter|this content was|©|\(c\)\s*20\d\d|"
    r"published by elsevier|licence|license",
    re.I,
)
_MARGIN_PAD = 6.0  # pt of slack, so a hanging indent is not called a margin


def _gutter_spans(page) -> list:
    """bboxes of pure-digit spans lying outside the prose column."""
    prose_x0, prose_x1, digits = [], [], []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            for sp in ln["spans"]:
                t = sp["text"].strip()
                if not t:
                    continue
                if _PURE_DIGIT.fullmatch(t):
                    digits.append(sp["bbox"])
                else:
                    prose_x0.append(sp["bbox"][0])
                    prose_x1.append(sp["bbox"][2])
    if not digits or len(prose_x0) < 5:
        return []
    left, right = min(prose_x0), max(prose_x1)
    return [b for b in digits if b[2] < left - _MARGIN_PAD or b[0] > right + _MARGIN_PAD]


FURNITURE_FILTER = True


def prose_text_fixed(page) -> tuple:
    """(_prose_text-equivalent text, n_line_numbers_dropped).

    IDENTICAL to the shipping oracle except that a margin pure-digit span is
    removed from the line it was merged into. The figure-region test stays at
    BLOCK granularity deliberately: moving it to spans would change a second
    thing at the same time, and then the measurement could not attribute its
    own delta.
    """
    regions = _figure_regions(page)
    gutters = _gutter_spans(page)
    gset = {(round(b[0], 1), round(b[1], 1)) for b in gutters}

    parts, dropped = [], 0
    for blk in page.get_text("dict")["blocks"]:
        bb = blk.get("bbox")
        if bb is None or "lines" not in blk:
            continue
        if regions:
            cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
            if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in regions):
                continue
        chunks = []
        for ln in blk["lines"]:
            for sp in ln["spans"]:
                if (round(sp["bbox"][0], 1), round(sp["bbox"][1], 1)) in gset:
                    dropped += 1
                    continue
                chunks.append(sp["text"])
            chunks.append("\n")
        text = "".join(chunks)
        if not text.strip():
            continue
        if FURNITURE_FILTER:
            text = "\n".join(
                ln for ln in text.split("\n") if not _FURNITURE.search(ln)
            )
            if not text.strip():
                continue
        stripped = re.sub(r"[\s,.\-–—°%()×±]+", "", text)
        if (
            len(text.strip()) < _PROSE_MIN_BLOCK
            and stripped
            and sum(c.isdigit() for c in stripped) / len(stripped) > _PROSE_DIGIT_FRAC
        ):
            continue
        parts.append(text)
    return "\n".join(parts), dropped


def rates(pdf_path: str, pages_md: list, fixed: bool) -> tuple:
    doc = fitz.open(pdf_path)
    nh = nt = sh = st = 0
    verified = dropped_total = 0
    aligned = len(pages_md) == doc.page_count
    whole = _norm("\n".join(pages_md))
    whole_c = re.sub(r"\s", "", whole)
    whole_w = set(whole.split())
    for i, page in enumerate(doc):
        if fixed:
            raw, dropped = prose_text_fixed(page)
            dropped_total += dropped
        else:
            from extract_batch import _prose_text

            raw, dropped = _prose_text(page), 0
        truth = _norm(raw)
        if len(truth.strip()) < 200:
            continue
        verified += 1
        if aligned:
            md_n = _norm(pages_md[i])
            md_c = re.sub(r"\s", "", md_n)
            md_w = set(md_n.split())
        else:
            md_n, md_c, md_w = whole, whole_c, whole_w
        for n in _NUM_RE.findall(truth):
            nt += 1
            if n in md_c or n in md_n:
                nh += 1
        words = _CJK_RE.sub(" ", truth).split()
        if len(words) >= _SPAN_WORDS:
            step = max(1, (len(words) - _SPAN_WORDS) // _SPANS_PER_PAGE)
            spans = [
                words[j : j + _SPAN_WORDS]
                for j in range(0, len(words) - _SPAN_WORDS + 1, step)
            ][:_SPANS_PER_PAGE]
            for sw in spans:
                st += 1
                if sum(1 for w in sw if w in md_w) >= len(sw) - 1:
                    sh += 1
    doc.close()
    return (
        nh / nt if nt else 1.0,
        sh / st if st else 1.0,
        nt,
        verified,
        dropped_total,
    )


def main() -> None:
    cases = json.loads(open(sys.argv[1]).read())
    sink = open(sys.argv[2], "w") if len(sys.argv) > 2 else None
    out = []
    for c in cases:
        pages_md = open(c["md"], errors="replace").read().split("\n\n---\n\n")
        on, os_, ont, _, _ = rates(c["pdf"], pages_md, fixed=False)
        fn, fs, fnt, ver, dropped = rates(c["pdf"], pages_md, fixed=True)
        out.append(
            {
                "case_id": c["case_id"],
                "arm": c["arm"],
                "gate_numeric": c["numeric"],
                "gate_span": c["span"],
                "old_numeric": round(on, 4),
                "old_span": round(os_, 4),
                "new_numeric": round(fn, 4),
                "new_span": round(fs, 4),
                "nums_old": ont,
                "nums_new": fnt,
                "line_numbers_dropped": dropped,
                "verified_pages": ver,
            }
        )
        if sink:
            sink.write(json.dumps(out[-1]) + "\n")
            sink.flush()
        print(
            f"{c['case_id']} {c['arm']:4s} num {on:.3f} -> {fn:.3f}   "
            f"span {os_:.3f} -> {fs:.3f}   linenos={dropped}",
            file=sys.stderr,
        )
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
