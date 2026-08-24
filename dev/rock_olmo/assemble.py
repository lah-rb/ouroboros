"""Assemble the interconnected training record stream.

DETERMINISTIC BY DESIGN — no inference anywhere. Every step is a join, a
lookup, a signal-processing pass or a template render, so the output is
reproducible from the corpus and the reference layer alone. The one step
that would have needed a model — pulling reported peak positions out of
paper prose — was already paid for and is banked as structured fields in
the packs.

SURFACE-FORM VARIATION WITHOUT A MODEL. Rendering 2,800 records from one
template per view would teach the template rather than the relation —
the same failure the multi-view design exists to avoid, reintroduced at
the sentence level. So each view carries several phrasings and picks one
by a STABLE HASH of the record's identity: varied across the corpus,
identical on every re-run, and no fabrication risk. If a trained model
still parrots the phrasing, that is the evidence that buys paraphrase
inference — and it will then be known which views need it.

WEIGHTING IS VIEWS-AS-WEIGHT (operator ruling). A species emits every
view it can support and no more: well-covered species naturally reach 7
or 8, thin ones get 2. Exposure follows evidence rather than a target,
and nothing is padded to hit a multiplier.

REFERENCE-ONLY SPECIES ARE CAPPED at parity with the paper-backed views.
Uncapped they outnumber them roughly 3:1 and the mix stops being a
literature corpus. Selection inside the cap prefers COVERAGE BREADTH —
species from mineral classes the paper corpus underrepresents — over
another example of a class already thick.
"""

from __future__ import annotations

import collections
import glob
import hashlib
import json
import os
import re
from typing import Any, Iterable, Iterator

from holdout import elements, select_holdout
from interconnect import build_views
from reference_layer import (
    REF_ROOT,
    iter_ecostress,
    iter_rruff,
    load_asd_lines,
    pick_peaks,
    pick_troughs,
    read_ecostress_spectrum,
)
from training_form import canonicalize

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
DATASET = os.path.join(CORPUS, "databank", "dataset")

#: Pack keys whose values are Raman/FTIR band positions, for the
#: corroboration view. Matches the schema the repack converged on.
_RAMAN_KEY = re.compile(r"raman.*(peak|band|wavenumber|shift)|(peak|band).*raman", re.I)
_FTIR_KEY = re.compile(r"(ftir|infrared).*(peak|band|wavenumber)|(peak|band).*(ftir|infrared)", re.I)


def stable_choice(seq: list, *parts: str) -> Any:
    """Deterministic pick from ``seq`` keyed on ``parts``.

    A hash, not random.choice: the same species must render the same way
    on every run or the corpus is not reproducible and two builds cannot
    be diffed.
    """
    if not seq:
        return None
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return seq[int(h[:8], 16) % len(seq)]


def normalise_formula(formula: str) -> str:
    """Formula reduced to a comparison key for sibling grouping."""
    return re.sub(r"[^A-Za-z0-9]", "", formula or "")


# ── loaders ──────────────────────────────────────────────────────────
def load_ima() -> dict[str, str]:
    out: dict[str, str] = {}
    path = os.path.join(REF_ROOT, "mindat", "minerals_ima.jsonl")
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        name = (r.get("name") or "").strip()
        if name:
            out[name] = re.sub(r"<[^>]+>", "", str(r.get("ima_formula") or ""))
    return out


def _numbers(value: Any, out: list[float], lo: float = 100.0, hi: float = 4000.0) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _numbers(v, out, lo, hi)
    elif isinstance(value, list):
        for v in value:
            _numbers(v, out, lo, hi)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if lo <= value <= hi:
            out.append(float(value))


def load_paper_reports(species_index: dict[str, str]) -> dict[str, list[dict]]:
    """species -> reported band positions, with the citing paper.

    Read from the CANONICAL form of each pack so units are normalised
    before comparison. Attribution is by species name appearing in the
    pack; a paper naming several species contributes to each, which is
    correct — it did measure them.
    """
    lowered = {s.lower(): s for s in species_index}
    out: dict[str, list[dict]] = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(DATASET, "*.json"))):
        if path.endswith("key_registry.json"):
            continue
        try:
            art = json.load(open(path))
        except Exception:
            continue
        data = canonicalize(art.get("data") or {})
        for pattern, technique in ((_RAMAN_KEY, "Raman"), (_FTIR_KEY, "FTIR")):
            vals: list[float] = []
            for key, value in data.items():
                if key.endswith("_as_packed"):
                    continue
                if pattern.search(key):
                    _numbers(value, vals)
            if not vals:
                continue
            blob = json.dumps(
                {"t": art.get("title", ""), "d": art.get("data") or {}}, ensure_ascii=False
            ).lower()
            citation = art.get("title") or art.get("paper_key", "")
            for low, name in lowered.items():
                if low in blob:
                    out[name].append(
                        {
                            "technique": technique,
                            "values": sorted(set(vals)),
                            "paper": {
                                "citation": citation[:120],
                                "paper_key": art.get("paper_key", ""),
                                "identifier": art.get("identifier") or art.get("doi", ""),
                            },
                        }
                    )
    return out


def load_reference_modalities(
    wanted: set[str] | None = None, rruff_archive: str = "excellent_unoriented.zip"
) -> dict[str, dict]:
    """species -> {modalities, provenance} from RRUFF and ECOSTRESS."""
    out: dict[str, dict] = {}
    for rec in iter_rruff(rruff_archive):
        species = (rec.get("species") or "").strip()
        if not species or (wanted and species not in wanted):
            continue
        peaks = pick_peaks(rec["spectrum"])
        if not peaks:
            continue
        slot = out.setdefault(species, {"modalities": {}, "provenance": {}, "facts": {}})
        # Keep the RICHEST record per species rather than the last one.
        prior = slot["modalities"].get("Raman")
        if prior is None or len(peaks) > len(prior):
            slot["modalities"]["Raman"] = peaks[:12]
            slot["provenance"]["Raman"] = {
                "source": "RRUFF",
                "derivation": "peak_pick",
                "sample_id": rec.get("rruff_id", ""),
                "laser_nm": rec.get("laser_nm", ""),
            }
            slot["facts"].update(
                {
                    "ideal_formula": rec.get("ideal_formula", ""),
                    "measured_formula": rec.get("measured_formula", ""),
                    "locality": rec.get("locality", ""),
                    "cell": rec.get("cell", {}),
                    "confirmation": rec.get("confirmation", ""),
                }
            )
    eco_dir = os.path.join(REF_ROOT, "ecostress", "ecospeclib-all")
    for rec in iter_ecostress():
        species = (rec.get("species") or "").strip()
        if not species or (wanted and species not in wanted):
            continue
        slot = out.setdefault(species, {"modalities": {}, "provenance": {}, "facts": {}})
        slot["facts"].setdefault("mineral_class", rec.get("mineral_class", ""))
        slot["facts"].setdefault("particle_size", rec.get("particle_size", ""))
        band = (
            "thermal infrared"
            if rec.get("wavelength_range") == "TIR"
            else "visible/short-wave infrared reflectance"
        )
        # ECOSTRESS ships the SPECTRUM, so the diagnostic features have
        # to be derived like RRUFF's — but as absorption TROUGHS, not
        # peaks: these are reflectance data, where the information is in
        # the minima. Without this the cross-modal view could never fire,
        # since a modality with provenance but no features produces
        # nothing (measured: 0 cross_modal records on the first run).
        spectrum = read_ecostress_spectrum(os.path.join(eco_dir, rec.get("file", "")))
        troughs = pick_troughs(spectrum)
        if not troughs:
            continue
        prior = slot["modalities"].get(band)
        if prior is None or len(troughs) > len(prior):
            slot["modalities"][band] = troughs[:8]
            slot["provenance"][band] = {
                "source": "ECOSTRESS",
                "derivation": "trough_pick",
                "sample_id": rec.get("sample_no", ""),
                "mineral_class": rec.get("mineral_class", ""),
            }
    return out


# ── sibling grouping (contrastive view) ──────────────────────────────
def formula_siblings(
    ima: dict[str, str], refmods: dict[str, dict], exclude: set[str] | None = None
) -> dict[str, list[dict]]:
    """species -> co-members of its formula group that have spectra.

    Only species with peaks can be contrasted; naming a sibling we
    cannot characterise teaches nothing. Polymorphs are the point
    (antigorite / lizardite / chrysotile all being Mg3Si2O5(OH)4), so
    grouping is on the formula alone.
    """
    # HELD-OUT SPECIES ARE EXCLUDED FROM SIBLING LISTS. A contrastive
    # view names every member of its formula group, so a train species
    # whose group contains a held-out one leaks it verbatim: "Anatase,
    # Brookite, Rutile share the composition Ti4+O2" published two
    # held-out names while training on rutile.
    exclude = exclude or set()
    groups: dict[str, list[str]] = collections.defaultdict(list)
    for species, formula in ima.items():
        if species in exclude:
            continue
        key = normalise_formula(formula)
        if key and species in refmods and refmods[species]["modalities"].get("Raman"):
            groups[key].append(species)
    out: dict[str, list[dict]] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        for species in members:
            out[species] = [
                {"species": m, "peaks": refmods[m]["modalities"]["Raman"][:3]}
                for m in sorted(members)
            ]
    return out


def _implied_class(formula: str) -> str:
    """Anion group a formula implies, for breadth ranking when ECOSTRESS
    supplies no class."""
    if "CO3" in formula:
        return "carbonate"
    if "SO4" in formula:
        return "sulfate"
    if "PO4" in formula or "AsO4" in formula:
        return "phosphate"
    if "Si" in formula:
        return "silicate"
    if re.search(r"S(?![eibnr])", formula):
        return "sulfide"
    if "O" in formula:
        return "oxide"
    return "other"


def select_reference_only(
    candidates: Iterable[str],
    refmods: dict[str, dict],
    ima: dict[str, str],
    corpus_classes: collections.Counter,
    budget: int,
) -> list[str]:
    """Reference-only species to include, preferring coverage BREADTH.

    Uncapped these outnumber paper-backed species roughly 3:1 and the
    mix stops being a literature corpus. Inside the budget the useful
    ones fill mineral classes the papers underrepresent — another
    feldspar adds little where a class with no coverage adds a lot.
    Deterministic: rarest class first, then species name.
    """
    ranked = []
    for species in candidates:
        cls = (refmods.get(species, {}).get("facts", {}) or {}).get("mineral_class") or ""
        if not cls:
            cls = _implied_class(ima.get(species, ""))
        ranked.append((corpus_classes.get(cls, 0), cls, species))
    ranked.sort()
    return [species for _, _, species in ranked[:budget]]


#: Alternative phrasings per view, selected by stable hash so the corpus
#: varies in surface form without varying between runs. Deliberately
#: modest — the goal is to break the single-template signature, not to
#: simulate prose diversity, which is what a paraphrase model would be
#: for if this proves insufficient.
PHRASINGS = {
    "forward": ("states", "reports", "characterises"),
    "inverse": ("identifies", "indicates", "points to"),
    "cross_modal": ("joint", "multimodal", "combined"),
    "contrastive": ("discriminate", "separate", "distinguish"),
    "corroboration": ("compare", "corroborate", "check"),
}

#: Corroborations emitted per species. Uncapped, this view produced 79%
#: of paper-backed records and swamped the other four.
MAX_CORROBORATIONS_PER_SPECIES = 6

SPLIT_TRAIN = "train"
SPLIT_HOLDOUT = "holdout"


def _species_papers_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "species_papers.json")


def _names_any(text: str, lowered: dict[str, str]) -> bool:
    """Whether ``text`` names any of ``lowered``, on word boundaries."""
    low = (text or "").lower()
    return any(
        re.search(rf"(?<![a-z]){re.escape(k)}(?![a-z])", low) for k in lowered
    )


def assemble(rruff_archive: str = "excellent_unoriented.zip", limit_species: int = 0) -> dict:
    """Build the record stream. Deterministic end to end."""
    ima = load_ima()
    species_papers = json.load(open(_species_papers_path()))["papers"]
    holdout = {s for picks in select_holdout(species_papers, ima).values() for s in picks}
    refmods = load_reference_modalities(rruff_archive=rruff_archive)
    reports = load_paper_reports({s: ima.get(s, "") for s in species_papers})
    siblings = formula_siblings(ima, refmods, exclude=holdout)
    corpus_classes = collections.Counter(
        (refmods.get(s, {}).get("facts", {}) or {}).get("mineral_class") or "unknown"
        for s in species_papers
    )

    records: list[dict] = []
    stats: collections.Counter = collections.Counter()

    holdout_lower = {h.lower(): h for h in holdout}

    def emit(species: str, split: str, origin: str) -> int:
        formula = ima.get(species, "")
        slot = refmods.get(species) or {}
        mods = dict(slot.get("modalities") or {})
        prov = dict(slot.get("provenance") or {})
        reported: list[dict] = []
        for entry in reports.get(species, []):
            ref_peaks = mods.get("Raman") if entry["technique"] == "Raman" else None
            if not ref_peaks:
                continue
            for value in entry["values"]:
                nearest = min(ref_peaks, key=lambda p: abs(p["position_cm-1"] - value))
                reported.append(
                    {
                        "reported": value,
                        "reference": nearest["position_cm-1"],
                        "technique": entry["technique"],
                        "paper": entry["paper"],
                        "ref": prov.get("Raman", {"source": "RRUFF"}),
                    }
                )
        # CAP CORROBORATIONS PER SPECIES. Every reported value crossed
        # with its nearest reference peak is a candidate, and on the
        # first full run that produced 4,176 of 5,266 paper-backed
        # records — one view drowning the other four, which defeats the
        # point of having four. Closest pairs first, so the cap keeps
        # the most informative comparisons.
        # A CITATION CAN LEAK. Paper titles name the species they
        # studied, so "Raman spectroscopic study of azurite and
        # malachite" published a held-out name inside a malachite
        # record. Titles naming a held-out species are replaced with a
        # neutral reference rather than dropping the comparison, which
        # is still valid data about the species being trained on.
        if split == SPLIT_TRAIN and holdout_lower:
            for entry in reported:
                title = (entry.get("paper") or {}).get("citation") or ""
                if _names_any(title, holdout_lower):
                    entry["paper"] = dict(
                        entry["paper"], citation="a study in this corpus", citation_redacted=True
                    )
        reported.sort(key=lambda r: abs(r["reported"] - r["reference"]))
        reported = reported[:MAX_CORROBORATIONS_PER_SPECIES]
        views = build_views(
            {
                "species": species,
                "formula": formula,
                "modalities": mods,
                "provenance": prov,
                "siblings": siblings.get(species),
                "reported": reported,
            }
        )
        for view in views:
            variants = PHRASINGS.get(view["view"])
            if variants:
                view["phrasing"] = stable_choice(list(variants), species, view["view"])
            view.update({"split": split, "origin": origin, "formula": formula})
            records.append(view)
            stats[f"{view['view']}:{split}"] += 1
        return len(views)

    paper_species = sorted(species_papers)
    if limit_species:
        paper_species = paper_species[:limit_species]
    for species in paper_species:
        emit(species, SPLIT_HOLDOUT if species in holdout else SPLIT_TRAIN, "paper_backed")

    paper_train_views = sum(
        1 for r in records if r["origin"] == "paper_backed" and r["split"] == SPLIT_TRAIN
    )
    ref_only = [s for s in sorted(refmods) if s not in species_papers and s not in holdout]
    picked = select_reference_only(ref_only, refmods, ima, corpus_classes, budget=len(ref_only))
    emitted = 0
    for species in picked:
        if emitted >= paper_train_views:
            break
        emitted += emit(species, SPLIT_TRAIN, "reference_only")

    return {
        "records": records,
        "counts": dict(stats),
        "species": {
            "paper_backed": len(paper_species),
            "held_out": len(holdout & set(paper_species)),
            "reference_only_available": len(ref_only),
            "reference_only_used": emitted and len(
                {r["species"] for r in records if r["origin"] == "reference_only"
                 and isinstance(r["species"], str)}
            ),
        },
        "holdout": sorted(holdout),
    }
