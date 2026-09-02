"""LIBS: the only place in this tool that knows what a wavelength is.

The core is technique-agnostic by construction — it maps pixels to numbers and
never names a unit. This layer supplies the two things that need domain
knowledge: which printed annotations on a figure are usable position
references, and which line to fall back on when a figure labels nothing.
"""

from __future__ import annotations

import re

# Ca II K. The strongest line in almost any geological LIBS spectrum, present
# whether or not calcium is a target — which is exactly what makes it usable
# as a fallback reference when a figure labels nothing.
#
# 393.366 nm is the AIR wavelength. It exists in NIST ASD only as a RITZ
# value, not an observed one, which is the trap `libs_layer.load_asd_full`
# handles and the buggy `reference_layer.load_asd_lines` does not. It is
# written literally here so this module does not depend on either.
CA_II_K_NM = 393.366
CA_II_H_NM = 396.847
FALLBACK_REFERENCES_NM = (CA_II_K_NM, CA_II_H_NM)

# A wavelength printed on a figure, with or without a species prefix:
# "Ca II 393.37 nm", "Fe I 404.6", "393.4 nm".
# The lookaround is load-bearing: without it a bare `\d{3}` matches a 3-digit
# run INSIDE a longer number, so "0.387 wt%" yielded 387 and "2024" yielded
# 202 -- turning a concentration and a year into wavelengths.
_LABEL_RE = re.compile(
    r"(?:(?P<species>[A-Z][a-z]?)\s*(?P<stage>I{1,3}|IV|V)?\s*)?"
    r"(?<![\d.])(?P<nm>\d{3}(?:\.\d{1,3})?)(?![\d])\s*(?:nm)?",
)

# Printed labels outside this window are not LIBS emission wavelengths --
# a figure number, a concentration, or a year.
_PLAUSIBLE_NM = (180.0, 1000.0)


def references_from_labels(labels) -> list[float]:
    """Wavelengths a figure prints on itself, as position references.

    A LIBS figure routinely annotates its major lines, and those annotations
    are the best reference available: they are the authors' own identification
    of a feature in their own spectrum, so using one to fix a global offset
    borrows nothing from a catalogue.
    """
    out: list[float] = []
    for item in labels or []:
        text = item if isinstance(item, str) else str(item.get("text", ""))
        for m in _LABEL_RE.finditer(text):
            try:
                nm = float(m.group("nm"))
            except (TypeError, ValueError):
                continue
            if _PLAUSIBLE_NM[0] <= nm <= _PLAUSIBLE_NM[1] and nm not in out:
                out.append(nm)
    return out


def position_references(labels=None) -> tuple[list[float], str]:
    """The references to align on, and where they came from.

    Labels printed on the figure win. Ca II H and K are the fallback, and the
    caller is told which was used so the artifact can record it — an offset
    taken from a catalogue line is a weaker claim than one taken from the
    authors' own annotation, and the two must not be indistinguishable
    downstream.
    """
    printed = references_from_labels(labels)
    if printed:
        return printed, "figure_label"
    return list(FALLBACK_REFERENCES_NM), "fallback_ca_ii"


# ── Triage prior ─────────────────────────────────────────────────────
#
# The detection curve (tools/figure_digitizer/confidence.py) needs an ABSOLUTE
# separation to judge a figure before anything is extracted. For LIBS that is
# how close real lines sit: over 5,590 lines at 10 sigma in five handheld SME
# iron-oxide spectra (200-800 nm), nearest-neighbour separation is
#   p10 0.224   p25 0.268   median 0.327   p75 0.433 nm
# with 83% of lines having a neighbour within 0.5 nm. Median relative
# intensity 0.047 (the fit set's was 0.053). Measured 2026-09-01. A denser
# matrix (steel, shale) sits lower; a sparse one (Au) higher -- so this is a
# triage prior, and the figure's own recovered peaks replace it once known.
NOMINAL_LINE_SEPARATION_NM = 0.327
MEDIAN_LINE_INTENSITY = 0.047


def triage_libs(
    grid_nm_per_px: float, pen_nm: float, seps_nm=None, intensities=None
) -> dict:
    """Digitise-or-refuse for a LIBS figure, in nm, with the LIBS prior."""
    from tools.figure_digitizer import confidence

    return confidence.triage(
        grid_nm_per_px,
        pen_nm,
        seps=seps_nm,
        intensities=intensities,
        nominal_sep=NOMINAL_LINE_SEPARATION_NM,
        nominal_intensity=MEDIAN_LINE_INTENSITY,
    )
