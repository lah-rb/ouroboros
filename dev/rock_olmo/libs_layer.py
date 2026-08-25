"""Predicted LIBS line intensities from NIST ASD atomic data.

WHY THIS EXISTS. Our LIBS records were flat wavelength lists — "Si at
190.13, 288.16 nm" — with no indication which line is strong. That is
not what a LIBS spectrum looks like, and the collected literature does
not fill the gap: of 126 LIBS-mentioning papers in the databank,
essentially none carry line-intensity tables. They carry acquisition
metadata (laser energy, pulse width, pressure, spectrometer resolution).

But the intensities are COMPUTABLE. For an optically thin plasma in
local thermodynamic equilibrium the emitted intensity of a line is

    I_ki  ∝  (g_k · A_ki / λ) · exp(−E_k / kT)

and NIST ASD gives all three of A_ki, E_k and J_k (→ g_k = 2J_k + 1)
for 31,576 of its 119,026 lines. So this is a DERIVATION from
catalogued atomic constants, not an estimate from a fitted correlation —
the distinction that decides whether something belongs in the corpus.

THE SCOPE LIMIT, WHICH IS NOT OPTIONAL. The formula above gives relative
intensities WITHIN one species — one element in one ionisation stage.
Comparing across elements or stages additionally requires the number
density ratio N_s and the partition function U_s(T), and the Saha
equation for the ionisation balance. We have no partition functions, so
cross-element intensity comparison is NOT asserted anywhere here. Each
element's lines are normalised to 100 within that element, and the
emitted text says so. Silently ranking Si against Fe would be inventing
a plasma model we do not have.

TEMPERATURE. LIBS plasmas sit around 8,000–12,000 K during the usual
detection window. T is a parameter of the MEASUREMENT, not of the
mineral: the same sample at two temperatures gives different relative
intensities. That dependence is stated in every record rather than
hidden, which is the point — it is the one thing a flat wavelength list
cannot teach.

COLUMN NAMES ARE NOT CONSISTENT ACROSS THE 92 FILES, and this matters
more than it sounds. 50 files head their wavelength columns `_vac` and
42 head them `_air`; hydrogen alone has neither an `element` nor an
`sp_num` column. Any loader that resolves one fixed name silently
returns NOTHING for the other 42 — which is exactly what
`reference_layer.load_asd_lines` did, dropping Ca, Li, H and 39 others
out of every LIBS record ever emitted. Calcium is among the most used
elements in geological LIBS. Columns are therefore resolved by trying
the known variants, and the element comes from the FILENAME.

THE HEADERS ALSO LIE ABOUT AIR vs VACUUM. Na D is catalogued as
588.995095 / 589.592424 in a file headed `obs_wl_vac(nm)`; those are the
AIR values (vacuum is 589.158 / 589.756), and K I 766.4899 and O I
777.194 agree. NIST reports air between 200 nm and 2 um and vacuum
outside. Records say "air (NIST convention)" and never repeat the
header's claim.

OBSERVED vs RITZ. A Ritz wavelength is computed from energy levels, not
measured. Preferring observed values alone would drop Ca II entirely —
its 393.37/396.85 H and K pair is among the most used lines in
geological LIBS, and every Ca II row here carries a Ritz value only.
Ritz lines are included and LABELLED, never silently mixed with
observations.
"""

from __future__ import annotations

import csv
import math
import os
from typing import Iterator

REF_ROOT = os.path.expanduser("~/corpora/mineral-refs")
ASD_DIR = os.path.join(REF_ROOT, "nist_asd")

#: Boltzmann constant expressed in cm^-1 per kelvin, so it pairs directly
#: with NIST's Ek column without converting energies to joules.
K_B_CM = 0.695034800

DEFAULT_T = 10000.0  # mid-range LIBS plasma
TEMPERATURE_PAIR = (8000.0, 12000.0)

STAGE_NAME = {1: "I", 2: "II", 3: "III"}


def parse_j(text: str) -> float | None:
    """J_k, which NIST writes as an integer or a half-integer fraction.

    Odd-electron systems have half-integer J ("3/2", "5/2"). Reading
    those with float() raises, and skipping them would quietly drop every
    line of every alkali and most transition metals — the elements LIBS
    is most used on."""
    t = (text or "").strip().strip('"')
    if not t:
        return None
    try:
        if "/" in t:
            num, den = t.split("/", 1)
            return float(num) / float(den)
        return float(t)
    except (ValueError, ZeroDivisionError):
        return None


#: Wavelength columns, in the order they are tried. See the docstring:
#: the file's own header cannot be trusted to name the convention, only
#: to name the column.
_WL_OBS = ("obs_wl_air(nm)", "obs_wl_vac(nm)", "obs_wl(nm)")
_WL_RITZ = ("ritz_wl_air(nm)", "ritz_wl_vac(nm)", "ritz_wl(nm)")


def _num(text: str) -> float | None:
    """Numeric fields carry annotation the catalogue uses to mark how a
    value was obtained: square brackets for a theoretical level
    (`[109610.2232]`), parentheses for an interpolated one, a trailing
    `+x` for an unknown additive constant. Strip the annotation, keep the
    number; a bracketed level is still the level."""
    t = (text or "").strip().strip('"').strip()
    if not t:
        return None
    t = t.strip("[]()").replace("+x", "").replace("*", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _first(row: dict, names: tuple[str, ...]) -> float | None:
    for n in names:
        if n in row:
            v = _num(row.get(n))
            if v is not None:
                return v
    return None


def load_asd_full(element: str) -> list[dict]:
    """Every ASD line for one element that carries the constants needed
    for a Boltzmann weight: observed wavelength, A_ki, E_k and J_k.

    A Ritz wavelength is a calculated value, not an observation, and is
    not substituted when the observed one is missing."""
    path = os.path.join(ASD_DIR, f"asd_{element}.tsv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, errors="ignore") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            wl = _first(row, _WL_OBS)
            basis = "observed"
            if wl is None:
                wl = _first(row, _WL_RITZ)
                basis = "ritz"
            aki = _num(row.get("Aki(s^-1)"))
            ek = _num(row.get("Ek(cm-1)"))
            jk = parse_j(row.get("J_k"))
            stage = _num(row.get("sp_num"))
            if stage is None and element == "H":
                # Hydrogen's sp_num column is empty for all 190 rows. Every
                # hydrogen line is necessarily H I: H II is a bare proton
                # with no bound electron and cannot emit line radiation at
                # all. This is physics, not an assumption about the file.
                stage = 1.0
            if None in (wl, aki, ek, jk, stage) or wl <= 0 or aki <= 0:
                continue
            out.append(
                {
                    "element": element,
                    "stage": int(stage),
                    "wavelength_nm_air": wl,
                    "wavelength_basis": basis,
                    "a_ki": aki,
                    "e_k_cm": ek,
                    "g_k": 2.0 * jk + 1.0,
                }
            )
    return out


def boltzmann_intensities(
    element: str, temperature_k: float = DEFAULT_T, stage: int = 1, top: int = 6
) -> list[dict]:
    """Strongest predicted lines for one element in one ionisation stage.

    Normalised to 100 WITHIN this element and stage — see the module
    docstring on why cross-element comparison is not available.
    """
    lines = [l for l in load_asd_full(element) if l["stage"] == stage]
    if not lines:
        return []
    kt = K_B_CM * temperature_k
    scored = []
    for l in lines:
        # exp(-E/kT) underflows for high levels at LIBS temperatures;
        # those lines are genuinely unpopulated, so 0 is correct.
        try:
            boltz = math.exp(-l["e_k_cm"] / kt)
        except OverflowError:
            boltz = 0.0
        val = (l["g_k"] * l["a_ki"] / l["wavelength_nm_air"]) * boltz
        if val > 0:
            scored.append(dict(l, raw=val))
    if not scored:
        return []
    peak = max(s["raw"] for s in scored)
    for s in scored:
        s["relative_intensity"] = round(100.0 * s["raw"] / peak, 3)
    scored.sort(key=lambda s: -s["raw"])
    return scored[:top]


def element_coverage(elements: list[str], temperature_k: float = DEFAULT_T) -> dict:
    out = {}
    for el in elements:
        neutral = boltzmann_intensities(el, temperature_k, stage=1, top=3)
        ionic = boltzmann_intensities(el, temperature_k, stage=2, top=3)
        out[el] = {"neutral": len(neutral), "ionic": len(ionic)}
    return out


if __name__ == "__main__":
    # Validation against lines any LIBS practitioner would name. If the
    # weighting is right these should surface near the top unprompted.
    KNOWN = {
        ("Si", 1): 288.16,
        ("Mg", 1): 285.21,
        ("Na", 1): 589.0,
        ("Ca", 2): 393.37,
        ("Al", 1): 396.15,
        ("K", 1): 766.49,
        ("O", 1): 777.19,
        ("H", 1): 656.28,
        ("Fe", 1): 373.49,
        ("Li", 1): 670.79,
    }
    print(f"Boltzmann-weighted line ranking at T = {DEFAULT_T:.0f} K")
    print("(air wavelengths above 200 nm, per NIST convention)\n")
    for (el, stage), expect in KNOWN.items():
        got = boltzmann_intensities(el, stage=stage, top=6)
        if not got:
            print(f"  {el} {STAGE_NAME[stage]:3s} no usable lines")
            continue
        wls = [g["wavelength_nm_air"] for g in got]
        near = min(wls, key=lambda w: abs(w - expect))
        hit = "OK " if abs(near - expect) < 0.5 else "-- "
        print(
            f"  {hit}{el} {STAGE_NAME[stage]:3s} expect ~{expect:7.2f}  "
            f"top6: {', '.join(f'{w:.2f}' for w in wls)}"
        )


def predict_species_lines(
    formula: str,
    temperature_k: float = DEFAULT_T,
    per_element: int = 4,
    max_elements: int = 6,
) -> list[dict]:
    """Predicted LIBS lines for a mineral, grouped BY ELEMENT.

    Grouped rather than merged because merging would imply a cross-element
    intensity ranking we cannot compute — see the module docstring. Each
    group is self-normalised and carries its own ionisation stage.

    Elements are taken in formula order and capped, because a record
    listing every line of a twelve-element silicate teaches nothing that
    the first few do not.
    """
    from holdout import elements as parse_elements

    out = []
    for el in parse_elements(formula)[:max_elements]:
        for stage in (1, 2):
            lines = boltzmann_intensities(
                el, temperature_k, stage=stage, top=per_element
            )
            if not lines:
                continue
            out.append(
                {
                    "element": el,
                    "stage": stage,
                    "stage_label": f"{el} {STAGE_NAME.get(stage, stage)}",
                    "lines": lines,
                }
            )
            break  # neutral preferred; ionic only when no neutral lines exist
    return out
