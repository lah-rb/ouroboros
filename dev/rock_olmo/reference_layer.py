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
import json
import os
import re
import zipfile
from typing import Any, Iterator

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
    # Intensity CHOOSES which peaks to keep; position PRESENTS them.
    # Ordering output by intensity implies "these are the most
    # diagnostic bands", a judgement a local-maximum finder cannot make.
    out.sort(key=lambda p: -p["relative_intensity"])
    out = out[:14]
    out.sort(key=lambda p: p["position_cm-1"])
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


def read_ecostress_spectrum(path: str) -> list[tuple[float, float]]:
    """(wavelength_um, reflectance_percent) samples from an ECOSTRESS file."""
    out: list[tuple[float, float]] = []
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) != 2:
                    continue
                try:
                    out.append((float(parts[0]), float(parts[1])))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def pick_troughs(
    spectrum: list[tuple[float, float]],
    min_depth_frac: float = 0.02,
    min_separation: float = 0.15,
) -> list[dict]:
    """Absorption MINIMA of a reflectance spectrum, as derived features.

    NOT pick_peaks with a sign flip in spirit — the physics differs and
    so does the vocabulary. A Raman spectrum's information is in its
    emission maxima; a reflectance or emissivity spectrum's is in its
    absorption features, which are TROUGHS. A maxima-finder run over
    reflectance data returns featureless continuum shoulders and misses
    every diagnostic band, so this is a separate function.

    Depth is measured against the LOCAL continuum (running maximum
    either side) because a broad reflectance rolloff otherwise swamps
    the shallow narrow features that identify a mineral.

    The 2% default was calibrated on real data: on a mimetite TIR
    spectrum spanning 0.3-76.5% reflectance, 5% returned 3 features and
    1% returned 42 (noise), while 2% returned 16 — the right order for a
    TIR spectrum's reststrahlen and Christiansen features.
    """
    if len(spectrum) < 5:
        return []
    # ECOSTRESS writes TIR spectra on a DESCENDING wavelength axis
    # (15.4 -> 2.0 um), which made every separation gap negative and
    # silently disabled de-duplication.
    spectrum = sorted(spectrum)
    ys = [y for _, y in spectrum]
    lo, hi = min(ys), max(ys)
    if hi <= lo:
        return []
    span = hi - lo
    # EDGE GUARD: a minimum in the outermost few percent of a scan is
    # usually the continuum turning over at the detector limit, and
    # because those turns are deep, depth-ranking put them first.
    x_lo, x_hi = spectrum[0][0], spectrum[-1][0]
    margin = (x_hi - x_lo) * 0.05
    out: list[dict] = []
    for i in range(1, len(spectrum) - 1):
        x, y = spectrum[i]
        if x < x_lo + margin or x > x_hi - margin:
            continue
        if not (y < spectrum[i - 1][1] and y <= spectrum[i + 1][1]):
            continue
        left = max(ys[max(0, i - 25) : i] or [y])
        right = max(ys[i + 1 : i + 26] or [y])
        depth = (min(left, right) - y) / span
        if depth < min_depth_frac:
            continue
        if out and x - out[-1]["position_um"] < min_separation:
            if depth > out[-1]["relative_depth"]:
                out[-1] = {
                    "position_um": round(x, 3),
                    "relative_depth": round(depth, 3),
                }
            continue
        out.append({"position_um": round(x, 3), "relative_depth": round(depth, 3)})
    out.sort(key=lambda p: -p["relative_depth"])
    out = out[:12]
    out.sort(key=lambda p: p["position_um"])
    return out


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
        # Column names differ across the 92 files: 50 use `_vac`, 42 use
        # `_air`. Resolving one fixed name returned [] for the other 42 —
        # Ca, Li, H and 39 more contributed NO lines to any LIBS record.
        i_wl = next(
            (
                header.index(c)
                for c in ("obs_wl_air(nm)", "obs_wl_vac(nm)", "obs_wl(nm)")
                if c in header
            ),
            None,
        )
        i_int = header.index("intens") if "intens" in header else None
        # Hydrogen has no sp_num column at all; every hydrogen line is
        # necessarily H I, since H II is a bare proton and cannot emit.
        i_sp = header.index("sp_num") if "sp_num" in header else None
        if i_wl is None or i_int is None:
            return []
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= max(i_wl, i_int, i_sp or 0):
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
                    "ionisation_stage": (
                        parts[i_sp].strip().strip('"') if i_sp is not None else "1"
                    ),
                    "element": element,
                }
            )
    rows.sort(key=lambda r: -r["relative_intensity"])
    return rows[:top]


# ---------------------------------------------------------------------------
# STRUCTURAL LAYER
#
# Added after run 2 showed the ceiling is a MISSING INPUT rather than
# missing capacity: Raman reads bonding geometry, composition does not
# encode bonding geometry, and composition was all the model had. LoRA on
# the backbone changed nothing, which is what a missing input looks like.
#
# Two sources, deliberately different in kind:
#   mindat  -- metadata ABOUT a structure (crystal system, cell, Strunz)
#              for 94.8% of our species. Broad, cheap, and measured at
#              +0.0714 holdout (t=7.50) as features.
#   AMCSD   -- the structure ITSELF (atomic positions -> coordination
#              numbers and bond lengths) for ~46%. Narrower, but it is
#              the quantity that actually sets a band position, and the
#              mindat metadata provably did NOT move polymorphs.
# ---------------------------------------------------------------------------

MINDAT_GEO = os.path.join(REF_ROOT, "mindat", "geomaterials.jsonl")
CIF_FEATURES = os.path.expanduser("~/corpora/rock-olmo-training/cif_features.json")

#: mindat stores the Strunz class as four separate columns; only the top
#: level is stable enough to name in prose.
STRUNZ_CLASSES = {
    "1": "native element",
    "2": "sulfide",
    "3": "halide",
    "4": "oxide",
    "5": "carbonate",
    "6": "borate",
    "7": "sulfate",
    "8": "phosphate",
    "9": "silicate",
    "10": "organic compound",
}


def _num(value: Any) -> float | None:
    """mindat writes absent numerics as '0' or '' rather than null, and a
    zero cell length is not a measurement — it is a missing field wearing
    a number. Treat non-positive as absent."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x > 0 else None


def load_mindat_structure(path: str = MINDAT_GEO) -> dict[str, dict]:
    """species (lowercased) -> structural metadata.

    Keys kept are the ones that survived feature selection: crystal
    system, space-group number, cell lengths and angles, Strunz class,
    calculated density, hardness. Everything else in the 148-field record
    is either provenance, optical, or locality data that no view uses.
    """
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    with open(path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            name = (rec.get("name") or "").strip()
            if not name:
                continue
            strunz = str(rec.get("strunz10ed1") or "").strip()
            # NOT the International Tables number. Verified 2026-08-24
            # against AMCSD H-M symbols: Quartz is ITA 152 and mindat says
            # 89; Pyrite is 205 and mindat says 204; Marcasite is 58 and
            # mindat says 73 — no offset fits, so this is a mindat-internal
            # id. Named accordingly: printing it as "space group 89" would
            # be a fabricated fact of exactly the kind the grounding gate
            # exists to stop. The TRUE symbol comes from AMCSD below.
            sg = rec.get("spacegroup")
            try:
                sg = int(sg) if sg not in (None, "", 0, "0") else None
            except (TypeError, ValueError):
                sg = None
            out[name.lower()] = {
                "species": name,
                "crystal_system": (rec.get("csystem") or "").strip() or None,
                "mindat_spacegroup_id": sg or None,  # opaque categorical; see above
                "a": _num(rec.get("a")),
                "b": _num(rec.get("b")),
                "c": _num(rec.get("c")),
                "alpha": _num(rec.get("alpha")),
                "beta": _num(rec.get("beta")),
                "gamma": _num(rec.get("gamma")),
                "strunz_class": STRUNZ_CLASSES.get(strunz),
                "density_calc": _num(rec.get("dcalc")),
                "hardness_max": _num(rec.get("hmax")),
                "source": "mindat",
            }
    return out


def load_cif_features(path: str = CIF_FEATURES) -> dict[str, dict]:
    """species (lowercased) -> coordination and bond geometry from AMCSD.

    Produced by ``cif_features.py``, whose coordination numbers are
    validated against textbook values for Diopside, Quartz, Pyrite,
    Rutile, Forsterite and Calcite before the extraction is trusted.
    """
    if not os.path.exists(path):
        return {}
    raw = json.load(open(path))
    return {k.lower(): dict(v, species=k, source="AMCSD") for k, v in raw.items()}


# ── ROD (Raman Open Database) ────────────────────────────────────────
#: Public domain by the contributors' declaration in every file header.
ROD_DIR = os.path.join(REF_ROOT, "rod")
_CIF_KV_RE = re.compile(
    r"^(_[A-Za-z0-9_.\-\[\]]+)(?:\s+(.*))?$"
)  # a bare tag line precedes a ;-block


def _cif_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    return raw


def parse_rod_file(text: str) -> dict:
    """Metadata + (raman_shift, intensity) samples from one .rod file.

    A CIF dialect: `_tag value` lines, `;`-delimited text blocks, and one
    `loop_` carrying `_raman_spectrum.raman_shift` / `.intensity` rows.
    Only the fields the corpus uses are lifted; everything else stays in
    the file.
    """
    meta: dict[str, str] = {}
    xs: list[float] = []
    ys: list[float] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("loop_"):
            cols: list[str] = []
            i += 1
            while i < len(lines) and lines[i].startswith("_"):
                cols.append(lines[i].split()[0])
                i += 1
            if (
                "_raman_spectrum.raman_shift" in cols
                and "_raman_spectrum.intensity" in cols
            ):
                ix, iy = cols.index("_raman_spectrum.raman_shift"), cols.index(
                    "_raman_spectrum.intensity"
                )
                while (
                    i < len(lines)
                    and lines[i].strip()
                    and not lines[i].startswith(("_", "loop_", "#"))
                ):
                    parts = lines[i].split()
                    if len(parts) >= len(cols):
                        try:
                            xs.append(float(parts[ix]))
                            ys.append(float(parts[iy]))
                        except ValueError:
                            pass
                    i += 1
                continue
            # a loop we do not read (authors etc.): skip its rows
            while (
                i < len(lines)
                and lines[i].strip()
                and not lines[i].startswith(("_", "loop_"))
            ):
                i += 1
            continue
        m = _CIF_KV_RE.match(line)
        if m:
            key, val = m.group(1), (m.group(2) or "").strip()
            if not val and i + 1 < len(lines) and lines[i + 1].startswith(";"):
                # multi-line text block
                block: list[str] = []
                i += 1
                first = lines[i][1:].strip()
                if first:
                    block.append(first)
                i += 1
                while i < len(lines) and not lines[i].startswith(";"):
                    block.append(lines[i].strip())
                    i += 1
                meta[key] = " ".join(b for b in block if b)
            else:
                meta[key] = _cif_value(val)
        i += 1
    return {
        "rod_id": meta.get("_rod_database.code") or meta.get("data_", ""),
        "species": meta.get("_chemical_name_mineral", ""),
        "formula": meta.get("_chemical_formula_sum", ""),
        "compound_source": meta.get("_chemical_compound_source", ""),
        "crystal_system": meta.get("_space_group_crystal_system", ""),
        "space_group_it": meta.get("_space_group_IT_number", ""),
        "space_group_hm": meta.get("_space_group_name_H-M_alt", ""),
        "laser_nm": meta.get(
            "_raman_measurement_device.excitation_laser_wavelength", ""
        ),
        "laser_type": meta.get("_raman_measurement_device.excitation_laser_type", ""),
        "device": " ".join(
            x
            for x in (
                meta.get("_raman_measurement_device.company", ""),
                meta.get("_raman_measurement_device.model", ""),
            )
            if x
        ),
        "resolution_cm1": meta.get("_raman_measurement_device.resolution", ""),
        "temperature": meta.get("_raman_measurement.temperature", ""),
        "environment": meta.get("_raman_measurement.environment", ""),
        "title": meta.get("_publ_section_title", ""),
        "journal": meta.get("_journal_name_full", ""),
        "year": meta.get("_journal_year", ""),
        "doi": meta.get("_journal_paper_doi", ""),
        "spectrum": list(zip(xs, ys)),
        "source": "ROD",
    }


def iter_rod(limit: int = 0) -> Iterator[dict]:
    """ROD records with a mineral name and a spectrum."""
    files = sorted(glob.glob(os.path.join(ROD_DIR, "*.rod")))
    n = 0
    for f in files:
        try:
            rec = parse_rod_file(open(f, errors="ignore").read())
        except Exception:  # noqa: BLE001
            continue
        if not rec["rod_id"]:
            rec["rod_id"] = os.path.splitext(os.path.basename(f))[0]
        if not rec["species"] or len(rec["spectrum"]) < 32:
            continue
        rec["file"] = os.path.basename(f)
        yield rec
        n += 1
        if limit and n >= limit:
            return


# ── prose renderings of reference records (stage-1 text) ─────────────
def rruff_prose(rec: dict) -> str:
    """One RRUFF record's header as a paragraph. Facts only, provenance kept:
    a peak position is never asserted here (those are DERIVED, see
    pick_peaks); this is what the archive states about the sample."""
    sp = rec.get("species", "")
    bits = []
    if rec.get("ideal_formula"):
        bits.append(f"{sp} has the ideal formula {rec['ideal_formula']}.")
    else:
        bits.append(f"{sp}.")
    if rec.get("rruff_id"):
        bits.append(
            f"RRUFF sample {rec['rruff_id']}"
            + (f", from {rec['locality']}," if rec.get("locality") else "")
            + (
                f" was measured by Raman spectroscopy at {rec['laser_nm']} nm excitation."
                if rec.get("laser_nm")
                else " was measured by Raman spectroscopy."
            )
        )
    if rec.get("measured_formula"):
        bits.append(f"Its measured chemistry is {rec['measured_formula']}.")
    cell = rec.get("cell") or {}
    if cell.get("crystal_system") or cell.get("a"):
        cp = ", ".join(f"{k} = {cell[k]:g}" for k in ("a", "b", "c") if cell.get(k))
        ang = ", ".join(
            f"{k} = {cell[k]:g}°"
            for k in ("alpha", "beta", "gamma")
            if cell.get(k) and abs(cell[k] - 90) > 1e-6
        )
        s = (
            f"The sample is {cell['crystal_system']}"
            if cell.get("crystal_system")
            else "Its cell has"
        )
        if cp:
            s += f" with cell parameters {cp}" + (f" Å, {ang}" if ang else " Å")
        bits.append(s + ".")
    if rec.get("confirmation"):
        bits.append(rec["confirmation"].rstrip(".") + ".")
    if rec.get("description"):
        bits.append(rec["description"].rstrip(".") + ".")
    return " ".join(bits)


def ecostress_prose(rec: dict) -> str:
    """One ECOSTRESS mineral entry's header as a paragraph."""
    sp = rec.get("species", "")
    name = rec.get("full_name") or sp
    bits = [
        f"{name} is catalogued in the ECOSTRESS spectral library as a {rec.get('mineral_class','').lower() or 'mineral'}"
        + (f" ({rec['subclass'].lower()})" if rec.get("subclass") else "")
        + "."
    ]
    if rec.get("wavelength_range"):
        rng = {
            "TIR": "thermal infrared",
            "VSWIR": "visible to short-wave infrared",
        }.get(rec["wavelength_range"], rec["wavelength_range"])
        bits.append(
            f"Its reflectance was measured over the {rng} range"
            + (
                f" on {rec['particle_size'].lower()} material"
                if rec.get("particle_size")
                else ""
            )
            + "."
        )
    if rec.get("sample_no"):
        bits.append(f"Sample {rec['sample_no']}.")
    if rec.get("origin"):
        bits.append(rec["origin"].rstrip(".") + ".")
    return " ".join(bits)


def rod_prose(rec: dict) -> str:
    """One ROD record's metadata as a paragraph (the spectrum itself is
    DERIVED downstream by pick_peaks and never quoted here)."""
    sp = rec.get("species", "")
    bits = [
        f"{sp}"
        + (f" ({rec['formula']})" if rec.get("formula") else "")
        + f" is recorded in the Raman Open Database as entry {rec.get('rod_id','')}."
    ]
    if rec.get("compound_source"):
        bits.append(
            f"The sample was {rec['compound_source'][0].lower() + rec['compound_source'][1:]}."
        )
    if rec.get("crystal_system"):
        bits.append(
            f"It is {rec['crystal_system']}"
            + (
                f", space group {rec['space_group_hm']}"
                if rec.get("space_group_hm")
                else ""
            )
            + "."
        )
    meas = []
    if rec.get("laser_nm"):
        meas.append(f"{rec['laser_nm']} nm excitation")
    if rec.get("device"):
        meas.append(f"a {rec['device']} spectrometer")
    if rec.get("resolution_cm1"):
        meas.append(f"{rec['resolution_cm1']} cm-1 resolution")
    if meas:
        bits.append("The Raman spectrum was measured with " + ", ".join(meas) + ".")
    if rec.get("title"):
        bits.append(
            f"It accompanies the study \"{rec['title']}\""
            + (f" ({rec['journal']}, {rec['year']})" if rec.get("journal") else "")
            + "."
        )
    return " ".join(bits)
