"""What the corpus already knows about a figure, before anything is measured.

Every figure in the corpus already carries a vision model's prose description
in ``databank/figtext/<key>.json``. That text is far too loose to calibrate an
axis from — a naive parse recovers two numeric ranges for only 28.9% of
nm-bearing figures, and "ranging from 378 to 390" never says WHICH axis — but
it is free, it is already on disk for ~48,700 figures, and it is more than
good enough to answer "is this a plot at all".

So figtext is used as a PREFILTER, not as the metadata source: it removes the
micrographs, maps and apparatus schematics before anything expensive runs, and
later serves as an independent second reading to cross-check the structured
ask against. The structured vision ask (Phase 1c) is what actually supplies
tick labels, and only for figures that survive here.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re

# Spectral x-axis units. A figure that names none of these is not a spectrum
# plot, whatever else it may be.
_UNITS = {
    "libs": re.compile(r"\bnm\b|\bnanomet|\bwavelength\b", re.I),
    "raman": re.compile(r"cm\s*[-−]\s*1|cm⁻¹|\braman shift\b|\bwavenumber", re.I),
    "xrd": re.compile(r"\b2\s*θ|\b2\s*theta\b|\bd[- ]spacing\b", re.I),
}

# How much of the description counts as the model stating what the image is.
_IDENTITY_CHARS = 240

_AXIS = re.compile(
    r"\baxis\b|\baxes\b|\bx[- ]axis\b|\by[- ]axis\b|\btick\b|\bhorizontal axis\b",
    re.I,
)
_PLOTLIKE = re.compile(
    r"\bspectr(?:um|a|al)\b|\bintensity\b|\bpeak(?:s)?\b|\bplot\b|\bcurve\b"
    r"|\bcounts\b|\ba\.?u\.?\b|\bemission line",
    re.I,
)
# Vocabulary that says "this is an image of a thing", not a plot — and it is
# read ONLY against the identity zone (caption plus the opening of the
# description, where the model states what the image IS). Matching it against
# the whole body rejected 1,929 otherwise-qualifying figures, because a long
# description of a real spectrum routinely mentions a micrograph, a map or a
# photograph in passing: "LIBS/ChemCam targets displaying Ca-sulfate signature
# (solid spectra)" was killed by the word "micrograph" appearing later in the
# same paragraph.
_NOT_A_PLOT = re.compile(
    r"\bmicrograph\b|\bSEM image\b|\bTEM image\b|\bphotograph\b|\bphoto of\b"
    r"|\bmap of\b|\btopographic\b|\bschematic (?:diagram|of)\b|\bapparatus\b"
    r"|\bflow ?chart\b|\bsetup\b|\bthin section\b|\boutcrop\b",
    re.I,
)
# figtext's own self-reported junk, the same classes the caption-repair pass
# refuses to ask about.
_JUNK = re.compile(
    r"\bfirst page\b|\bcover page\b|\bjournal logo\b|\bpublisher\b|\bQR code\b"
    r"|\btable\b.{0,20}\bimage\b|\bentirely text\b|\bpage of text\b",
    re.I,
)


@dataclasses.dataclass(frozen=True)
class FigtextHint:
    """The prefilter's reading of one figure's existing description."""

    fig: str
    caption: str
    text: str
    technique: str | None
    plot_like: bool
    reject_reason: str | None

    @property
    def is_candidate(self) -> bool:
        return self.plot_like and self.technique is not None


def _technique(text: str) -> str | None:
    for name, rx in _UNITS.items():
        if rx.search(text):
            return name
    return None


def read_figtext(working_dir: str, key: str) -> dict:
    """The stored figtext record for a paper, or an empty one."""
    path = os.path.join(working_dir, "databank", "figtext", f"{key}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — a paper without figtext is not an error
        return {}


def hints(working_dir: str, key: str) -> list[FigtextHint]:
    """Prefilter every figure of a paper by what figtext already says.

    Cheap and deliberately permissive on the positive side: a figure that
    survives still has to pass axis detection and calibration, so a false
    positive here costs one CV pass, while a false negative silently drops a
    real spectrum from the corpus.
    """
    rec = read_figtext(working_dir, key)
    out: list[FigtextHint] = []
    for fig in rec.get("figs", []):
        body = fig.get("figtext", "") or ""
        caption = fig.get("caption", "") or ""
        text = f"{caption}\n{body}"
        identity = f"{caption}\n{body[:_IDENTITY_CHARS]}"
        reason = None
        if _JUNK.search(identity):
            reason = "figtext_junk"
        elif _NOT_A_PLOT.search(identity) and not _PLOTLIKE.search(identity):
            reason = "not_a_plot"
        elif not _PLOTLIKE.search(text):
            reason = "no_plot_vocabulary"
        elif not _AXIS.search(text):
            reason = "no_axis_mentioned"
        tech = _technique(text)
        if reason is None and tech is None:
            reason = "no_spectral_unit"
        out.append(
            FigtextHint(
                fig=fig.get("fig", ""),
                caption=fig.get("caption", "") or "",
                text=fig.get("figtext", "") or "",
                technique=tech,
                plot_like=reason is None,
                reject_reason=reason,
            )
        )
    return out
