"""Vision triage of OCR'd tables against the printed page.

WHY (2026-10-02). PaddleOCR-VL reads table digits well but gets STRUCTURE
wrong often and invisibly: in the 48-table audit, 26 put values under the
wrong header or row (most often the header row loses the label column's cell
and every heading slides one column left), and only the page image shows it.
Packs inherit those as misattributed values that the grounding gate cannot
catch, because every number is printed somewhere.

The v1 bench (dev/bench_table_triage.py) showed muse, given the page image
and the OCR's HTML, catches every damaged table (35/35) and cuts rows with a
misplaced or wrong value by 64%, but it is not safe blind. It flagged 9 of 13
clean tables, and all three corrections that made a table WORSE kept every
number and only moved cells, so a number check cannot stop them. v2 is what
this module holds:

  * a prompt that asks for evidence and says most transcriptions are right
    (v1 named the expected failure, and muse found it everywhere);
  * plain VERDICT / PROBLEMS / TABLE lines (HTML inside JSON broke on
    unescaped quotes);
  * the model thinks in its own channel: the caller sends a reasoning level,
    so the answer is the final channel only (LLMVP 2026-10-02);
  * a GATE: corrections that lose numbers keep the OCR table; corrections
    that read numbers from the image are applied (v1: 20 better, 0 worse);
    corrections that only move cells are applied only when a blind A/B read
    of the page prefers them.
"""

from __future__ import annotations

import collections
import html as htmlmod
import random
import re

#: Reasoning levels (muse's own vocabulary) for the two reads.
TRIAGE_REASONING = "medium"
VERIFY_REASONING = "low"

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

VERIFY_PROMPT = """The image is one page of a scientific paper. Below are two HTML transcriptions, A and B, of the same table on this page. Compare each with the printed table: which one puts every value under its printed column header, on its printed row, with the printed digits?

Answer in exactly this format:
CHOICE: A | B | SAME
REASON: one line

SAME means both are equally right or equally wrong.

<A>
{a}
</A>

<B>
{b}
</B>"""

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


def parse_choice(text: str) -> str:
    """ "A" | "B" | "SAME" | "unparsed" -- the last CHOICE line."""
    found = re.findall(r"CHOICE:\s*\**\s*(A|B|SAME)\b", text, re.I)
    return found[-1].upper() if found else "unparsed"


def verify_order(key: str) -> bool:
    """True when the CORRECTION is shown as A. Seeded by the table's key so a
    re-run asks the same question, and balanced across tables so a model's
    letter bias cannot pass as a preference."""
    return random.Random(f"table-verify:{key}").random() < 0.5


def verify_prompt(key: str, ocr_html: str, fixed_html: str) -> str:
    fixed_first = verify_order(key)
    a, b = (fixed_html, ocr_html) if fixed_first else (ocr_html, fixed_html)
    return VERIFY_PROMPT.format(a=slim(a), b=slim(b))


def prefers_fix(key: str, choice: str) -> bool | None:
    """Did the blind read prefer the correction? None when it did not choose."""
    if choice not in ("A", "B"):
        return None
    return (choice == "A") == verify_order(key)


def gate(triage: dict, ocr_html: str, verify_choice: str | None, key: str) -> dict:
    """Apply the correction or keep the OCR table.

    Returns {"apply": bool, "reason": str, "numbers": numbers_outcome | None}.
    Only a "fixed" verdict with a changed table is a candidate; then lost
    numbers keep the OCR table, numbers read from the image apply, and a
    cells-only move applies only when the blind read preferred it."""
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
    if nums["kind"] == "from-image":
        return {"apply": True, "reason": "numbers read from the image", "numbers": nums}
    pref = prefers_fix(key, verify_choice or "")
    if pref:
        return {
            "apply": True,
            "reason": "cells moved; blind read prefers the fix",
            "numbers": nums,
        }
    why = "blind read prefers the OCR" if pref is False else "blind read undecided"
    return {"apply": False, "reason": f"cells moved; {why}", "numbers": nums}
