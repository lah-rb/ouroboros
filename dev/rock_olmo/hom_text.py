"""Handbook of Mineralogy sheets -> stage-1 prose (root venv). LICENCE-FLAGGED.

WHAT THE SHEETS ARE. ~/corpora/mineral-refs/hom/<Name>.pdf: 3,872 one-page
species sheets (Mineral Data Publishing, © 2001–2005; included for this
private research run by operator decision 2026-09-07, every record tagged
`license: restricted-hom`). Every sheet has a text layer, so `pdftotext
-layout` reads it without OCR — but the older "version 1.2" sheets were set
in Computer Modern with a non-standard encoding, and pdftotext reports the
glyphs by their font slots. Measured over 200 sheets (2026-09-07):

    f001g  -> {001}          Miller indices lose their braces to f…g
    2{1    -> 2–1            the en dash in ranges comes out as "{"
    °uorite -> fluorite      the "fl" ligature prints as °
    ¯ne     -> fine           the "fi" ligature prints as ¯
    Je®erson -> Jefferson     the "ff" ligature prints as ®
    Dauphin¶ e -> Dauphiné    acute accent as ¶ before the vowel
    Isµere  -> Isère          grave accent as µ
    573 ± C -> 573 °C         degree sign as ±
    \\twisted." -> "twisted." opening quote as backslash
    ! = 1.544 / ² = 1.553    ω and ε in the optical-class line

The newer "version 1" sheets are clean Unicode and must pass unchanged, so
every rule is keyed to a glyph or a position that clean text never uses.
The copyright and "All rights reserved" furniture is dropped; the section
headers (Crystal Data … References) are kept — they are how the sheet is
structured and how stage-2 facts will be lifted from it later.
"""

from __future__ import annotations

import glob
import os
import re
import subprocess

HOM_DIR = os.path.expanduser("~/corpora/mineral-refs/hom")
LICENSE = "restricted-hom"

SECTIONS = (
    "Crystal Data",
    "Physical Properties",
    "Optical Properties",
    "Cell Data",
    "X-ray Powder Pattern",
    "Chemistry",
    "Polymorphism & Series",
    "Mineral Group",
    "Occurrence",
    "Association",
    "Distribution",
    "Name",
    "Type Material",
    "References",
)
_ACUTE = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú", "y": "ý"}
_GRAVE = {"a": "à", "e": "è", "i": "ì", "o": "ò", "u": "ù"}

_RULES: list[tuple[re.Pattern, object]] = [
    (re.compile(r"\bf(\d{3,4})g\b"), r"{\1}"),  # Miller indices
    (re.compile(r"(?<=\d)\{(?=\d)"), "–"),  # numeric ranges
    (re.compile(r"°(?=[a-z])"), "fl"),  # fl ligature
    (re.compile(r"¯(?=[a-z])"), "fi"),  # fi ligature
    (re.compile(r"®(?=[a-z])"), "ff"),  # ff ligature
    (re.compile(r"¶ ?([aeiouy])"), lambda m: _ACUTE[m.group(1)]),  # acute accents
    (re.compile(r"µ([aeiou])"), lambda m: _GRAVE[m.group(1)]),  # grave accents
    (re.compile(r"(?<=\d) ?± ?C\b"), " °C"),  # degree Celsius
    (re.compile(r"\\(?=[A-Za-z])"), "\u201c"),  # opening quote
    (re.compile(r'(?<=[a-z.])"'), "\u201d"),  # closing quote
    (re.compile(r"(?<![A-Za-z])! = "), "ω = "),  # omega in optics
    (re.compile(r"(?<![A-Za-z0-9])² = "), "ε = "),  # epsilon in optics
    (re.compile(r" \? \["), " ⊥ ["),  # perpendicular
    (re.compile(r"\bver sion\b"), "version"),
]
_FURNITURE_RE = re.compile(
    r"^\s*°?\s*c\s*\d{4}(–\d{4})?\s*Mineral Data Publishing.*$|^\s*c\s*2001-2005 Mineral Data Publishing.*$"
    r"|^All rights reserved\..*$|^any form or by any means.*$|^permission of Mineral Data Publishing\.\s*$",
    re.M,
)


def repair_glyphs(text: str) -> str:
    for rx, rep in _RULES:
        text = rx.sub(rep, text)  # type: ignore[arg-type]
    return text


def _collapse_layout(text: str) -> str:
    """-layout pads columns with runs of spaces; fold them, keep line breaks,
    and put each section header at the start of its own paragraph."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = _FURNITURE_RE.sub("", text)
    for sec in SECTIONS:
        text = re.sub(rf"(?<!\n\n)(^|\n)({re.escape(sec)}:)", r"\1\n\2", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def sheet_text(pdf_path: str) -> str:
    out = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if out.returncode != 0:
        return ""
    return _collapse_layout(repair_glyphs(out.stdout))


def species_from_path(pdf_path: str) -> str:
    return os.path.splitext(os.path.basename(pdf_path))[0].replace("_", " ")


def iter_sheets(limit: int = 0):
    """(species, text, path) for every sheet that renders."""
    n = 0
    for path in sorted(glob.glob(os.path.join(HOM_DIR, "*.pdf"))):
        text = sheet_text(path)
        if len(text) < 200:
            continue
        yield species_from_path(path), text, path
        n += 1
        if limit and n >= limit:
            return
