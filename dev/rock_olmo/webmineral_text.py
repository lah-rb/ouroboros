"""webmineral species pages -> stage-1 prose (root venv). LICENCE-FLAGGED.

WHAT THE PAGES ARE. ~/corpora/mineral-refs/webmineral/data/<Name>.shtml:
4,700 species pages from webmineral.com (scraped; the site is copyrighted —
included for this private research run by operator decision 2026-09-07,
every record tagged `license: restricted-webmineral`). Each page is nav
chrome, then six sections — "General X Information", "X Crystallography",
"Physical Properties of X", "Calculated Properties of X", "X Classification",
"Other X Information" — then link lists, advertiser blocks, a search widget
and a specimen-label form. Only the six sections are knowledge.

RENDERING. Tags stripped, the text is a stream of label lines ending in ":"
followed by value lines. The stream is re-folded into "Label: value" lines
under "## Section" headings; value lines are joined with spaces, and the
Empirical Formula's exploded subscripts ("NiC", "31", "H", "32") are
re-fused. Image credits ("©", "Images:") are dropped; image "Comments:"
(a specimen description) are kept. Everything from "See Also:" onward is
furniture and is cut.
"""

from __future__ import annotations

import glob
import html
import os
import re

WM_DIR = os.path.expanduser("~/corpora/mineral-refs/webmineral/data")
LICENSE = "restricted-webmineral"

_SECTION_PATTERNS = (
    ("General Information", r"^General (.+?) Information$"),
    ("Crystallography", r"^(.+?) Crystallography$"),
    ("Physical Properties", r"^Physical Properties of (.+)$"),
    ("Calculated Properties", r"^Calculated Properties of (.+)$"),
    ("Classification", r"^(.+?) Classification$"),
    ("Other Information", r"^Other (.+?) Information$"),
    ("Images", r"^(.+?) Image$"),
)
#: In the Images section only these labels carry knowledge; the rest is
#: gallery furniture (image names, credits).
_IMAGE_LABELS = {"Comments", "Location", "Scale"}
_DROP_LABELS = {"Name Pronunciation", "Images", "Image"}
_STOP_RE = re.compile(
    r"^(See Also:|Search for .+ using:|Visit our Advertisers|Ask about .+ here|Print or Cut-and-Paste)"
)
_DROP_LINE_RE = re.compile(r"^(©|Images:|Image)$|^Google Search|^Website Link$")
_FUSE_LABELS = {"Empirical Formula", "Chemical Formula"}


def strip_tags(page: str) -> list[str]:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S | re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return [ln.strip() for ln in text.split("\n") if ln.strip()]


def _section(line: str) -> str | None:
    for name, pat in _SECTION_PATTERNS:
        if re.match(pat, line):
            return name
    return None


def render(page: str) -> tuple[str, str]:
    """(species_name, prose) for one page; prose "" if the page has no sections."""
    lines = strip_tags(page)
    species = ""
    out: list[str] = []
    started = False
    section = ""
    label: str | None = None
    values: list[str] = []
    skip_next = False  # the line after a "©" is a credit
    dropping = False  # value lines of a dropped label

    def flush():
        nonlocal label, values
        if label is not None:
            v = " ".join(values).strip()
            if label in _FUSE_LABELS:
                v = re.sub(r"(?<=[A-Za-z\)\]]) (?=\d)", "", v)
                v = re.sub(r"(?<=\d) (?=[A-Z\(\[])", "", v)
            if v:
                out.append(f"{label}: {v}")
            elif label:
                out.append(f"{label}:")
        label, values = None, []

    for ln in lines:
        sec = _section(ln)
        if sec:
            if not started:
                m = re.match(_SECTION_PATTERNS[0][1], ln)
                species = m.group(1).strip() if m else ""
            started = True
            flush()
            section = sec
            if sec != "Images":
                out.append(f"\n## {sec}")
            continue
        if not started:
            continue
        if _STOP_RE.match(ln):
            break
        if skip_next:
            skip_next = False
            continue
        if ln == "©":
            skip_next = True
            flush()
            continue
        if _DROP_LINE_RE.match(ln):
            continue
        if ln.endswith(":") and len(ln) <= 48 and not re.search(r"\d", ln[:-1]):
            flush()
            lab = ln[:-1].strip()
            if lab in _DROP_LABELS or (
                section == "Images" and lab not in _IMAGE_LABELS
            ):
                label = None
                dropping = True  # swallow this label's value lines
                continue
            label = lab
            dropping = False
            continue
        if label is None:
            if not dropping and section != "Images":
                out.append(ln)
        else:
            values.append(ln)
    flush()
    text = "\n".join(out).strip()
    return species, text


def iter_pages(limit: int = 0):
    """(species, prose, path) for every page with content."""
    n = 0
    for path in sorted(glob.glob(os.path.join(WM_DIR, "*.shtml"))):
        try:
            page = open(path, errors="ignore").read()
        except OSError:
            continue
        species, text = render(page)
        if not species or len(text) < 200:
            continue
        yield species, text, path
        n += 1
        if limit and n >= limit:
            return
