"""WURM: ab-initio Raman modes with symmetry labels.

WHAT THIS IS FOR. Every composition-based predictor we built fails on
polymorphs by construction -- Anatase and Rutile are both TiO2 and their
spectra are unrelated. mindat's structural metadata did not fix it
(+0.060 on non-polymorphs, -0.016 on polymorphs), because crystal system
and cell lengths are metadata about a structure, not the vibrational
physics. WURM is that physics, computed: for each mineral, every normal
mode with its frequency, its irreducible representation, and its Raman
tensor. It is small -- 461 minerals -- but it is exactly on target.

THE JOIN, AND WHY IT IS NEEDED. The mirror was taken in two halves and
neither is usable alone:
  * the HTML pages carry `no_itot_rel` (relative Raman intensity, powder
    average) and `symlabel`, indexed 0..n-1, but `freq` is declared and
    never assigned
  * the XMLs carry <freqTO> and <char> per <mode>, numbered 1..n
So intensity comes from the HTML, frequency from the XML, and they are
joined on mode index (HTML i <-> XML mode_no i+1). The alignment is
asserted on mode COUNT before any join, because a silent off-by-one
would shift every band in the record onto its neighbour's intensity --
wrong in a way that looks entirely plausible.

ACOUSTIC MODES are dropped: char == "ac", frequency 0, no Raman
intensity. They are the three rigid translations of the whole cell, not
spectroscopy.
"""

from __future__ import annotations

import glob
import html
import os
import re
import unicodedata
from typing import Iterator

ROOT = os.path.expanduser("~/corpora/mineral-refs/wurm")
XMLS = os.path.join(ROOT, "xmls")

_TAG = lambda t: re.compile(rf"<{t}>(.*?)</{t}>", re.S)
_NAME = _TAG("name")
_FORMULA = _TAG("formula")
_SPG_NO = _TAG("spgroup_no")
_SPG_SYM = _TAG("spgroup_sym")
_ACELL = _TAG("acell")
_ANGDEG = _TAG("angdeg")
_NATOM = _TAG("natom")
_GROUP = re.compile(r"<group>(.*?)</group>")
_MODE = re.compile(r"<mode>(.*?)</mode>", re.S)
_MODE_NO = _TAG("mode_no")
_CHAR = _TAG("char")
_FREQ_TO = _TAG("freqTO")
_NUM = r"-?\d*\.?\d+(?:[eE][+-]?\d+)?"
_ITOT = re.compile(rf"no_itot_rel\['(\d+)'\]\s*=\s*({_NUM})")
_SYMLAB = re.compile(r"symlabel\['(\d+)'\]\s*=\s*[\"']([^\"']*)")


def _clean(s: str | None) -> str:
    """WURM double-escapes formulas (&lt;sub&gt;) and pads names. Unescape
    twice, strip the subscript markup, collapse whitespace."""
    if not s:
        return ""
    t = html.unescape(html.unescape(s))
    # CDATA wrappers survive unescaping and leaked into formulas as
    # "<![CDATA[ZrSiO4]]>". Strip the wrapper, keep the payload.
    t = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t, flags=re.S)
    t = re.sub(r"</?(sub|sup|i|b)>", "", t)
    return " ".join(t.split())


def fold_name(name: str) -> str:
    """Join key tolerant of diacritics.

    WURM writes ASCII where IMA uses accents — AKERMANITE for Åkermanite,
    and similar for the several species named after Scandinavian
    localities. Matching raw dropped 53 of 324 records, a good share of
    them real minerals we hold spectra for. Decompose to NFKD, drop the
    combining marks, casefold."""
    n = unicodedata.normalize("NFKD", name or "")
    return "".join(c for c in n if not unicodedata.combining(c)).casefold().strip()


def _title(name: str) -> str:
    """WURM stores names uppercase (AKERMANITE). Title-case for joining
    against IMA/mindat/RRUFF, preserving internal hyphens and parens."""
    n = _clean(name)
    if not n or not n.isupper():
        return n
    return re.sub(r"[A-Z]+", lambda m: m.group(0).capitalize(), n.lower().upper())


def parse_xml(text: str) -> dict:
    """One WURM XML -> header, structure, and the mode table."""
    head = text[: text.find("</HEADER>") + 9] or text[:4000]
    modes = []
    for blk in _MODE.findall(text):
        no = _MODE_NO.search(blk)
        ch = _CHAR.search(blk)
        fr = _FREQ_TO.search(blk)
        if not (no and fr):
            continue
        try:
            freq = float(fr.group(1).strip())
        except ValueError:
            continue
        modes.append(
            {
                "mode_no": int(no.group(1)),
                "irrep": _clean(ch.group(1)) if ch else "",
                "freq_cm-1": freq,
            }
        )
    sg_no = _SPG_NO.search(text)
    acell = _ACELL.search(text)
    return {
        "species": _title(_NAME.search(head).group(1)) if _NAME.search(head) else "",
        "formula": (
            _clean(_FORMULA.search(head).group(1)) if _FORMULA.search(head) else ""
        ),
        "mineral_groups": [_clean(g) for g in _GROUP.findall(head)],
        "space_group_number": (
            int(sg_no.group(1)) if sg_no and sg_no.group(1).strip().isdigit() else None
        ),
        "space_group_symbol": (
            _clean(_SPG_SYM.search(text).group(1)) if _SPG_SYM.search(text) else None
        ),
        "cell": _clean(acell.group(1)) if acell else None,
        "cell_angles": (
            _clean(_ANGDEG.search(text).group(1)) if _ANGDEG.search(text) else None
        ),
        "n_atoms": int(_NATOM.search(text).group(1)) if _NATOM.search(text) else None,
        "modes": modes,
    }


def parse_html_intensities(text: str) -> tuple[dict[int, float], dict[int, str]]:
    inten = {int(k): float(v) for k, v in _ITOT.findall(text)}
    syms = {int(k): v.strip() for k, v in _SYMLAB.findall(text)}
    return inten, syms


def iter_wurm(limit: int = 0) -> Iterator[dict]:
    """Joined records: species, structure, and Raman-active modes.

    Yields only minerals whose XML and HTML mode counts AGREE. A mismatch
    means the two halves of the mirror are describing different runs, and
    joining them on index would attach every intensity to the wrong band.
    """
    files = sorted(glob.glob(os.path.join(XMLS, "*.xml")))
    n = 0
    for xp in files:
        wid = os.path.splitext(os.path.basename(xp))[0]
        hp = os.path.join(ROOT, f"{wid}.html")
        if not os.path.exists(hp):
            continue
        rec = parse_xml(open(xp, errors="ignore").read())
        if not rec["species"] or not rec["modes"]:
            continue
        inten, syms = parse_html_intensities(open(hp, errors="ignore").read())
        if inten and len(inten) != len(rec["modes"]):
            # Do not guess. Skipping is cheap at 461 minerals; a
            # misaligned record is a wrong fact that looks right.
            continue

        # "TBD" is WURM's placeholder for an unassigned symmetry label, not
        # a label. Treated as present it would teach "TBD" as an irrep for
        # every mode of the affected minerals.
        def _irrep(idx: int, fallback: str) -> str:
            lab = (syms.get(idx) or "").strip()
            if lab and lab.upper() != "TBD":
                return lab
            fb = (fallback or "").strip()
            return "" if fb.upper() in ("TBD", "AC") else fb

        bands = []
        for m in rec["modes"]:
            i = m["mode_no"] - 1
            if m["irrep"] == "ac" or m["freq_cm-1"] <= 0:
                continue
            bands.append(
                {
                    "position_cm-1": round(m["freq_cm-1"], 2),
                    "irrep": _irrep(i, m["irrep"]),
                    "intensity_rel": round(inten[i], 4) if i in inten else None,
                }
            )
        if not bands:
            continue
        rec["bands"] = sorted(bands, key=lambda b: b["position_cm-1"])
        rec["wurm_id"] = wid
        rec.pop("modes", None)
        yield rec
        n += 1
        if limit and n >= limit:
            return


if __name__ == "__main__":
    recs = list(iter_wurm())
    print(f"{len(recs)} WURM minerals joined")
    withi = sum(
        1 for r in recs if any(b["intensity_rel"] is not None for b in r["bands"])
    )
    print(f"  with intensities: {withi}")
    for r in recs[:3]:
        strong = sorted(
            (b for b in r["bands"] if b["intensity_rel"]),
            key=lambda b: -b["intensity_rel"],
        )[:5]
        print(
            f"\n  {r['species']} ({r['formula']}) {r['space_group_symbol']} "
            f"#{r['space_group_number']}, {len(r['bands'])} Raman modes"
        )
        for b in strong:
            print(
                f"     {b['position_cm-1']:8.1f} cm-1  {b['irrep']:6s} I={b['intensity_rel']}"
            )
