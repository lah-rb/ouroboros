"""Typed, single-valued facts for corpus v4 (root venv).

WHY A FACTS LAYER. §11 of PROCEDURE.md: species -> formula was learned
because the target was ONE string; species -> bands was not because the
same prompt had several competing continuations. Every fact here is
single-valued for its key: a measurement-keyed fact names its sample
("RRUFF R050125 of Quartz at 532 nm"), a species-canonical fact carries
exactly one list per species and modality (the richest record, the rule
already in `assemble.load_reference_modalities`). Templates (templates.py)
render a fact many ways; the fact itself never disagrees with itself.

WHAT IT REUSES. The loaders are the assembler's and the reference layer's
(`load_reference_modalities`, `formula_siblings`, `load_paper_reports`,
`load_mindat_structure`, `load_cif_features`, `iter_wurm`,
`predict_species_lines`, `iter_rruff`, `iter_ecostress`, `iter_rod`); the
provenance discipline is the interconnect module's (`within_tolerance`, the
derivation phrases). Nothing here infers; every value is a lookup, a join
or a deterministic peak-pick, and the pick is labelled as DERIVED.

EXCLUSION. `build_facts(exclude=...)` drops the probe species from every
fact, from sibling groups (a contrastive group that loses a member keeps the
rest; one that falls below two members is dropped) and from corroborations.
There is no other holdout (operator ruling 2026-09-07).
"""

from __future__ import annotations

import collections
import csv
import glob
import json
import os
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from assemble import (
    DATASET,
    MAX_CORROBORATIONS_PER_SPECIES,
    formula_siblings,
    load_ima,
    load_paper_reports,
    load_reference_modalities,
)
from interconnect import within_tolerance
from libs_layer import DEFAULT_T, TEMPERATURE_PAIR, predict_species_lines
from formula_norm import normalize_formula
from reference_layer import (
    REF_ROOT,
    iter_ecostress,
    iter_rod,
    iter_rruff,
    load_cif_features,
    load_mindat_structure,
    pick_peaks,
    pick_troughs,
    read_ecostress_spectrum,
)
from training_form import canonicalize
from wurm_layer import fold_name, iter_wurm

_C_CM_S = 2.99792458e10

KINDS = (
    "formula",  # species -> IMA formula
    "raman_bands",  # species (+sample) -> Raman band list (RRUFF / ROD)
    "ir_troughs",  # species (+sample) -> reflectance minima (ECOSTRESS)
    "cross_modal",  # species -> joint signature across techniques
    "contrastive",  # same formula, different band lists
    "structure",  # species -> crystal system / cell / class / geometry
    "polymorph",  # same formula, different structures, different bands
    "computed",  # species -> ab-initio modes with irreps (WURM)
    "libs_lines",  # species -> predicted LIBS lines at T
    "libs_temperature",  # species -> lines at two T
    "corroboration",  # paper value beside reference value
    "ice_bandlist",  # SSHADE phase -> catalogued range/temperature
    "pack_fact",  # paper -> one packed key/value
)


@dataclass
class Fact:
    fact_id: str
    kind: str
    species: str | list[str]
    formula: str
    payload: dict
    provenance: dict = field(default_factory=dict)
    source: str = ""
    canonical: bool = True  # False for measurement-keyed facts


def _positions(peaks: list[dict], key: str) -> list[float]:
    return [round(float(p[key]), 1) for p in peaks if key in p]


def _intensities(peaks: list[dict], key: str) -> list[float]:
    """Relative intensities aligned with _positions(peaks, key). pick_peaks
    computes them and until 2026-09-11 every payload dropped them; the
    synthetic corpus needs the full (position, intensity) list because the
    strongest band flips identity in 29 % of same-species pairs."""
    return [
        round(float(p.get("relative_intensity", 0.0)), 3) for p in peaks if key in p
    ]


def _skip(species: str | Iterable[str], exclude: set[str]) -> bool:
    if isinstance(species, str):
        return species in exclude
    return any(s in exclude for s in species)


# ── builders ─────────────────────────────────────────────────────────
def formula_facts(ima: dict[str, str], exclude: set[str]) -> Iterator[Fact]:
    for sp, f in sorted(ima.items()):
        if f and sp not in exclude:
            yield Fact(
                f"formula:{sp}",
                "formula",
                sp,
                f,
                {},
                {"source": "IMA/mindat"},
                "mindat",
            )


def raman_facts(
    ima: dict[str, str],
    exclude: set[str],
    rruff_archive: str = "excellent_unoriented.zip",
) -> Iterator[Fact]:
    """Measurement-keyed Raman facts from every processed RRUFF spectrum and
    every ROD record; species-canonical facts via the richest-record rule."""
    richest: dict[str, tuple[int, Fact]] = {}
    for rec in iter_rruff(rruff_archive):
        sp = rec["species"].strip()
        if not sp or sp in exclude:
            continue
        peaks = pick_peaks(rec["spectrum"])
        if not peaks:
            continue
        bands = _positions(peaks, "position_cm-1")[:12]
        prov = {
            "source": "RRUFF",
            "derivation": "peak_pick",
            "sample_id": rec.get("rruff_id", ""),
            "laser_nm": rec.get("laser_nm", ""),
        }
        f = Fact(
            f"raman:RRUFF:{rec.get('rruff_id','')}",
            "raman_bands",
            sp,
            ima.get(sp) or normalize_formula(rec.get("ideal_formula", "")),
            {
                "bands_cm1": bands,
                "rel": _intensities(peaks, "position_cm-1")[:12],
                "tech": "Raman",
                "sample_id": rec.get("rruff_id", ""),
                "laser_nm": rec.get("laser_nm", ""),
                "locality": rec.get("locality", ""),
            },
            prov,
            "RRUFF",
            canonical=False,
        )
        yield f
        if sp not in richest or len(bands) > richest[sp][0]:
            richest[sp] = (len(bands), f)
    for rec in iter_rod():
        sp = rec["species"].strip()
        if not sp or sp in exclude:
            continue
        peaks = pick_peaks(rec["spectrum"])
        if not peaks:
            continue
        bands = _positions(peaks, "position_cm-1")[:12]
        prov = {
            "source": "ROD",
            "derivation": "peak_pick",
            "sample_id": f"ROD {rec['rod_id']}",
            "laser_nm": rec.get("laser_nm", ""),
        }
        yield Fact(
            f"raman:ROD:{rec['rod_id']}",
            "raman_bands",
            sp,
            ima.get(sp) or normalize_formula(rec.get("formula", "")),
            {
                "bands_cm1": bands,
                "rel": _intensities(peaks, "position_cm-1")[:12],
                "tech": "Raman",
                "sample_id": f"ROD {rec['rod_id']}",
                "laser_nm": rec.get("laser_nm", ""),
                "locality": rec.get("compound_source", ""),
            },
            prov,
            "ROD",
            canonical=False,
        )
    for sp, (_, f) in sorted(richest.items()):
        yield Fact(
            f"raman:canonical:{sp}",
            "raman_bands",
            sp,
            f.formula,
            dict(f.payload),
            dict(f.provenance),
            "RRUFF",
            canonical=True,
        )


def ir_facts(ima: dict[str, str], exclude: set[str]) -> Iterator[Fact]:
    eco_dir = os.path.join(REF_ROOT, "ecostress", "ecospeclib-all")
    richest: dict[tuple[str, str], tuple[int, Fact]] = {}
    for rec in iter_ecostress():
        sp = rec["species"].strip()
        if not sp or sp in exclude:
            continue
        troughs = pick_troughs(
            read_ecostress_spectrum(os.path.join(eco_dir, rec.get("file", "")))
        )
        if not troughs:
            continue
        band = (
            "thermal infrared"
            if rec.get("wavelength_range") == "TIR"
            else "visible/short-wave infrared reflectance"
        )
        pos = [round(float(t["position_um"]), 3) for t in troughs][:8]
        prov = {
            "source": "ECOSTRESS",
            "derivation": "trough_pick",
            "sample_id": rec.get("sample_no", ""),
            "mineral_class": rec.get("mineral_class", ""),
        }
        f = Fact(
            f"ir:ECOSTRESS:{rec.get('file','')}",
            "ir_troughs",
            sp,
            ima.get(sp, ""),
            {
                "troughs_um": pos,
                "tech": band,
                "sample_id": rec.get("sample_no", ""),
                "particle_size": rec.get("particle_size", ""),
            },
            prov,
            "ECOSTRESS",
            canonical=False,
        )
        yield f
        k = (sp, band)
        if k not in richest or len(pos) > richest[k][0]:
            richest[k] = (len(pos), f)
    for (sp, band), (_, f) in sorted(richest.items()):
        yield Fact(
            f"ir:canonical:{sp}:{band}",
            "ir_troughs",
            sp,
            f.formula,
            dict(f.payload),
            dict(f.provenance),
            "ECOSTRESS",
            canonical=True,
        )


def _canonical_modalities(facts: list[Fact]) -> dict[str, dict[str, dict]]:
    """species -> {tech: {positions, unit, provenance}} from canonical facts."""
    out: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for f in facts:
        if not f.canonical:
            continue
        if f.kind == "raman_bands":
            out[f.species]["Raman"] = {
                "positions": f.payload["bands_cm1"],
                "unit": "cm-1",
                "provenance": f.provenance,
            }
        elif f.kind == "ir_troughs":
            out[f.species][f.payload["tech"]] = {
                "positions": f.payload["troughs_um"],
                "unit": "um",
                "provenance": f.provenance,
            }
    return out


def cross_modal_facts(
    mods: dict[str, dict[str, dict]], ima: dict[str, str]
) -> Iterator[Fact]:
    for sp, techs in sorted(mods.items()):
        if len(techs) < 2:
            continue
        yield Fact(
            f"xmodal:{sp}",
            "cross_modal",
            sp,
            ima.get(sp, ""),
            {
                "modalities": {
                    t: {"positions": v["positions"], "unit": v["unit"]}
                    for t, v in techs.items()
                }
            },
            {t: v["provenance"] for t, v in techs.items()},
            "RRUFF+ECOSTRESS",
        )


def structure_facts(
    ima: dict[str, str], exclude: set[str], structures: dict, cifs: dict
) -> Iterator[Fact]:
    for sp in sorted(ima):
        if sp in exclude:
            continue
        st = structures.get(sp.lower())
        cf = cifs.get(sp.lower()) or {}
        if not st or not (st.get("crystal_system") or st.get("a")):
            continue
        payload = {
            "crystal_system": st.get("crystal_system"),
            "space_group": cf.get("space_group_symbol"),
            "cell": {
                k: st.get(k)
                for k in ("a", "b", "c", "alpha", "beta", "gamma")
                if st.get(k)
            },
            "strunz_class": st.get("strunz_class"),
            "density_calc": st.get("density_calc"),
            "hardness_max": st.get("hardness_max"),
            "coordination": cf.get("coordination") or {},
            "shortest_bond_a": cf.get("shortest_bond_a"),
            "shortest_bond_pair": cf.get("shortest_bond_pair"),
        }
        yield Fact(
            f"structure:{sp}",
            "structure",
            sp,
            ima.get(sp, ""),
            payload,
            {"source": "mindat" + ("+AMCSD" if cf else "")},
            "mindat" + ("+AMCSD" if cf else ""),
        )


def sibling_facts(ima, refmods, exclude, structures, cifs) -> Iterator[Fact]:
    groups = formula_siblings(
        ima, refmods, exclude=exclude, structures=structures, cifs=cifs
    )
    seen: set[str] = set()
    for sp, members in sorted(groups.items()):
        key = "|".join(sorted(m["species"] for m in members))
        if key in seen:
            continue
        seen.add(key)
        named = [m for m in members if m.get("peaks") and m["species"] not in exclude]
        if len(named) < 2:
            continue
        formula = ima.get(named[0]["species"], "")
        yield Fact(
            f"contrastive:{key}",
            "contrastive",
            [m["species"] for m in named],
            formula,
            {
                "members": [
                    {
                        "species": m["species"],
                        "bands_cm1": _positions(m["peaks"], "position_cm-1")[:3],
                    }
                    for m in named
                ]
            },
            {"source": "contrastive"},
            "RRUFF",
        )
        usable = [m for m in named if m.get("structure") or m.get("cif")]
        sigs = {
            (
                (m.get("structure") or {}).get("crystal_system"),
                (m.get("cif") or {}).get("space_group_symbol"),
            )
            for m in usable
        }
        if len(usable) >= 2 and len(sigs) >= 2:
            yield Fact(
                f"polymorph:{key}",
                "polymorph",
                [m["species"] for m in usable],
                formula,
                {
                    "members": [
                        {
                            "species": m["species"],
                            "crystal_system": (m.get("structure") or {}).get(
                                "crystal_system"
                            ),
                            "space_group": (m.get("cif") or {}).get(
                                "space_group_symbol"
                            ),
                            "bands_cm1": _positions(m["peaks"], "position_cm-1")[:4],
                        }
                        for m in usable
                    ]
                },
                {"source": "polymorph"},
                "mindat+AMCSD+RRUFF",
            )


def computed_facts(ima: dict[str, str], exclude: set[str]) -> Iterator[Fact]:
    fold_ima = {fold_name(s): s for s in ima}
    for rec in iter_wurm():
        sp = fold_ima.get(fold_name(rec["species"]), rec["species"])
        if sp in exclude:
            continue
        bands = [
            b
            for b in (rec.get("bands") or [])
            if b.get("intensity_rel") and b.get("position_cm-1", 0) > 0
        ]
        if not bands:
            continue
        seen: set = set()
        uniq = []
        for b in sorted(bands, key=lambda b: -b["intensity_rel"]):
            k = (round(b["position_cm-1"], 1), b.get("irrep") or "")
            if k in seen:
                continue
            seen.add(k)
            uniq.append(b)
        strong = sorted(uniq[:6], key=lambda b: b["position_cm-1"])
        yield Fact(
            f"computed:{rec.get('wurm_id', sp)}",
            "computed",
            sp,
            ima.get(sp, rec.get("formula", "")),
            {
                "modes": [
                    {
                        "cm1": round(b["position_cm-1"], 1),
                        "irrep": b.get("irrep") or "",
                        "rel": round(b["intensity_rel"], 3),
                    }
                    for b in strong
                ],
                "strongest_cm1": round(
                    max(strong, key=lambda b: b["intensity_rel"])["position_cm-1"], 1
                ),
                "space_group": rec.get("space_group_symbol"),
                "n_modes": len(rec.get("bands") or []),
            },
            {"source": "WURM", "wurm_id": rec.get("wurm_id")},
            "WURM",
        )


def libs_facts(species: Iterable[str], ima: dict[str, str]) -> Iterator[Fact]:
    t_cool, t_hot = TEMPERATURE_PAIR
    for sp in sorted(set(species)):
        formula = ima.get(sp, "")
        if not formula:
            continue
        groups = predict_species_lines(formula, DEFAULT_T)
        if not groups:
            continue

        def pack(gs):
            return [
                {
                    "stage_label": g["stage_label"],
                    "lines": [
                        {
                            "nm": round(ln["wavelength_nm_air"], 2),
                            "rel": round(ln["relative_intensity"], 1),
                            "ritz": ln.get("wavelength_basis") == "ritz",
                        }
                        for ln in g["lines"]
                    ],
                }
                for g in gs
            ]

        yield Fact(
            f"libs:{sp}",
            "libs_lines",
            sp,
            formula,
            {"temperature_k": DEFAULT_T, "groups": pack(groups)},
            {"source": "NIST ASD", "derivation": "boltzmann_lte"},
            "NIST ASD",
        )
        cool = predict_species_lines(formula, t_cool)
        hot = predict_species_lines(formula, t_hot)
        if cool and hot and pack(cool) != pack(hot):
            yield Fact(
                f"libs_t:{sp}",
                "libs_temperature",
                sp,
                formula,
                {
                    "t_cool": t_cool,
                    "t_hot": t_hot,
                    "cool": pack(cool),
                    "hot": pack(hot),
                },
                {"source": "NIST ASD", "derivation": "boltzmann_lte"},
                "NIST ASD",
            )


def corroboration_facts(
    mods, ima, exclude, species_papers: list[str]
) -> Iterator[Fact]:
    reports = load_paper_reports(
        {s: ima.get(s, "") for s in species_papers if s not in exclude}
    )
    for sp, entries in sorted(reports.items()):
        ref = mods.get(sp, {}).get("Raman")
        if not ref or sp in exclude:
            continue
        pairs = []
        for e in entries:
            if e["technique"] != "Raman":
                continue
            for value in e["values"]:
                nearest = min(ref["positions"], key=lambda p: abs(p - value))
                if within_tolerance(value, nearest):
                    pairs.append((abs(value - nearest), value, nearest, e["paper"]))
        pairs.sort(key=lambda t: t[0])
        for i, (_, value, nearest, paper) in enumerate(
            pairs[:MAX_CORROBORATIONS_PER_SPECIES]
        ):
            yield Fact(
                f"corr:{sp}:{i}",
                "corroboration",
                sp,
                ima.get(sp, ""),
                {
                    "reported": value,
                    "reference": nearest,
                    "tech": "Raman",
                    "citation": paper.get("citation", ""),
                    "paper_key": paper.get("paper_key", ""),
                    "ref_sample": ref["provenance"].get("sample_id", ""),
                },
                {"paper": paper, "reference": ref["provenance"]},
                "corpus+RRUFF",
            )


def ice_facts() -> Iterator[Fact]:
    path = os.path.join(REF_ROOT, "sshade", "sshade_bandlist.csv")
    if not os.path.exists(path):
        return
    with open(path, newline="") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            name = (row.get("species_name") or "").strip()
            if not name:
                continue
            try:
                lo, hi = (
                    float(row["spectral_range_min"]) / _C_CM_S,
                    float(row["spectral_range_max"]) / _C_CM_S,
                )
            except (KeyError, TypeError, ValueError):
                lo = hi = 0.0
            cls = [c for c in (row.get("sample_classification") or "").split("#") if c]
            yield Fact(
                f"ice:{i}",
                "ice_bandlist",
                name,
                "",
                {
                    "phase": (row.get("alt_target_name") or name).strip(),
                    "classification": cls[:3],
                    "range_cm1": [round(lo), round(hi)] if hi > lo else None,
                    "waveband": "/".join(
                        c for c in (row.get("waveband") or "").split("#") if c
                    ),
                    "temperature_k": (row.get("temperature") or "").strip(),
                },
                {"source": "SSHADE"},
                "SSHADE",
            )


def pack_facts(max_keys: int = 12) -> Iterator[Fact]:
    for path in sorted(glob.glob(os.path.join(DATASET, "*.json"))):
        if path.endswith("key_registry.json"):
            continue
        try:
            art = json.load(open(path))
        except Exception:  # noqa: BLE001
            continue
        title = (art.get("title") or "").strip()
        key = art.get("paper_key", "")
        if not title or not key:
            continue
        data = canonicalize(art.get("data") or {})
        n = 0
        for k, v in data.items():
            if k.endswith("_as_packed") or v in (None, "", [], {}):
                continue
            rendered = (
                json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            )
            if len(rendered) > 160:
                continue
            yield Fact(
                f"pack:{key}:{k}",
                "pack_fact",
                [],
                "",
                {
                    "title": title[:140],
                    "key": k.replace("_", " "),
                    "value": rendered,
                    "paper_key": key,
                },
                {
                    "source": "corpus",
                    "paper_key": key,
                    "license": art.get("license", ""),
                },
                "corpus",
            )
            n += 1
            if n >= max_keys:
                break


# ── entry point ──────────────────────────────────────────────────────
def build_facts(
    exclude: set[str] | None = None, *, rruff_archive: str = "excellent_unoriented.zip"
) -> list[Fact]:
    exclude = set(exclude or ())
    # Plain-textbook spelling everywhere (formula_norm): mindat's HTML-stripped
    # IMA strings still carry charges/vacancy boxes and 13 conventions live in
    # the paper markdown; every fact's formula is the one spelling the model
    # should learn and emit (PROCEDURE §20c: Fe3O4 = 4 tokens / 1.4 nats).
    ima = {k: normalize_formula(v) or v for k, v in load_ima().items()}
    species_papers = json.load(
        open(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "species_papers.json"
            )
        )
    )["papers"]
    refmods = load_reference_modalities(rruff_archive=rruff_archive)
    structures = load_mindat_structure()
    cifs = load_cif_features()

    facts: list[Fact] = list(formula_facts(ima, exclude))
    spectral = list(raman_facts(ima, exclude, rruff_archive)) + list(
        ir_facts(ima, exclude)
    )
    facts += spectral
    mods = _canonical_modalities(spectral)
    facts += list(cross_modal_facts(mods, ima))
    facts += list(structure_facts(ima, exclude, structures, cifs))
    facts += list(sibling_facts(ima, refmods, exclude, structures, cifs))
    facts += list(computed_facts(ima, exclude))
    facts += list(libs_facts([s for s in mods if s in ima], ima))
    facts += list(corroboration_facts(mods, ima, exclude, species_papers))
    facts += list(ice_facts())
    facts += list(pack_facts())
    return facts


def census(facts: list[Fact]) -> dict:
    c = collections.Counter(f.kind for f in facts)
    canon = collections.Counter(f.kind for f in facts if f.canonical)
    return {"total": len(facts), "by_kind": dict(c), "canonical_by_kind": dict(canon)}


if __name__ == "__main__":
    import sys

    excl = set()
    if len(sys.argv) > 1:
        excl = set(json.load(open(sys.argv[1]))["species"])
    fs = build_facts(excl)
    print(json.dumps(census(fs), indent=1))
