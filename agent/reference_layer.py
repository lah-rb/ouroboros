"""Readers for the mineral reference databases.

The corpus these feed is the LITERATURE side; this is the REFERENCE
side, and the distinction is load-bearing. A paper reports what its
authors measured on their sample; a reference database reports a
characterised specimen. When the two disagree the reference is the
grounding point, but the paper is not therefore wrong — the difference
may be precision (rounding, digitising a plot) or accuracy (calibration,
a different polytype, a substituted cation). Records carry enough
provenance for that distinction to be stated rather than assumed
(operator ruling 2026-08-24).

WHAT IS FACT AND WHAT IS DERIVED, kept apart deliberately:

  FACT — read straight off the source. RRUFF's ideal and measured
  chemistry, cell parameters, crystal system, locality and
  confirmation status; ECOSTRESS's class/subclass, particle size and
  wavelength range; mindat's IMA formula and symbol; NIST ASD's
  observed line wavelengths and intensities.

  DERIVED — computed here, and labelled. RRUFF distributes SPECTRA
  (wavenumber, intensity), not peak lists, so any peak position
  attributed to RRUFF is OUR peak-pick of its spectrum. Presenting
  that as "the RRUFF value" would be a quiet fabrication of
  provenance, so it travels as ``derivation="peak_pick"`` with the
  method recorded.
"""

from __future__ import annotations

import glob
import io
import os
import re
import zipfile
from typing import Iterator

REF_ROOT = os.path.expanduser("~/corpora/mineral-refs")

# ── RRUFF ────────────────────────────────────────────────────────────
_RRUFF_META_RE = re.compile(r"^##([A-Z][A-Z \-]*)=(.*)$")
_RRUFF_NAME_RE = re.compile(r"^([A-Za-z'\-()]+?)__R\d+__")


def _clean_formula(s: str) -> str:
    """RRUFF writes subscripts as Mg_3_Si_2_ — flatten to Mg3Si2."""
    return re.sub(r"_(\d+(?:\.\d+)?)_", r"\1", s or "").replace("_", "")


def parse_rruff_file(text: str) -> dict:
    """Metadata + (wavenumber, intensity) samples from one RRUFF file."""
    meta: dict[str, str] = {}
    xs: list[float] = []
    ys: list[float] = []
    for line in text.splitlines():
        m = _RRUFF_META_RE.match(line)
        if m:
            meta[m.group(1).strip().lower().replace(" ", "_")] = m.group(2).strip()
            continue
        if "," in line:
            a, _, b = line.partition(",")
            try:
                xs.append(float(a))
                ys.append(float(b))
            except ValueError:
                continue
    cell = {}
    if meta.get("cell_parameters"):
        for key, val in re.findall(r"(\w+):\s*([\d.]+)", meta["cell_parameters"]):
            cell[key] = float(val)
        sysm = re.search(r"crystal system:\s*(\w+)", meta["cell_parameters"])
        if sysm:
            cell["crystal_system"] = sysm.group(1)
    return {
        "species": meta.get("names", ""),
        "rruff_id": meta.get("rruffid", ""),
        "ideal_formula": _clean_formula(meta.get("ideal_chemistry", "")),
        "measured_formula": _clean_formula(meta.get("measured_chemistry", "")),
        "locality": meta.get("locality", ""),
        "description": meta.get("description", ""),
        "confirmation": meta.get("status", ""),
        "laser_nm": meta.get("raman_wavelength", ""),
        "cell": cell,
        "spectrum": list(zip(xs, ys)),
        "source": "RRUFF",
    }


def pick_peaks(
    spectrum: list[tuple[float, float]],
    min_prominence_frac: float = 0.05,
    min_separation: float = 8.0,
    min_position: float = 100.0,
) -> list[dict]:
    """Local maxima of a spectrum, as DERIVED peak positions.

    Deliberately conservative and deterministic — a strict local
    maximum, at least ``min_prominence_frac`` of the spectrum's range
    above the local floor, and no two peaks closer than
    ``min_separation`` wavenumbers. It will miss shoulders a
    spectroscopist would call peaks. That is the right trade for
    training data: a missed peak costs a fact, an invented one teaches
    a falsehood.
    """
    if len(spectrum) < 5:
        return []
    # BELOW ~100 cm-1 a Raman spectrum is dominated by the notch
    # filter's shoulder, not by the sample. Abellaite's spectrum peaked
    # "hardest" at 52 and 99.5 cm-1 on the first run — instrument, not
    # mineral. Its real carbonate band at 1058.3 was ranked third.
    spectrum = [(x, y) for x, y in spectrum if x >= min_position]
    if len(spectrum) < 5:
        return []
    ys = [y for _, y in spectrum]
    lo, hi = min(ys), max(ys)
    if hi <= lo:
        return []
    threshold = lo + (hi - lo) * min_prominence_frac
    out: list[dict] = []
    for i in range(1, len(spectrum) - 1):
        x, y = spectrum[i]
        if y < threshold:
            continue
        if not (y > spectrum[i - 1][1] and y >= spectrum[i + 1][1]):
            continue
        if out and x - out[-1]["position_cm-1"] < min_separation:
            if y > out[-1]["relative_intensity"] * (hi - lo) + lo:
                out[-1] = {
                    "position_cm-1": round(x, 1),
                    "relative_intensity": round((y - lo) / (hi - lo), 3),
                }
            continue
        out.append(
            {
                "position_cm-1": round(x, 1),
                "relative_intensity": round((y - lo) / (hi - lo), 3),
            }
        )
    out.sort(key=lambda p: -p["relative_intensity"])
    return out


def iter_rruff(
    archive: str = "excellent_unoriented.zip", limit: int = 0
) -> Iterator[dict]:
    """RRUFF records from one archive. Processed files only."""
    path = os.path.join(REF_ROOT, "rruff", archive)
    if not os.path.exists(path):
        return
    n = 0
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if "Processed" not in name:
                continue
            try:
                rec = parse_rruff_file(z.read(name).decode("utf-8", "replace"))
            except Exception:
                continue
            if not rec["species"]:
                m = _RRUFF_NAME_RE.match(os.path.basename(name))
                rec["species"] = m.group(1) if m else ""
            if not rec["species"]:
                continue
            rec["archive"] = archive
            rec["file"] = name
            yield rec
            n += 1
            if limit and n >= limit:
                return


# ── ECOSTRESS ────────────────────────────────────────────────────────
def parse_ecostress_header(text: str) -> dict:
    meta: dict[str, str] = {}
    for line in text.splitlines()[:14]:
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip().lower().replace(" ", "_")] = v.strip()
    name = meta.get("name", "")
    return {
        "species": name.split()[0].strip(",") if name else "",
        "full_name": name,
        "kind": meta.get("type", ""),
        "mineral_class": meta.get("class", ""),
        "subclass": meta.get("subclass", ""),
        "particle_size": meta.get("particle_size", ""),
        "sample_no": meta.get("sample_no.", ""),
        "wavelength_range": meta.get("wavelength_range", ""),
        "origin": meta.get("origin", ""),
        "source": "ECOSTRESS",
    }


def iter_ecostress(limit: int = 0) -> Iterator[dict]:
    files = sorted(
        glob.glob(os.path.join(REF_ROOT, "ecostress", "ecospeclib-all", "*.txt"))
    )
    for i, f in enumerate(files):
        if limit and i >= limit:
            return
        try:
            with open(f, errors="ignore") as fh:
                head = "".join(fh.readline() for _ in range(14))
        except OSError:
            continue
        rec = parse_ecostress_header(head)
        # The library also covers vegetation, soils, water and
        # man-made surfaces ("Construction / Concrete"), whose first
        # word is not a mineral species. Only mineral records join the
        # species-keyed interconnects.
        if rec["species"] and rec["kind"].strip().lower() == "mineral":
            rec["file"] = os.path.basename(f)
            yield rec


# ── NIST ASD ─────────────────────────────────────────────────────────
def load_asd_lines(element: str, top: int = 12) -> list[dict]:
    """Strongest observed emission lines for one element.

    Ranked by the catalogue's own intensity column. Lines without an
    observed wavelength are skipped — a Ritz (calculated) wavelength is
    not an observation and must not be presented as one.
    """
    path = os.path.join(REF_ROOT, "nist_asd", f"asd_{element}.tsv")
    if not os.path.exists(path):
        return []
    rows: list[dict] = []
    with open(path, errors="ignore") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        try:
            i_wl = header.index("obs_wl_vac(nm)")
            i_int = header.index("intens")
            i_sp = header.index("sp_num")
        except ValueError:
            return []
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= max(i_wl, i_int, i_sp):
                continue
            wl = parts[i_wl].strip().strip('"')
            inten = parts[i_int].strip().strip('"')
            if not wl:
                continue
            try:
                wlf = float(wl)
                intf = float(re.sub(r"[^\d.]", "", inten) or 0)
            except ValueError:
                continue
            rows.append(
                {
                    "wavelength_nm": round(wlf, 4),
                    "relative_intensity": intf,
                    "ionisation_stage": parts[i_sp].strip().strip('"'),
                    "element": element,
                }
            )
    rows.sort(key=lambda r: -r["relative_intensity"])
    return rows[:top]
