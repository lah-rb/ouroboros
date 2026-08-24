"""Interconnected views of one spectral fact, for training.

THE PROBLEM WITH REPETITION. Oversampling an important dataset by
showing the same record four times teaches the surface form: the model
learns the sentence, not the relation, and the extra copies buy
memorisation rather than competence. Four DIFFERENT framings of the same
underlying fact cost the same tokens and each exercises a distinct
retrieval direction (operator's design, 2026-08-24).

THE VIEWS, and why each is not redundant:

  FORWARD        species -> spectrum. The textbook direction, and the
                 one the literature already supplies.
  INVERSE        spectrum -> species. THE ACTUAL WORKING TASK — a
                 spectroscopist arrives with bands and needs a name.
                 Forward prose does not teach this direction.
  CROSS_MODAL    one species, several techniques at once. Teaches that a
                 material has a JOINT signature; measured over the
                 corpus, 97 species carry Raman + thermal-IR + paper
                 coverage and account for 59% of all species mentions.
  CONTRASTIVE    near neighbours that share a formula. Antigorite,
                 lizardite and chrysotile are all Mg3Si2O5(OH)4 and are
                 separated only by band detail — the real hard problem,
                 and invisible to any single-species view.
  CORROBORATION  a paper's reported value beside the reference. The only
                 view that uses BOTH corpora, and the one that teaches
                 checkability.

REPORTED VERSUS REFERENCE (operator ruling). The reference is the
grounding point, but a paper that differs is not therefore wrong: the
gap may be PRECISION (rounding, a digitised plot) or ACCURACY
(calibration, a different polytype, a substituted cation). Views state
the difference and name both possibilities rather than asserting an
uncertainty budget or declaring the reference authoritative.

PROVENANCE IS NEVER SMOOTHED. RRUFF publishes spectra, not peak lists,
so a peak position attributed to it is our own peak-pick and says so.
A reader of the training data can always tell a measured value from a
derived one.
"""

from __future__ import annotations

from typing import Any, Iterable

#: Datasets the operator has marked for repeated presentation. The
#: repetition is delivered as DISTINCT VIEWS, not copies, so a "4x"
#: dataset appears four times in four framings.
PRIORITY_SOURCES = ("RRUFF", "ECOSTRESS", "SSHADE", "NIST_ASD")

VIEWS = ("forward", "inverse", "cross_modal", "contrastive", "corroboration")


def _fmt_peaks(
    peaks: Iterable[dict], key: str = "position_cm-1", unit: str = "cm-1"
) -> str:
    vals = [p[key] for p in peaks if key in p]
    if not vals:
        return ""
    return ", ".join(f"{v:g}" for v in vals) + f" {unit}"


#: Wavenumber agreement tiers, in cm-1. Set from BOTH a measurement over
#: this corpus and the field's own conventions, because either alone
#: would have been a guess.
#:
#: MEASURED: 22,913 paper-reported Raman values paired against their
#: nearest RRUFF peak-pick for a species the paper names. Binned at
#: 1 cm-1, the bin-to-bin decay runs 0.51, 0.76, 0.81, 0.83, 0.80, 0.79
#: through bin 6 and then FLATTENS — 0.94, 0.97, 0.88, 0.94, 1.14. The
#: steep part is the genuine population; the flat part is coincidental
#: proximity, running about 1% of pairs per bin. So real agreement is
#: concentrated below ~7 cm-1.
#:
#: FIELD PRACTICE: a well-calibrated modern Raman spectrometer holds
#: sub-1 cm-1 wavenumber accuracy (~0.33 cm-1 MAE typical, ~1 cm-1 at
#: the detector edge), while realistic calibration DRIFT reaches
#: ±9 cm-1 — and ±10 cm-1 is the tolerance conventionally accepted when
#: comparing spectra against RRUFF.
#:
#: The two agree, which is the reassuring part: the measured knee at
#: ~7 sits inside the ±10 convention, and the ±9 drift figure explains
#: why the genuine population reaches that far at all.
INSTRUMENT_PRECISION_CM1 = 1.0
REPORTING_PRECISION_CM1 = 3.0
ACCEPTED_COMPARISON_CM1 = 10.0


def within_tolerance(reported: float, reference: float) -> bool:
    """Whether a pair is close enough to be worth asserting at all.

    Beyond the field's ±10 cm-1 comparison tolerance a "match" is more
    likely a DIFFERENT vibrational mode or a second phase than the same
    band shifted, and emitting it would manufacture a corroboration the
    data does not support. Measured here: past 10 cm-1 the distribution
    is flat, i.e. indistinguishable from coincidence.
    """
    return abs(reported - reference) <= ACCEPTED_COMPARISON_CM1


def _tol_phrase(reported: float, reference: float) -> str:
    """How a reported value relates to the reference.

    The reference grounds the comparison; it does not adjudicate. A
    difference is named as precision or as accuracy, with physical
    causes offered rather than a verdict (operator ruling 2026-08-24).
    """
    gap = abs(reported - reference)
    if gap == 0:
        return "matching the reference position exactly"
    if gap <= INSTRUMENT_PRECISION_CM1:
        return (
            f"differing by {gap:.2g} — inside the wavenumber accuracy a "
            "calibrated spectrometer holds, so the two agree"
        )
    if gap <= REPORTING_PRECISION_CM1:
        return (
            f"differing by {gap:.2g}, within the precision at which band "
            "positions are normally reported"
        )
    if gap <= ACCEPTED_COMPARISON_CM1:
        return (
            f"offset by {gap:.2g}, inside the tolerance conventionally accepted "
            "when comparing against reference spectra — consistent with "
            "calibration drift, or with a real shift from substitution or "
            "crystallinity in this sample"
        )
    return (
        f"offset by {gap:.3g}, beyond the tolerance normally accepted for "
        "comparison — more likely a different vibrational mode or a second "
        "phase than the same band displaced"
    )


def view_forward(
    species: str, formula: str, tech: str, peaks: list[dict], provenance: dict
) -> dict:
    body = _fmt_peaks(peaks)
    if not body:
        return {}
    derived = provenance.get("derivation") == "peak_pick"
    how = (
        f"peak-picked from the {provenance.get('source','reference')} spectrum"
        if derived
        else f"as catalogued by {provenance.get('source','the reference')}"
    )
    return {
        "view": "forward",
        "species": species,
        "text": (
            f"{species} ({formula}) shows {tech} bands at {body}, {how}"
            f"{' (' + provenance['sample_id'] + ')' if provenance.get('sample_id') else ''}."
        ),
        "provenance": provenance,
    }


def view_inverse(
    species: str, formula: str, tech: str, peaks: list[dict], provenance: dict
) -> dict:
    body = _fmt_peaks(peaks[:4])
    if not body:
        return {}
    return {
        "view": "inverse",
        "species": species,
        "text": (
            f"A {tech} spectrum with bands at {body} is characteristic of "
            f"{species}, whose composition is {formula}."
        ),
        "provenance": provenance,
    }


def view_cross_modal(
    species: str, formula: str, modalities: dict, provenance: dict
) -> dict:
    """One species, several techniques — the joint signature."""
    parts = []
    for tech, peaks in modalities.items():
        body = _fmt_peaks(peaks)
        if body:
            parts.append(f"{tech} at {body}")
    if len(parts) < 2:
        return {}
    return {
        "view": "cross_modal",
        "species": species,
        "text": (
            f"{species} ({formula}) can be identified across techniques: "
            + "; ".join(parts)
            + ". The same material presents a different signature in each."
        ),
        "provenance": provenance,
    }


def view_contrastive(formula: str, members: list[dict]) -> dict:
    """Species sharing a formula, separated by band detail."""
    named = [m for m in members if m.get("peaks")]
    if len(named) < 2:
        return {}
    lines = [f"{m['species']}: {_fmt_peaks(m['peaks'][:3])}" for m in named]
    return {
        "view": "contrastive",
        "species": sorted(m["species"] for m in named),
        "text": (
            f"{', '.join(m['species'] for m in named)} share the composition "
            f"{formula} and cannot be told apart by chemistry alone. Their "
            "spectra differ: " + "; ".join(lines) + "."
        ),
        "provenance": {"source": "contrastive", "formula": formula},
    }


def view_corroboration(
    species: str, reported: float, reference: float, tech: str, paper: dict, ref: dict
) -> dict:
    """A paper's reported position beside the reference position."""
    derived = ref.get("derivation") == "peak_pick"
    ref_desc = (
        f"peak-picked from the {ref.get('source','reference')} spectrum of "
        f"{ref.get('sample_id','a characterised specimen')}"
        if derived
        else f"catalogued by {ref.get('source','the reference')}"
    )
    return {
        "view": "corroboration",
        "species": species,
        "text": (
            f"For {species}, {paper.get('citation','a study in this corpus')} reports a "
            f"{tech} band at {reported:g}. The reference position is {reference:g}, "
            f"{ref_desc} — the reported value {_tol_phrase(reported, reference)}."
        ),
        "provenance": {"paper": paper, "reference": ref},
    }


def build_views(record: dict) -> list[dict]:
    """Every applicable view for one assembled species record.

    ``record`` carries: species, formula, modalities {technique: peaks},
    optional siblings (same formula) and optional reported values from
    papers. Missing inputs simply produce fewer views — a species with
    one technique and no paper still yields forward and inverse.
    """
    out: list[dict] = []
    species = record.get("species", "")
    formula = record.get("formula", "")
    if not species:
        return out
    mods: dict[str, list[dict]] = record.get("modalities") or {}
    prov = record.get("provenance") or {}
    for tech, peaks in mods.items():
        for fn in (view_forward, view_inverse):
            v = fn(species, formula, tech, peaks, prov.get(tech, prov))
            if v:
                out.append(v)
    v = view_cross_modal(species, formula, mods, prov)
    if v:
        out.append(v)
    sibs = record.get("siblings") or []
    if sibs:
        v = view_contrastive(formula, sibs)
        if v:
            out.append(v)
    for rep in record.get("reported") or []:
        # GATE, not just phrasing: a pair beyond the accepted comparison
        # tolerance is not a weak corroboration, it is probably a
        # different vibrational mode. Emitting it would teach a false
        # equivalence between two unrelated bands.
        if not within_tolerance(rep["reported"], rep["reference"]):
            continue
        v = view_corroboration(
            species,
            rep["reported"],
            rep["reference"],
            rep.get("technique", "Raman"),
            rep.get("paper", {}),
            rep.get("ref", {}),
        )
        out.append(v)
    return out
