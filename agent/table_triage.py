"""Vision triage of OCR'd tables against the printed page.

WHY (2026-10-02). PaddleOCR-VL reads table digits well but gets STRUCTURE
wrong often and invisibly: in the 48-table audit, 26 put values under the
wrong header or row (most often the header row loses the label column's cell
and every heading slides one column left), and only the page image shows it.
Packs inherit those as misattributed values that the grounding gate cannot
catch, because every number is printed somewhere.

The v1 bench (dev/bench_table_triage.py) showed muse, given the page image
and the OCR's HTML, catches every damaged table (35/35) and cuts rows with a
misplaced or wrong value by 64%, but it is not safe blind: it flagged 9 of 13
clean tables and corrupted 2 of them. v2, measured on the same 48 tables
(2026-10-03) and judged against the pages:

  * a prompt that asks for evidence and says most transcriptions are right:
    clean tables flagged 4 of 13 (v1: 9), and no clean table corrupted;
  * the model thinks in its own channel -- the caller sends a reasoning
    level, so the answer is the final channel only (LLMVP 2026-10-02);
  * plain VERDICT / PROBLEMS / TABLE lines (HTML inside JSON broke on
    unescaped quotes);
  * corrections: 30 better, 2 same, 3 worse of 35; number edits 99/106 right;
  * a deterministic GATE that keeps the OCR table when the correction LOSES
    numbers (the one worse fix that dropped printed values) or BREAKS A
    RECTANGULAR GRID into rows of unequal width (both other worse fixes: 20
    rows one cell short; a value outside the header columns). Every better
    fix kept a rectangular grid except one whose OCR grid was ragged already.
    Gated: 28 better, 2 same, 0 worse.

A blind A/B second read was tried as the gate and dropped: it preferred the
correction 34 times of 35, so it cost a vision call per fix for no signal.
An EMPTY answer is retryable, never a verdict: 5 of 48 first reads were
admitted with a shrunk generation budget on a busy server (1,723-2,254
tokens) and thinking used it all.
"""

from __future__ import annotations

import collections
import html as htmlmod
import re

#: Reasoning level (muse's own vocabulary) for the triage read.
TRIAGE_REASONING = "medium"

TRIAGE_PROMPT = """The image is one page of a scientific paper. Below is an HTML transcription of ONE table on this page, made by an OCR model.

Check the transcription against the printed table. For each column, read its printed header and confirm that the values under it in the transcription are the values the page prints in that column. For each row, confirm that the row label and its values belong together. Many transcriptions are entirely correct. Report only discrepancies you can point to in the image: a value under the wrong header or on the wrong row, values shifted along a row, cells merged or split, a missing row or column, a wrong digit.

Answer in exactly this format:
VERDICT: ok | fixed | unfixable
PROBLEMS:
- one line per discrepancy: the cell, what the transcription has, what the page prints
TABLE:
<table>...</table>

- ok: every value already sits under its printed header, on its printed row, with the printed digits. Write no PROBLEMS lines and no TABLE.
- fixed: write the corrected table after TABLE. Keep every value of the transcription; move cells, insert empty cells, fix header text and spans, split values the page prints in separate cells. Change or add a number only where the page clearly prints it and the transcription lost or misread it.
- unfixable: the table cannot be repaired from this page (for example it is not on this page); say why under PROBLEMS.

<transcription>
{table}
</transcription>"""

_NUM = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?(?![\d])")
_STYLE = re.compile(r"""\s+style=(['"]).*?\1""")
_BORDER = re.compile(r"\s+border=\d+")


def slim(table_html: str) -> str:
    """The OCR's HTML without per-cell style noise (spans kept)."""
    return _BORDER.sub("", _STYLE.sub("", table_html))


def same_table(a: str, b: str) -> bool:
    """Equal up to whitespace and cell styling."""
    return re.sub(r"\s+", "", slim(a)) == re.sub(r"\s+", "", slim(b))


def numbers(table_html: str) -> collections.Counter:
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", table_html))
    return collections.Counter(n.replace(",", ".") for n in _NUM.findall(text))


def numbers_outcome(before: str, after: str) -> dict:
    """How a correction treated the numbers.

    ``structure-only``: every number kept, none added (cells moved).
    ``from-image``: numbers changed or added, none lost outright.
    ``lost``: numbers dropped with nothing in their place.
    Splitting a fused cell ("123,728" -> "12" + "3,728") is structure: a
    number that disappeared and is the concatenation of two that appeared
    is not counted as lost."""
    a, b = numbers(before), numbers(after)
    missing, pool = list((a - b).elements()), list((b - a).elements())
    unexplained = []
    for m in missing:
        hit = next(
            (
                (i, j)
                for i in range(len(pool))
                for j in range(len(pool))
                if i != j and pool[i] + pool[j] == m
            ),
            None,
        )
        if hit:
            for k in sorted(hit, reverse=True):
                pool.pop(k)
        else:
            unexplained.append(m)
    if not unexplained and not pool:
        kind = "structure-only"
    elif unexplained and not pool:
        kind = "lost"
    else:
        kind = "from-image"
    return {"kind": kind, "missing": unexplained[:20], "added": pool[:20]}


def parse_triage(text: str) -> dict:
    """{verdict, problems, html} from the VERDICT / PROBLEMS / TABLE answer.
    The LAST verdict line wins (an answer can restate itself); the table is
    the last one after the last VERDICT. ``verdict`` is "unparsed" when no
    line carries one."""
    found = list(re.finditer(r"VERDICT:\s*\**\s*(ok|fixed|unfixable)\b", text, re.I))
    if not found:
        return {"verdict": "unparsed", "problems": [], "html": ""}
    last = found[-1]
    verdict = last.group(1).lower()
    tail = text[last.end() :]
    probs = re.search(r"PROBLEMS:\s*(.*?)(?:\n\s*TABLE:|\Z)", tail, re.S | re.I)
    problems = [
        ln.strip().lstrip("-* ").strip()
        for ln in (probs.group(1).splitlines() if probs else [])
        if ln.strip().lstrip("-* ").strip()
    ]
    html = ""
    if verdict == "fixed" and "<table" in tail:
        t = tail[tail.rindex("<table") :]
        end = t.find("</table>")
        html = t[: end + len("</table>")] if end != -1 else ""
    return {"verdict": verdict, "problems": problems, "html": html}


def grid_widths(table_html: str) -> list[int]:
    """Each row's width in columns, colspans counted and rowspans carried down."""
    rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", table_html, re.S | re.I)
    carry: list[tuple[int, int]] = []  # (rows still covered, width)
    out = []
    for row in rows:
        cells = re.findall(r"<t[dh]\b([^>]*)>", row, re.I)
        spans = []
        for attrs in cells:
            c = re.search(r"colspan\s*=\s*['\"]?(\d+)", attrs, re.I)
            r = re.search(r"rowspan\s*=\s*['\"]?(\d+)", attrs, re.I)
            spans.append((int(c.group(1)) if c else 1, int(r.group(1)) if r else 1))
        out.append(sum(c for c, _ in spans) + sum(w for _, w in carry))
        carry = [(n - 1, w) for n, w in carry if n > 1]
        carry += [(r - 1, c) for c, r in spans if r > 1]
    return out


def is_rectangular(table_html: str) -> bool:
    """Every non-empty row spans the same number of columns."""
    return len({w for w in grid_widths(table_html) if w}) <= 1


def gate(triage: dict, ocr_html: str) -> dict:
    """Apply the correction or keep the OCR table.

    Returns {"apply": bool, "reason": str, "numbers": numbers_outcome | None}.
    Only a "fixed" verdict with a changed table is a candidate. Lost numbers
    keep the OCR table, and so does a correction that breaks the OCR's
    rectangular grid; everything else applies."""
    html = triage.get("html") or ""
    if triage.get("verdict") != "fixed" or not html:
        return {
            "apply": False,
            "reason": f"verdict {triage.get('verdict')}",
            "numbers": None,
        }
    if same_table(html, ocr_html):
        return {"apply": False, "reason": "no-op fix", "numbers": None}
    nums = numbers_outcome(slim(ocr_html), html)
    if nums["kind"] == "lost":
        return {"apply": False, "reason": "numbers lost", "numbers": nums}
    if is_rectangular(ocr_html) and not is_rectangular(html):
        return {
            "apply": False,
            "reason": "grid broken (rows of unequal width)",
            "numbers": nums,
        }
    return {"apply": True, "reason": f"applied ({nums['kind']})", "numbers": nums}
