"""Where a paper's reference list starts: the one heading definition.

ONE SOURCE OF TRUTH (2026-10-03). Three consumers needed it and had grown
two definitions: the translation gate (strip_reference_section, 2026-08-18)
and the biblio lane's reference-DOI extraction shared a narrow pattern --
bare English/CJK/Korean/Russian headings only -- while the pack windows
(2026-10-03) got a wider one. The narrow one missed numbered and bold
headings ("## 7. References", "**REFERENCES**"), Portuguese "Referências",
Spanish "Bibliografía", French "Références bibliographiques" and "Literature
cited"; five of the eighteen accepted papers booked pack_failed for a failed
translation had in fact translated correctly and were failed on their
reference lists, which the translator reformats (gate 0.80-0.97 -> 0.98-1.00
with the list excluded).

A heading line: optional markdown level, optional numbering (arabic or roman),
optional bold, the name, an optional trailing period or colon.
"""

from __future__ import annotations

import re

BIBLIOGRAPHY_HEADING = re.compile(
    r"^[ \t]*#{1,6}[ \t]*(?:[\dIVX]+[.)]?[ \t]*)?(?:\*\*)?[ \t]*(?:"
    r"references?(?:[ \t]+and[ \t]+notes|[ \t]+cited)?|notes[ \t]+and[ \t]+references"
    r"|bibliography|bibliographie|literature[ \t]+cited|works[ \t]+cited|cited[ \t]+literature"
    r"|refer[êe]ncias(?:[ \t]+bibliogr[áa]ficas)?|referencias(?:[ \t]+bibliogr[áa]ficas)?"
    r"|r[ée]f[ée]rences(?:[ \t]+bibliographiques)?|bibliograf[íi]a|literatur(?:verzeichnis)?"
    r"|参\s*考\s*文\s*献|引\s*用\s*文\s*献|文\s*献|참\s*고\s*문\s*헌"
    r"|список[ \t]+литературы|литература"
    r")[ \t]*(?:\*\*)?[ \t]*[.:]?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


_HEADING = re.compile(r"^[ \t]*(#{1,6})[ \t]+\S", re.MULTILINE)


def bibliography_spans(md: str) -> list[tuple[int, int]]:
    """Every reference-list SECTION: from its heading to the next heading of
    the same or a higher level (or the end).

    Not "everything after the first heading": a report's annexes and a
    thesis's later chapters follow their reference lists, and cutting there
    blinded the translation gate to data lost in an annex (SPECTRHABENT,
    2026-10-03: a quarter of its data numbers lost, scored 0.997)."""
    md = md or ""
    heads = [(m.start(), len(m.group(1))) for m in _HEADING.finditer(md)]
    spans = []
    for m in BIBLIOGRAPHY_HEADING.finditer(md):
        level = len(re.match(r"[ \t]*(#{1,6})", m.group(0)).group(1))
        end = next((at for at, lv in heads if at > m.start() and lv <= level), len(md))
        spans.append((m.start(), end))
    return spans


def bibliography_start(md: str) -> int:
    """Offset of the first reference-list heading, or -1 when there is none."""
    spans = bibliography_spans(md)
    return spans[0][0] if spans else -1


def strip_bibliography(md: str) -> str:
    """The text without its reference-list sections; the whole text when no
    heading matches (over-stripping would blind a gate for real)."""
    out, pos = [], 0
    for start, end in bibliography_spans(md):
        out.append(md[pos:start])
        pos = end
    out.append((md or "")[pos:])
    return "".join(out)
