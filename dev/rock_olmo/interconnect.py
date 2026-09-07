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

VIEWS = (
    "forward",
    "inverse",
    "cross_modal",
    "contrastive",
    "corroboration",
    "structure",
    "polymorph",
    "computed",
)


#: Position key -> the unit it is expressed in. A feature list carries
#: its own axis: Raman/FTIR peaks are wavenumbers, reflectance troughs
#: are wavelengths. Hardcoding one key silently formatted the other to
#: an empty string, so the cross-modal view saw a single modality and
#: emitted nothing — 0 cross_modal records until this was found, despite
#: 172 species having two or more modalities indexed.
_POSITION_KEYS = (
    ("position_cm-1", "cm-1"),
    ("position_um", "um"),
    ("position_nm", "nm"),
    ("peak_cm-1", "cm-1"),
    ("wavelength_nm", "nm"),
)


def _fmt_peaks(
    peaks: Iterable[dict], key: str | None = None, unit: str | None = None
) -> str:
    peaks = list(peaks)
    if not peaks:
        return ""
    if key is None:
        for candidate, candidate_unit in _POSITION_KEYS:
            if any(candidate in p for p in peaks):
                key, unit = candidate, candidate_unit
                break
    if key is None:
        return ""
    vals = [p[key] for p in peaks if key in p]
    if not vals:
        return ""
    return ", ".join(f"{v:g}" for v in vals) + (f" {unit}" if unit else "")


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


#: derivation tag -> how the position was obtained, in words.
_DERIVATION_PHRASES = {
    "peak_pick": "peak-picked from the {source} spectrum",
    "trough_pick": "read as absorption minima from the {source} spectrum",
    "formula_to_lines": "derived from the formula against {source}",
}


def _derivation_phrase(provenance: dict) -> str:
    source = provenance.get("source", "the reference")
    tag = provenance.get("derivation")
    if tag in _DERIVATION_PHRASES:
        return _DERIVATION_PHRASES[tag].format(source=source)
    return f"as catalogued by {source}"


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
    # ANY derivation, not just peak_pick. Checking one derivation name
    # made ECOSTRESS trough-picks read "as catalogued by ECOSTRESS" —
    # asserting the library published positions it does not publish,
    # which is precisely the provenance smoothing this module exists to
    # avoid.
    how = _derivation_phrase(provenance)
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
    ref_desc = _derivation_phrase(ref)
    if ref.get("sample_id"):
        ref_desc += f" of {ref['sample_id']}"
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
    st = record.get("structure") or {}
    cf = record.get("cif") or {}
    if st:
        v = view_structure(species, formula, st, cf)
        if v:
            out.append(v)
    if record.get("computed"):
        v = view_computed(record["computed"])
        if v:
            out.append(v)
    sibs = record.get("siblings") or []
    if sibs:
        v = view_contrastive(formula, sibs)
        if v:
            out.append(v)
        # The polymorph view supersedes nothing — it runs BESIDE the
        # contrastive one, adding the structural reason the contrastive
        # view cannot give. It self-suppresses when the siblings carry no
        # structure or do not actually differ structurally.
        v = view_polymorph(formula, sibs)
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


# ---------------------------------------------------------------------------
# STRUCTURAL VIEWS
#
# Run 2's measured result is the whole argument for these. Everything the
# model learned, it read off the chemical FORMULA; the mineral name was
# worse than useless (name-only scored below a constant-prompt control);
# and on polymorphs — same formula, different structure — every predictor
# we built sat at the unrelated-minerals floor. Composition cannot encode
# bonding geometry, and Raman frequency IS bonding geometry.
#
# The other measured result shaping these: adding exposures of the same
# fact did nothing, while a genuinely NEW VIEW of the same species was
# worth +0.0714 holdout. So structure enters as views, not as repetition.
# ---------------------------------------------------------------------------


def _fmt_cell(st: dict) -> str:
    """Cell edges present in the source, in angstroms. mindat omits b for
    tetragonal/hexagonal and both b and c for cubic rather than repeating
    a; printing a placeholder would assert a measurement nobody made."""
    parts = [f"{k} = {st[k]:.4g} Å" for k in ("a", "b", "c") if st.get(k)]
    _GREEK = {"alpha": "α", "beta": "β", "gamma": "γ"}
    ang = [
        f"{_GREEK[k]} = {st[k]:.4g}°"
        for k in ("alpha", "beta", "gamma")
        if st.get(k) and abs(st[k] - 90.0) > 1e-6
    ]
    return ", ".join(parts + ang)


def _fmt_geometry(cf: dict) -> str:
    """Coordination and shortest bond from the CIF-derived features."""
    if not cf:
        return ""
    bits = []
    coord = cf.get("coordination") or {}
    if coord:
        # These are MEANS over crystallographically distinct sites. A
        # cation sitting in two different environments yields e.g. 6.75,
        # and "Ca in 6.75-fold coordination" states something that does
        # not exist — coordination numbers are integers per site. Print an
        # integer only when every site agrees; otherwise say it is a mean.
        named = ", ".join(
            (
                f"{el} in {round(cn)}-fold coordination"
                if abs(cn - round(cn)) < 0.05
                else f"{el} in {cn:.3g}-fold coordination on average across sites"
            )
            for el, cn in list(coord.items())[:4]
        )
        bits.append(named)
    if cf.get("shortest_bond_a") and cf.get("shortest_bond_pair"):
        bits.append(
            f"a shortest {cf['shortest_bond_pair']} bond of "
            f"{cf['shortest_bond_a']:.4g} Å"
        )
    return " with ".join(bits)


def view_structure(
    species: str, formula: str, st: dict, cf: dict | None = None
) -> dict:
    """The species' structural identity — a new view of a known species.

    Deliberately does NOT assert a space-group number from mindat: that
    field is a mindat-internal id, not the International Tables number
    (Quartz is ITA 152 and mindat says 89). The true symbol comes from
    the CIF-derived features when present, and is simply omitted when not.
    """
    if not st:
        return {}
    clauses = []
    if st.get("crystal_system"):
        sg = (cf or {}).get("space_group_symbol")
        sys_txt = f"is {st['crystal_system'].lower()}"
        clauses.append(f"{sys_txt} (space group {sg})" if sg else sys_txt)
    cell = _fmt_cell(st)
    if cell:
        clauses.append(f"with unit cell {cell}")
    geom = _fmt_geometry(cf or {})
    if geom:
        clauses.append(f"and has {geom}")
    if not clauses:
        return {}
    tail = []
    if st.get("strunz_class"):
        _art = "an" if st["strunz_class"][0] in "aeiou" else "a"
        tail.append(f"It is classified as {_art} {st['strunz_class']}")
    if st.get("density_calc"):
        tail.append(f"calculated density {st['density_calc']:.4g} g/cm³")
    if st.get("hardness_max"):
        tail.append(f"Mohs hardness up to {st['hardness_max']:.3g}")
    text = f"{species} ({formula}) {' '.join(clauses)}."
    if tail:
        text += " " + ", ".join(tail) + "."
    return {
        "view": "structure",
        "species": species,
        "text": text,
        "provenance": {
            "source": "mindat" + ("+AMCSD" if cf else ""),
            "species": species,
        },
    }


def view_polymorph(formula: str, members: list[dict]) -> dict:
    """Same formula, different structure, different spectrum.

    This is the contrastive view with the REASON attached. The plain
    contrastive view already told the model that these species differ
    spectrally; what it could not say is why, and 'why' is the only part
    that generalises to a polymorph pair the model has not seen.
    """
    usable = [
        m for m in members if m.get("peaks") and (m.get("structure") or m.get("cif"))
    ]
    if len(usable) < 2:
        return {}
    # Require that the structures actually DIFFER. Two entries of the same
    # mineral under variant names would otherwise be presented as a
    # contrast, teaching a distinction that does not exist.
    sigs = {
        (
            (m.get("structure") or {}).get("crystal_system"),
            (m.get("cif") or {}).get("space_group_symbol"),
        )
        for m in usable
    }
    if len(sigs) < 2:
        return {}
    lines = []
    for m in usable:
        st, cf = m.get("structure") or {}, m.get("cif") or {}
        desc = []
        if st.get("crystal_system"):
            sg = cf.get("space_group_symbol")
            desc.append(
                f"{st['crystal_system'].lower()}"
                + (f", space group {sg}" if sg else "")
            )
        geom = _fmt_geometry(cf)
        if geom:
            desc.append(geom)
        lines.append(
            f"{m['species']} is {'; '.join(desc)}, and shows bands at "
            f"{_fmt_peaks(m['peaks'][:4])}"
        )
    names = ", ".join(m["species"] for m in usable)
    return {
        "view": "polymorph",
        "species": sorted(m["species"] for m in usable),
        "text": (
            f"{names} all have the composition {formula}, so no chemical "
            "analysis can separate them. They are different structures, and "
            "the Raman spectrum follows the structure rather than the "
            "composition: " + ". ".join(lines) + "."
        ),
        "provenance": {"source": "polymorph", "formula": formula},
    }


def view_computed(rec: dict) -> dict:
    """Ab-initio Raman modes with their irreducible representations.

    The symmetry label is the part worth having: it names WHICH motion
    produces the band, which is the link between a structure and a
    spectrum that no amount of composition data supplies.
    """
    bands = [b for b in (rec.get("bands") or []) if b.get("intensity_rel")]
    if not bands:
        return {}
    # Degenerate modes (E, T) appear once per component at the SAME
    # frequency and label. Listing "364 cm-1 (Eg), 364 cm-1 (Eg)" reads as
    # two bands where the spectrum shows one.
    seen, uniq = set(), []
    for b in sorted(bands, key=lambda b: -b["intensity_rel"]):
        key = (round(b["position_cm-1"], 1), b.get("irrep") or "")
        if key in seen:
            continue
        seen.add(key)
        uniq.append(b)
    strong = uniq[:6]
    strong.sort(key=lambda b: b["position_cm-1"])
    parts = []
    for b in strong:
        lab = f" ({b['irrep']})" if b.get("irrep") else ""
        parts.append(f"{b['position_cm-1']:g} cm⁻¹{lab}")
    sg = rec.get("space_group_symbol")
    where = f", space group {sg}," if sg else ""
    return {
        "view": "computed",
        "species": rec.get("species", ""),
        "text": (
            f"A first-principles calculation for {rec.get('species','')} "
            f"({rec.get('formula','')}){where} predicts Raman-active modes at "
            + ", ".join(parts)
            + f". The strongest is {max(strong, key=lambda b: b['intensity_rel'])['position_cm-1']:g}"
            " cm⁻¹. Calculated frequencies are systematically offset from "
            "measured ones and should be read as the pattern of a spectrum, not "
            "its exact positions."
        ),
        "provenance": {
            "source": "WURM",
            "wurm_id": rec.get("wurm_id"),
            "n_modes": len(rec.get("bands") or []),
        },
    }


# ---------------------------------------------------------------------------
# LIBS VIEWS
#
# The existing LIBS record is a flat wavelength list — "Si at 190.13,
# 288.16 nm" — which does not say which line is strong, and strength is
# most of what makes a LIBS spectrum usable. The collected literature
# cannot supply it: of 126 LIBS papers in the databank, essentially none
# carry intensity tables, only acquisition metadata.
#
# It is computable instead. NIST ASD gives the transition probability,
# upper-level energy and degeneracy for 31,201 lines, and for an
# optically thin plasma in LTE the relative intensity within one species
# is (g_k A_ki / lambda) exp(-E_k / kT). That is a derivation from
# catalogued constants, not an estimate from a fitted correlation.
# ---------------------------------------------------------------------------


def _fmt_libs_group(group: dict) -> str:
    parts = [
        f"{l['wavelength_nm_air']:.2f} nm ({l['relative_intensity']:g})"
        + ("*" if l.get("wavelength_basis") == "ritz" else "")
        for l in group["lines"]
    ]
    return f"{group['stage_label']} at " + ", ".join(parts)


def view_libs_predicted(
    species: str, formula: str, groups: list[dict], temperature_k: float
) -> dict:
    """Predicted LIBS lines with relative intensities at a stated T."""
    if not groups:
        return {}
    has_ritz = any(
        l.get("wavelength_basis") == "ritz" for g in groups for l in g["lines"]
    )
    body = "; ".join(_fmt_libs_group(g) for g in groups)
    text = (
        f"In a LIBS measurement of {species} ({formula}) with the plasma near "
        f"{temperature_k:,.0f} K, the strongest expected emission lines are {body}. "
        "Numbers in brackets are relative intensities computed from the catalogued "
        "transition probability, upper-level energy and level degeneracy; they are "
        "comparable WITHIN each element only, since ranking one element against "
        "another additionally requires the partition functions and the ionisation "
        "balance. Wavelengths are air values above 200 nm, per the NIST convention."
    )
    if has_ritz:
        text += (
            " Lines marked * have a calculated (Ritz) wavelength rather than "
            "an observed one."
        )
    return {
        "view": "libs_predicted",
        "species": species,
        "text": text,
        "provenance": {
            "source": "NIST ASD",
            "derivation": "boltzmann_lte",
            "temperature_k": temperature_k,
        },
    }


def view_libs_temperature(
    species: str,
    formula: str,
    cool: list[dict],
    hot: list[dict],
    t_cool: float,
    t_hot: float,
) -> dict:
    """How the SAME mineral's LIBS spectrum changes with plasma temperature.

    This is the part a flat wavelength list cannot teach. Plasma
    temperature is a property of the measurement, not of the mineral, so
    two spectra of one sample legitimately disagree on relative
    intensities. Stating that explicitly is the same reported-vs-reference
    lesson the corroboration views carry, applied to a nuisance variable.
    """
    if not cool or not hot:
        return {}

    # Only worth emitting where the ranking actually moves.
    def top(gs):
        return [(g["stage_label"], g["lines"][0]["wavelength_nm_air"]) for g in gs]

    if top(cool) == top(hot):
        moved = False
        for a, b in zip(cool, hot):
            if [round(l["relative_intensity"]) for l in a["lines"]] != [
                round(l["relative_intensity"]) for l in b["lines"]
            ]:
                moved = True
                break
        if not moved:
            return {}
    return {
        "view": "libs_temperature",
        "species": species,
        "text": (
            f"Plasma temperature changes which {species} ({formula}) lines dominate a "
            f"LIBS spectrum, without anything about the mineral changing. At "
            f"{t_cool:,.0f} K the strongest lines are "
            + "; ".join(_fmt_libs_group(g) for g in cool)
            + f". At {t_hot:,.0f} K they are "
            + "; ".join(_fmt_libs_group(g) for g in hot)
            + ". Transitions from higher upper levels gain relative strength as the "
            "plasma gets hotter, because the Boltzmann population of those levels "
            "rises. Two LIBS spectra of the same sample can therefore disagree on "
            "relative intensities without either being wrong."
        ),
        "provenance": {
            "source": "NIST ASD",
            "derivation": "boltzmann_lte",
            "temperature_k": [t_cool, t_hot],
        },
    }
