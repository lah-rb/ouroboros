"""Speed-read compression for oversized curator docs.

WHY. The curate doc budget is pool-bound (`_curate_doc_budget_chars`),
and papers over it are parked unreviewed — 491 of 546 pending at the
65,536-cell pool (2026-08-23). Blind agreement check (42 verdicted
papers re-reviewed on compressed docs, ~/tmp/compress_agreement.jsonl):
95% verdict agreement, zero parse failures, and BOTH misses were
conservative accept->deny flips at the deepest compressions (ratio
<= 0.37). Hence the LADDER below: a doc is only ever compressed as far
as needed to fit, so the deep rungs touch only docs whose alternative
is staying parked forever.

WHAT EACH RUNG DOES:
  raw     — untouched (fits as-is; the only rung most docs ever see)
  tables  — HTML tables -> pipe markdown. LOSSLESS: paddle emits every
            cell as <td style='text-align: center; ...'> and a single
            117 KB table measured ~85% styling bytes.
  gentle  — + middle paragraphs of each section keep first/last TWO
            sentences; runs of >= 8 short blocks keep 3 + 3.
  full    — + first/last ONE sentence, runs of >= 5 keep 2 + 2, and
            big blocks with no sentence structure (TOCs, data columns)
            collapse by lines. This is the form the agreement check
            validated.

INVARIANTS (tested):
  * Blocks containing figure tags (<img, ![) are NEVER squeezed or
    elided — figtext anchoring and pack grounding match against them.
  * Tables, code fences and headings are never dropped.
  * Every elision leaves an explicit [… N … elided …] marker, so the
    curator knows it is reading an abridged doc.
"""

from __future__ import annotations

import re

# Sentence boundary: terminal punctuation then whitespace then an
# uppercase-ish start (Latin, accented, Cyrillic, CJK bracket, digit).
_SENT_RE = re.compile(r"(?<=[.!?。])\s+(?=[A-ZÀ-ÞА-Я0-9「(])")

_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_TR_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_TD_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.S | re.I)
# Strip markup INSIDE table cells — except figure tags: paddle places
# figures inside table cells in ~10% of table-bearing docs (57 of 547
# pending, corpus validation 2026-08-23), and dropping the anchor breaks
# figtext inlining and pack grounding.
_TAG_RE = re.compile(r"<(?!img\b)[^>]+>")

_HAS_FIG_RE = re.compile(r"<img\b|!\[")

#: rung name -> knobs; None knobs = no prose squeezing (tables only).
LADDER = ("raw", "tables", "gentle", "full")


def _html_table_to_pipes(match: re.Match) -> str:
    rows = []
    for tr in _TR_RE.findall(match.group(0)):
        cells = [_TAG_RE.sub("", c).strip() for c in _TD_RE.findall(tr)]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return match.group(0)
    if len(rows) >= 2:
        rows.insert(1, "|" + "---|" * max(1, rows[0].count("|") - 1))
    return "\n".join(rows)


def html_tables_to_pipes(md: str) -> str:
    """Convert paddle's styled HTML tables to pipe markdown. Lossless."""
    return _TABLE_RE.sub(_html_table_to_pipes, md)


def _is_table_block(block: str) -> bool:
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if not lines:
        return False
    piped = sum(1 for ln in lines if ln.lstrip().startswith("|") or ln.count("|") >= 2)
    return piped >= max(1, len(lines) // 2)


def _is_figure_block(block: str) -> bool:
    s = block.strip()
    if _HAS_FIG_RE.search(s):
        return True
    return s.lower().startswith(("figure", "fig.", "table", "tab.")) and len(s) < 600


def _squeeze(block: str, keep: int, line_fallback: bool) -> str:
    if _HAS_FIG_RE.search(block):
        return block  # never squeeze a block anchoring a figure
    sents = _SENT_RE.split(block.strip())
    if len(sents) > 2 * keep:
        return " ".join(sents[:keep]) + " […] " + " ".join(sents[-keep:])
    if line_fallback and len(block) > 1200:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) >= 6:
            return (
                "\n".join(lines[:2])
                + f"\n[… {len(lines) - 4} lines elided …]\n"
                + "\n".join(lines[-2:])
            )
    return block


def speedread(md: str, gentle: bool) -> str:
    """First/last paragraph of each section kept whole; middles squeezed."""
    keep = 2 if gentle else 1
    short, run_min, edge = (600, 8, 3) if gentle else (600, 5, 2)
    line_fallback = not gentle

    sections: list[list[str]] = []
    cur: list[str] = []
    for ln in md.splitlines():
        if ln.startswith("#"):
            if cur:
                sections.append(cur)
            cur = [ln]
        else:
            cur.append(ln)
    if cur:
        sections.append(cur)

    out = []
    for sec in sections:
        blocks = [b for b in re.split(r"\n\s*\n", "\n".join(sec)) if b.strip()]
        kinds = [
            (
                "h"
                if b.strip().startswith("#")
                else (
                    "t"
                    if _is_table_block(b)
                    else (
                        "f"
                        if _is_figure_block(b)
                        else "c" if b.lstrip().startswith("```") else "p"
                    )
                )
            )
            for b in blocks
        ]
        prose = [i for i, k in enumerate(kinds) if k == "p"]
        keep_full = {prose[0], prose[-1]} if prose else set()
        new: list[str] = []
        i = 0
        while i < len(blocks):
            b, k = blocks[i], kinds[i]
            if (
                k == "p"
                and len(b) < short
                and i not in keep_full
                and not _HAS_FIG_RE.search(b)
            ):
                j = i
                while (
                    j < len(blocks)
                    and kinds[j] == "p"
                    and len(blocks[j]) < short
                    and j not in keep_full
                    and not _HAS_FIG_RE.search(blocks[j])
                ):
                    j += 1
                if j - i >= run_min:
                    new.extend(blocks[i : i + edge])
                    new.append(f"[… {j - i - 2 * edge} similar short entries elided …]")
                    new.extend(blocks[j - edge : j])
                    i = j
                    continue
            if k in ("h", "t", "f", "c") or i in keep_full:
                new.append(b)
            else:
                new.append(_squeeze(b, keep, line_fallback))
            i += 1
        out.append("\n\n".join(new))
    return "\n\n".join(out)


def compress_rung(md: str, rung: str) -> str:
    """The markdown at one named rung of the ladder."""
    if rung == "raw":
        return md
    detabled = html_tables_to_pipes(md)
    if rung == "tables":
        return detabled
    return speedread(detabled, gentle=(rung == "gentle"))


def fig_tag_count(md: str) -> int:
    """Figure anchors in a doc — the count every rung must preserve."""
    return len(_HAS_FIG_RE.findall(md))
