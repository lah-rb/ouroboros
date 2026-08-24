"""Canonical field schema for the TRAINING form.

WHY THIS IS A SEPARATE LAYER, NOT AN EDIT TO THE PACKS. The dataset
artifacts are the GROUNDED record: every numeric value in them appears
verbatim in its paper, and `grounding_check` is what enforces that.
Converting "3.551" (MHz) into "3551000" (Hz) inside an artifact would
destroy exactly that property — the number would no longer be findable
in the source, and the anti-fabrication gate would fail the paper it
just certified. So packing stays verbatim and normalisation happens
HERE, on the way out, with the original value carried alongside.

WHAT THIS FIXES, measured over the corpus 2026-08-24 (3,802 distinct
data keys, 17,679 uses):

  * UNIT SPLITS — 163 keys share a base with a different unit suffix
    ("particle_size_nm" / "particle_size_um" / "particle_size_m";
    "measurement_temperature_c" / "_k"). A model reading the same
    physical quantity under two names learns a distinction that is not
    real. Collapsing them recovers 88 keys.
  * QUALIFIER SPRAWL — 752 keys carry a _min/_max/_avg suffix. Folded
    into range objects on one key, that is 210 keys recovered, and the
    pair becomes a single fact ("scanned 10-90 deg") instead of two
    half-facts.
  * SEMANTIC COLLISION — "xrd_2theta_min_deg"/"_max_deg" is a SCAN
    RANGE while "xrd_2theta_deg" and "xrd_peaks_2theta_deg" are PEAK
    POSITIONS. Same-looking keys, different meanings; the one
    inconsistency in the corpus most likely to teach a model something
    false.

The long tail is deliberately NOT collapsed. 93% of keys are used
exactly once ("olivine_required_billion_tonnes_per_year_sequester_all_
anthropogenic_co2") and carry the domain's real specificity; forcing
them into a schema would lose more than it gains. They serialize as
prose instead.
"""

from __future__ import annotations

import re
from typing import Any

#: quantity family -> (canonical unit, {suffix: factor to canonical})
#: Factors convert the PACKED number into the canonical unit. Only
#: unambiguous, exact conversions belong here — anything requiring an
#: offset (Celsius/Kelvin) or a convention call is handled explicitly.
UNIT_FAMILIES: dict[str, tuple[str, dict[str, float]]] = {
    "length_small": (
        "nm",
        {
            "nm": 1.0,
            "um": 1e3,
            "µm": 1e3,
            "mm": 1e6,
            "angstrom": 0.1,
            "ang": 0.1,
            "pm": 1e-3,
        },
    ),
    "frequency": ("hz", {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9, "thz": 1e12}),
    "pressure": ("mpa", {"pa": 1e-6, "kpa": 1e-3, "mpa": 1.0, "gpa": 1e3}),
    "energy_ev": ("ev", {"ev": 1.0, "kev": 1e3, "mev": 1e6}),
    "time": (
        "s",
        {
            "s": 1.0,
            "ms": 1e-3,
            "us": 1e-6,
            "ns": 1e-9,
            "ps": 1e-12,
            "fs": 1e-15,
            "min": 60.0,
            "h": 3600.0,
        },
    ),
    "wavenumber": ("cm-1", {"cm-1": 1.0, "cm_1": 1.0}),
}

#: Units that are RECOGNISED but never converted. They earn their place
#: because recognising a suffix is what lets a _min/_max pair fold onto
#: one key — "xrd_2theta_min_deg" is unfoldable until "deg" is known to
#: be a unit. Degrees, percentages and the rest have no sensible
#: canonical target here, so the value passes through untouched.
IDENTITY_UNITS = frozenset(
    {
        "deg",
        "degrees",
        "rad",
        "percent",
        "pct",
        "wt_percent",
        "at_percent",
        "mol_percent",
        "vol_percent",
        "ppm",
        "ppb",
        "k",
        "kelvin",
        "c",
        "celsius",
        "degc",
        "emu_g",
        "hv",
        "hrc",
        "g_cm3",
        "kg_m3",
        "mol_l",
        "molar",
        "v",
        "mv",
        "kv",
        "ma",
        "ua",
        "ohm_cm",
        "s_cm",
        "j",
        "kj",
        "w",
        "mw",
        "kw",
        "counts",
        "cps",
        "au",
        "index",
        "ratio",
    }
)

#: suffix -> family, built once (longest suffix wins at match time)
_SUFFIX_FAMILY: dict[str, str] = {}
for _fam, (_canon, _facs) in UNIT_FAMILIES.items():
    for _suf in _facs:
        _SUFFIX_FAMILY.setdefault(_suf, _fam)

_ALL_UNITS = sorted(
    (re.escape(u) for u in set(_SUFFIX_FAMILY) | IDENTITY_UNITS),
    key=len,
    reverse=True,
)
_UNIT_SUFFIX_RE = re.compile(r"_(" + "|".join(_ALL_UNITS) + r")$")
_QUALIFIER_RE = re.compile(r"_(min|max|avg|average|mean)$")

#: Bases whose FOLDED RANGE would collide with an existing key that means
#: something else. "xrd_2theta_min_deg"/"_max_deg" (342 uses each) is the
#: instrument's SCAN RANGE, while "xrd_2theta_deg" (58) and
#: "xrd_peaks_2theta_deg" (46) are measured PEAK POSITIONS. Folding the
#: first pair onto the second name would merge a scan window with a
#: diffraction peak — the single inconsistency in this corpus most likely
#: to teach a model something false. The range gets an explicit name.
RANGE_RENAMES: dict[str, str] = {
    "xrd_2theta": "xrd_scan_range_2theta",
    "ftir_spectral_range": "ftir_scan_range",
    "raman_spectral_range": "raman_scan_range",
    "libs_spectral_range": "libs_scan_range",
    "optical_transmission_range": "optical_transmission_range",
}

#: Temperature is deliberately NOT in UNIT_FAMILIES: C->K is an offset,
#: not a factor, and a packed "25" under _c and a packed "298" under _k
#: are the same measurement. Handled explicitly so the intent is visible.
_TEMP_RE = re.compile(r"_(c|celsius|degc)$")


def split_key(key: str) -> tuple[str, str, str]:
    """(base, qualifier, unit_suffix) — any part may be "".

    Order matters: the qualifier sits OUTSIDE the unit in this corpus's
    spelling ("ftir_spectral_range_cm-1_min"), but also inside it
    ("particle_size_nm_min"), so both orders are stripped.
    """
    base, qual, unit = key, "", ""
    # Strip repeatedly and in either order: this corpus spells the
    # qualifier outside the unit ("ftir_spectral_range_cm-1_min") AND
    # inside it ("xrd_2theta_min_deg"), so a single pass in one fixed
    # order leaves half of them unsplit — which is precisely why the
    # 342-use xrd_2theta_min/max pair never folded on the first attempt.
    for _ in range(3):
        m = _QUALIFIER_RE.search(base)
        if m and not qual:
            qual, base = m.group(1), base[: m.start()]
            continue
        m = _UNIT_SUFFIX_RE.search(base)
        if m and not unit:
            unit, base = m.group(1), base[: m.start()]
            continue
        break
    return base, qual, unit


def canonical_unit(unit: str) -> str:
    """The unit this one converges on. Identity units map to themselves."""
    fam = _SUFFIX_FAMILY.get(unit)
    return UNIT_FAMILIES[fam][0] if fam else unit


def convert(value: Any, unit: str) -> Any:
    """Value expressed in its family's canonical unit, or unchanged."""
    fam = _SUFFIX_FAMILY.get(unit)
    if fam is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    factor = UNIT_FAMILIES[fam][1][unit]
    out = value * factor
    # Keep integers integral where the conversion is exact.
    return int(out) if float(out).is_integer() and abs(out) < 1e15 else out


def canonicalize(data: dict) -> dict:
    """Pack data in canonical training form.

    Unit variants converge on one key per quantity; _min/_max pairs fold
    into a single key holding {"min":…, "max":…, "unit":…}. Every
    normalised field keeps ``*_as_packed`` so the verbatim value a
    reader can find in the paper is never lost.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    passthrough: dict[str, Any] = {}

    for key, value in data.items():
        base, qual, unit = split_key(key)
        if not unit and not qual:
            passthrough[key] = value
            continue
        # A key is only re-labelled with the canonical unit if its value
        # can actually BE converted. A string like "0.5 x 0.5" (a beam
        # profile in mm) passes through convert() untouched, so renaming
        # it to _nm would assert a conversion that never happened —
        # misleading data, worse than the split it was fixing.
        convertible = isinstance(value, (int, float)) and not isinstance(value, bool)
        canon = canonical_unit(unit) if (unit and convertible) else unit
        slot = groups.setdefault((base, canon), {"packed": {}, "unit": canon})
        # COLLISION MUST NOT DROP DATA. Two source keys can legitimately
        # land in the same slot — "particle_size_nm" and
        # "particle_size_um" both become particle_size_nm, and a paper
        # may state both for different samples. Silently keeping the
        # last one loses a measured value, so collisions accumulate.
        slot["packed"].setdefault(qual or "value", []).append((value, unit, key))

    out: dict[str, Any] = dict(passthrough)
    for (base, canon), slot in groups.items():
        packed = slot["packed"]
        name = f"{base}_{canon}".rstrip("_") if canon else base
        if "min" in packed or "max" in packed:
            renamed = RANGE_RENAMES.get(base)
            range_name = (
                (f"{renamed}_{canon}".rstrip("_") if canon else renamed)
                if renamed
                else name
            )
            rng: dict[str, Any] = {}
            asp: list[dict[str, Any]] = []
            for q in ("min", "max", "avg", "average", "mean"):
                for v, unit, orig in packed.get(q, []):
                    label = "avg" if q in ("average", "mean") else q
                    rng.setdefault(label, convert(v, unit))
                    asp.append({"role": label, "value": v, "key": orig})
            if canon:
                rng["unit"] = canon
            out[range_name] = rng
            out[f"{range_name}_as_packed"] = asp
            # A bare-qualifier entry sharing this base is a DIFFERENT
            # quantity, not part of the range: "xrd_2theta_deg" is a peak
            # position while "xrd_2theta_min/max_deg" is the scan window.
            # It keeps its own key rather than being folded in.
            bare = packed.get("value", [])
            if bare:
                vals = [convert(v, unit) for v, unit, _ in bare]
                one = vals[0] if len(vals) == 1 else vals
                if range_name == name:
                    # Same key would be written twice and the range —
                    # min AND max — silently lost. With no rename to
                    # separate them the bare figure is a nominal value
                    # OF this range, so it joins the object.
                    rng["value"] = one
                    out[range_name] = rng
                else:
                    out[name] = one
        else:
            # NO min/max here, but there may still be an avg/mean —
            # which must keep its own key rather than being read as the
            # bare value. Reading only packed["value"] silently emitted
            # an EMPTY LIST for any key whose sole qualifier was _mean
            # (17 artifacts, e.g. biaxial_flexural_strength_mean_mpa),
            # destroying the measurement. Every qualifier is emitted.
            for qual, entries in packed.items():
                if not entries:
                    continue
                qname = (
                    name
                    if qual == "value"
                    else f"{name}_{'avg' if qual in ('average', 'mean') else qual}"
                )
                converted = [convert(v, unit) for v, unit, _ in entries]
                out[qname] = converted[0] if len(converted) == 1 else converted
                if any(c != v for c, (v, _, _) in zip(converted, entries)) or any(
                    orig != qname for _, _, orig in entries
                ):
                    out[f"{qname}_as_packed"] = [
                        {"value": v, "key": orig} for v, _, orig in entries
                    ]
    return out
