"""Frame library: many surface forms for one fact (root venv).

WHY. §17/§18: 3,318 structure views rendered from ONE frame taught a 1B
model the frame — it filled the slots with whatever was frequent and its
name-conditioned representation got WORSE than the base model's. The
assembler's `PHRASINGS` field was meant to vary the surface and was never
rendered (400 inverse records, 11 distinct openings). This module is the
fix: every fact kind has >=10 statement frames and >=4 question frames that
differ in STRUCTURE (subject-first, spectrum-first, passive, catalogue voice,
comparison, list-then-name …), not in a synonym. Frame 0 of each kind is the
v3 wording, so v3 text is a subset of what v4 can render.

ONE FRAME, TWO USES. A frame is a (prompt, completion) pair. Stage 1 trains
on `prompt + completion` as prose. Stage 2 trains on the completion given
the prompt, so the split sits right before the fact payload: the prompt is
the lead-in that names the subject, the completion carries the numbers and
the provenance phrase. Question frames are the same pair with a question
as the prompt.

PROVENANCE IS NEVER SMOOTHED. Every frame that states a derived position
carries `{how}` — "peak-picked from the RRUFF spectrum", "read as
absorption minima from the ECOSTRESS spectrum" — reusing the interconnect
module's derivation phrases. Numbers are formatted once in `fields()` and
copied verbatim into every frame; a test asserts that every band string
survives into the rendered completion.

PROBE FRAMES are held out of training entirely (one per kind). The
recall probes score the model on trained frames AND probe frames; the gap
between them is the template-collapse metric pre-registered in §19.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from interconnect import _derivation_phrase, _tol_phrase


@dataclass(frozen=True)
class Frame:
    prompt: str
    completion: str

    def render(self, f: dict) -> dict:
        p = self.prompt.format(**f)
        c = self.completion.format(**f)
        return {"prompt": p, "completion": c, "text": p + c}


def _num(x) -> str:
    return f"{float(x):g}"


def _join(vals, unit: str) -> str:
    return ", ".join(_num(v) for v in vals) + (f" {unit}" if unit else "")


def _and(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _cell(cell: dict) -> str:
    parts = [f"{k} = {cell[k]:.4g} Å" for k in ("a", "b", "c") if cell.get(k)]
    greek = {"alpha": "α", "beta": "β", "gamma": "γ"}
    parts += [
        f"{greek[k]} = {cell[k]:.4g}°"
        for k in ("alpha", "beta", "gamma")
        if cell.get(k) and abs(cell[k] - 90) > 1e-6
    ]
    return ", ".join(parts)


def _geometry(p: dict) -> str:
    bits = []
    coord = p.get("coordination") or {}
    if coord:
        bits.append(
            ", ".join(
                (
                    f"{el} in {round(cn)}-fold coordination"
                    if abs(cn - round(cn)) < 0.05
                    else f"{el} in {cn:.3g}-fold coordination on average across sites"
                )
                for el, cn in list(coord.items())[:4]
            )
        )
    if p.get("shortest_bond_a") and p.get("shortest_bond_pair"):
        bits.append(
            f"a shortest {p['shortest_bond_pair']} bond of {p['shortest_bond_a']:.4g} Å"
        )
    return " with ".join(bits)


def _libs_body(groups: list[dict]) -> str:
    out = []
    for g in groups:
        parts = [
            f"{ln['nm']:.2f} nm ({ln['rel']:g})" + ("*" if ln.get("ritz") else "")
            for ln in g["lines"]
        ]
        out.append(f"{g['stage_label']} at " + ", ".join(parts))
    return "; ".join(out)


# Slots the SYNTHETIC corpus adds on top of fields(): instrument variance
# (filled by synth_variance at render time), fact-graph relations and sample
# provenance (filled by synth_render). name -> (type, description, example).
# fields() gives every one of them an empty default so v4 frames still render
# unchanged; the spec exporter (synth_export.py) reads this table verbatim.
SYNTH_SLOTS: dict[str, tuple[str, str, str]] = {
    "peak_list": (
        "number-list",
        "measured peak list: position cm-1 (relative intensity), strongest listed = 1.00, position-sorted",
        "128.0 (0.31), 206.3 (0.55), 464.8 (1.00)",
    ),
    "band_strongest": ("number", "position of the strongest band, cm-1", "464.8"),
    "instrument_class": (
        "text",
        "instrument class: lab, portable or handheld (Raman); broadband or windowed (LIBS)",
        "portable",
    ),
    "laser_nm": ("number", "excitation wavelength in nm, bare number", "785"),
    "tol": ("phrase", "position tolerance phrase", "±3 cm-1"),
    "calibration": (
        "phrase",
        "how the unit was calibrated",
        "the 520.5 cm-1 silicon line",
    ),
    "window": ("phrase", "spectral window covered", "150–3200 cm-1"),
    "libs_T": ("number", "LIBS plasma temperature in K, formatted", "10,000"),
    "resolution": ("phrase", "LIBS spectral resolution", "0.1 nm"),
    "libs_lines": (
        "number-list",
        "LIBS emission lines by ionisation stage: nm (relative intensity)",
        "Fe I at 371.99 nm (100), 438.35 nm (62); Ca II at 393.37 nm (90)",
    ),
    "libs_top3": (
        "number-list",
        "the three strongest LIBS lines, nm",
        "371.99, 393.37, 438.35 nm",
    ),
    "locality": ("text", "sample locality as catalogued", "Minas Gerais, Brazil"),
    "also": (
        "phrase",
        "leading-space phrase naming other species consistent with the list, or empty",
        " (also consistent with Coesite)",
    ),
    "group": ("text", "the mineral's anion/Strunz class", "silicate"),
    "neighbours": (
        "text",
        "related species named from the fact graph",
        "Coesite and Cristobalite",
    ),
    "polymorphs": (
        "text",
        "the other polymorphs of the same composition",
        "Anatase and Brookite",
    ),
    "paper_title": (
        "text",
        "title of a corpus paper that measured this species",
        "Raman spectroscopy of quartz inclusions in garnet",
    ),
}


def _peak_list(bands: list, rel: list) -> str:
    if rel and len(rel) == len(bands):
        pairs = sorted(zip(bands, rel), key=lambda br: br[0])
        return ", ".join(f"{float(b):.1f} ({float(r):.2f})" for b, r in pairs)
    return ", ".join(f"{float(b):.1f}" for b in sorted(bands))


def fields(fact) -> dict:
    """The formatted slots every frame of this kind may use."""
    p = fact.payload
    sp = fact.species if isinstance(fact.species, str) else _and(list(fact.species))
    f: dict = {
        "species": sp,
        "formula": fact.formula,
        "sp_f": f"{sp} ({fact.formula})" if fact.formula else sp,
    }
    k = fact.kind
    if k in ("raman_bands", "inverse"):
        bands = p["bands_cm1"]
        f.update(
            tech=p.get("tech", "Raman"),
            bands=_join(bands, "cm-1"),
            top4=_join(bands[:4], "cm-1"),
            top3=_join(bands[:3], "cm-1"),
            first=_num(bands[0]),
            n=len(bands),
            how=_derivation_phrase(fact.provenance),
            sample=p.get("sample_id", ""),
            sample_paren=f" ({p['sample_id']})" if p.get("sample_id") else "",
            laser=f" at {p['laser_nm']} nm excitation" if p.get("laser_nm") else "",
            archive=fact.provenance.get("source", "the reference"),
        )
        rel = p.get("rel") or []
        f["peak_list"] = _peak_list(bands, rel)
        f["band_strongest"] = _num(
            max(zip(bands, rel), key=lambda br: br[1])[0]
            if rel and len(rel) == len(bands)
            else bands[0]
        )
        f["laser_nm"] = str(p.get("laser_nm") or "")
        f["locality"] = str(p.get("locality") or "")
    elif k == "ir_troughs":
        pos = p["troughs_um"]
        f.update(
            tech=p["tech"],
            bands=_join(pos, "um"),
            top4=_join(pos[:4], "um"),
            first=_num(pos[0]),
            n=len(pos),
            how=_derivation_phrase(fact.provenance),
            sample=p.get("sample_id", ""),
            sample_paren=f" ({p['sample_id']})" if p.get("sample_id") else "",
            archive="ECOSTRESS",
        )
    elif k == "cross_modal":
        mods = p["modalities"]
        f.update(
            parts="; ".join(
                f"{t} at {_join(v['positions'], v['unit'])}" for t, v in mods.items()
            ),
            n_tech=len(mods),
            techs=_and(list(mods)),
        )
    elif k in ("contrastive", "polymorph"):
        mem = p["members"]
        f.update(
            names=_and([m["species"] for m in mem]),
            names_comma=", ".join(m["species"] for m in mem),
            n=len(mem),
        )
        if k == "contrastive":
            f["lines"] = "; ".join(
                f"{m['species']}: {_join(m['bands_cm1'], 'cm-1')}" for m in mem
            )
        else:
            lines = []
            for m in mem:
                d = (m.get("crystal_system") or "").lower()
                if m.get("space_group"):
                    d += f", space group {m['space_group']}"
                lines.append(
                    f"{m['species']} is {d}, and shows bands at {_join(m['bands_cm1'], 'cm-1')}"
                )
            f["lines"] = ". ".join(lines)
    elif k == "structure":
        sysl = (p.get("crystal_system") or "").lower()
        sg = p.get("space_group")
        f.update(
            system=sysl,
            sys_sg=f"{sysl} (space group {sg})" if sg else sysl,
            sg=sg or "",
            cell=_cell(p.get("cell") or {}),
            geometry=_geometry(p),
            strunz=p.get("strunz_class") or "",
            strunz_art=("an" if (p.get("strunz_class") or "x")[0] in "aeiou" else "a"),
            density=f"{p['density_calc']:.4g}" if p.get("density_calc") else "",
            hardness=f"{p['hardness_max']:.3g}" if p.get("hardness_max") else "",
        )
        tail = []
        if f["strunz"]:
            tail.append(f"It is classified as {f['strunz_art']} {f['strunz']}")
        if f["density"]:
            tail.append(f"calculated density {f['density']} g/cm³")
        if f["hardness"]:
            tail.append(f"Mohs hardness up to {f['hardness']}")
        f["tail"] = (" " + ", ".join(tail) + ".") if tail else ""
        clauses = [f"is {f['sys_sg']}"] if sysl else []
        if f["cell"]:
            clauses.append(f"with unit cell {f['cell']}")
        if f["geometry"]:
            clauses.append(f"and has {f['geometry']}")
        f["clauses"] = " ".join(clauses)
    elif k == "computed":
        f.update(
            modes=", ".join(
                f"{m['cm1']:g} cm⁻¹" + (f" ({m['irrep']})" if m.get("irrep") else "")
                for m in p["modes"]
            ),
            strongest=_num(p["strongest_cm1"]),
            where=f", space group {p['space_group']}," if p.get("space_group") else "",
            n_modes=p.get("n_modes", 0),
        )
    elif k == "libs_lines":
        f.update(
            T=f"{p['temperature_k']:,.0f}",
            body=_libs_body(p["groups"]),
            ritz_note=(
                " Lines marked * have a calculated (Ritz) wavelength rather than an observed one."
                if any(ln.get("ritz") for g in p["groups"] for ln in g["lines"])
                else ""
            ),
        )
    elif k == "libs_temperature":
        f.update(
            t_cool=f"{p['t_cool']:,.0f}",
            t_hot=f"{p['t_hot']:,.0f}",
            cool=_libs_body(p["cool"]),
            hot=_libs_body(p["hot"]),
        )
    elif k == "corroboration":
        ref_desc = _derivation_phrase(fact.provenance.get("reference") or {})
        if p.get("ref_sample"):
            ref_desc += f" of {p['ref_sample']}"
        f.update(
            citation=p.get("citation") or "a study in this corpus",
            tech=p.get("tech", "Raman"),
            reported=_num(p["reported"]),
            reference=_num(p["reference"]),
            tol=_tol_phrase(p["reported"], p["reference"]),
            ref_desc=ref_desc,
        )
    elif k == "ice_bandlist":
        f.update(
            phase=p["phase"],
            desc=", ".join(p.get("classification") or []) or "molecular solid",
            rng=(
                f"{p['range_cm1'][0]}-{p['range_cm1'][1]} cm-1"
                if p.get("range_cm1")
                else "its catalogued range"
            ),
            band=f" in the {p['waveband']}" if p.get("waveband") else "",
            temp=(
                f", measured at {p['temperature_k']} K"
                if p.get("temperature_k")
                else ""
            ),
        )
    elif k == "pack_fact":
        f.update(title=p["title"], key=p["key"], value=p["value"])
    for name in SYNTH_SLOTS:
        f.setdefault(name, "")
    return f


# ── frames ───────────────────────────────────────────────────────────
S = Frame  # statement frames: prompt = lead-in, completion = payload
FRAMES: dict[str, dict[str, list[Frame]]] = {
    "formula": {
        "statements": [
            S("{species} has the formula", " {formula}."),
            S("The IMA formula of {species} is", " {formula}."),
            S("Chemically, {species} is", " {formula}."),
            S("{species}:", " {formula}."),
            S("Mineral: {species}. Formula:", " {formula}."),
            S("The composition of {species} is written", " {formula}."),
            S("{species} is the mineral with composition", " {formula}."),
            S("In the IMA list, {species} is given as", " {formula}."),
            S("Formula of {species} —", " {formula}."),
            S("The species {species} corresponds to", " {formula}."),
            S(
                "{species}, an IMA-approved mineral, has the ideal composition",
                " {formula}.",
            ),
        ],
        "questions": [
            S("Q: What is the chemical formula of {species}?\nA:", " {formula}."),
            S("Which formula belongs to the mineral {species}?", " {formula}."),
            S("Give the IMA formula for {species}.", " {formula}."),
            S("Question: {species} — composition?\nAnswer:", " {formula}."),
            S("The mineral {species} is composed of what?", " {formula}."),
        ],
    },
    "raman_bands": {
        "statements": [
            S("{sp_f} shows {tech} bands at", " {bands}, {how}{sample_paren}."),
            S(
                "The {tech} spectrum of {species} ({formula}){laser} has bands at",
                " {bands}; positions {how}{sample_paren}.",
            ),
            S(
                "{archive} sample {sample}, {species} ({formula}), was measured{laser}; its {tech} bands lie at",
                " {bands} ({how}).",
            ),
            S(
                "Bands at",
                " {bands} — {how}{sample_paren} — make up the {tech} signature of {species} ({formula}).",
            ),
            S(
                "For {species}, {formula}, the {tech} band positions {how}{sample_paren} are",
                " {bands}.",
            ),
            S(
                "A {tech} measurement of {species}{laser} gives",
                " {bands}, {how}{sample_paren}.",
            ),
            S("{species}: {tech} bands", " {bands} ({how}{sample_paren})."),
            S(
                "Recorded in {archive} as {sample}, {species} ({formula}) has {n} {tech} bands in the picked list, at",
                " {bands}; the positions are {how}.",
            ),
            S(
                "The strongest-region {tech} features of {species} ({formula}) sit at",
                " {bands}, {how}{sample_paren}.",
            ),
            S(
                "If you measure {species} ({formula}) by {tech} spectroscopy{laser}, expect bands near",
                " {bands} — {how}{sample_paren}.",
            ),
            S(
                "{tech} band list for {species} ({formula}), {how}{sample_paren}:",
                " {bands}.",
            ),
            S(
                "The mineral {species} ({formula}) is characterised in {tech} spectroscopy by bands at",
                " {bands} ({how}{sample_paren}).",
            ),
        ],
        "questions": [
            S(
                "Q: What {tech} bands does {species} ({formula}) show?\nA:",
                " {bands}, {how}{sample_paren}.",
            ),
            S(
                "Where are the {tech} bands of {species}?",
                " At {bands}, {how}{sample_paren}.",
            ),
            S(
                "List the {tech} band positions for {species} ({formula}).",
                " {bands} ({how}{sample_paren}).",
            ),
            S(
                "Question: {tech} spectrum of {species} — band positions?\nAnswer:",
                " {bands}; {how}{sample_paren}.",
            ),
            S(
                "What does the {tech} spectrum of {sample} ({species}) show?",
                " Bands at {bands}, {how}.",
            ),
        ],
    },
    "inverse": {
        "statements": [
            S(
                "A {tech} spectrum with bands at {top4} is characteristic of",
                " {species}, whose composition is {formula}.",
            ),
            S("{tech} bands at {top4} point to", " {species} ({formula})."),
            S(
                "Bands at {top4} in a {tech} spectrum identify the mineral as",
                " {species}, {formula}.",
            ),
            S(
                "If a {tech} spectrum shows {top4}, the sample is most likely",
                " {species} ({formula}).",
            ),
            S("{tech}: {top4} →", " {species} ({formula})."),
            S(
                "The combination {top4} is the {tech} fingerprint of",
                " {species} ({formula}).",
            ),
            S(
                "A spectroscopist seeing {tech} bands at {top4} would name the phase",
                " {species}, composition {formula}.",
            ),
            S(
                "Matching {tech} bands at {top4} against the reference library returns",
                " {species} ({formula}).",
            ),
            S(
                "Unknown with {tech} bands at {top4}: identified as",
                " {species} ({formula}).",
            ),
            S("The {tech} band set {top4} belongs to", " {species}, {formula}."),
            S("Which mineral has {tech} bands at {top4}?", " {species} ({formula})."),
        ],
        "questions": [
            S(
                "Q: A {tech} spectrum shows bands at {top4}. Which mineral is it?\nA:",
                " {species} ({formula}).",
            ),
            S(
                "Identify the mineral from its {tech} bands: {top4}.",
                " {species}, {formula}.",
            ),
            S(
                "Question: {tech} bands at {top4} — what phase?\nAnswer:",
                " {species} ({formula}).",
            ),
            S("What mineral gives {tech} bands at {top4}?", " {species} ({formula})."),
        ],
    },
    "ir_troughs": {
        "statements": [
            S("{sp_f} shows {tech} bands at", " {bands}, {how}{sample_paren}."),
            S(
                "In {tech} reflectance, {species} ({formula}) has absorption minima at",
                " {bands} ({how}{sample_paren}).",
            ),
            S(
                "The {tech} spectrum of {species} dips at",
                " {bands}; these minima are {how}{sample_paren}.",
            ),
            S(
                "{archive} sample {sample} ({species}, {formula}) shows {tech} absorption features at",
                " {bands}, {how}.",
            ),
            S(
                "Reflectance minima at",
                " {bands} — {how}{sample_paren} — characterise {species} ({formula}) in the {tech}.",
            ),
            S(
                "For {species} ({formula}), the {tech} absorption positions {how}{sample_paren} are",
                " {bands}.",
            ),
            S("{species}: {tech} minima at", " {bands} ({how}{sample_paren})."),
            S(
                "Measured in the {tech}, {species} ({formula}) absorbs at",
                " {bands}, {how}{sample_paren}.",
            ),
            S(
                "The {n} diagnostic {tech} features of {species} ({formula}) are at",
                " {bands}; positions {how}{sample_paren}.",
            ),
            S(
                "Expect {tech} absorption from {species} ({formula}) near",
                " {bands} — {how}{sample_paren}.",
            ),
            S(
                "{tech} feature list for {species} ({formula}), {how}{sample_paren}:",
                " {bands}.",
            ),
        ],
        "questions": [
            S(
                "Q: Where does {species} ({formula}) absorb in the {tech}?\nA:",
                " At {bands}, {how}{sample_paren}.",
            ),
            S(
                "List the {tech} absorption features of {species}.",
                " {bands} ({how}{sample_paren}).",
            ),
            S(
                "Question: {tech} minima of {species}?\nAnswer:",
                " {bands}; {how}{sample_paren}.",
            ),
            S(
                "What are the {tech} band positions of {sample} ({species})?",
                " {bands}, {how}.",
            ),
        ],
    },
    "cross_modal": {
        "statements": [
            S(
                "{sp_f} can be identified across techniques:",
                " {parts}. The same material presents a different signature in each.",
            ),
            S(
                "Across {n_tech} techniques — {techs} — {species} ({formula}) shows",
                " {parts}.",
            ),
            S(
                "The joint signature of {species} ({formula}) is",
                " {parts}; each technique reads a different property of the same material.",
            ),
            S("{species} ({formula}), by technique:", " {parts}."),
            S("Measured by {techs}, {species} gives", " {parts}."),
            S(
                "One mineral, several fingerprints — {species} ({formula}):",
                " {parts}.",
            ),
            S("A multimodal record for {species} ({formula}) reads", " {parts}."),
            S(
                "{species} ({formula}) presents differently in each technique:",
                " {parts}.",
            ),
            S("Combined reference data for {species} ({formula}):", " {parts}."),
            S(
                "To confirm {species} ({formula}) with more than one method, look for",
                " {parts}.",
            ),
        ],
        "questions": [
            S("Q: How does {species} ({formula}) appear in {techs}?\nA:", " {parts}."),
            S("Give the multi-technique signature of {species}.", " {parts}."),
            S("Question: {species} across {techs}?\nAnswer:", " {parts}."),
            S("What do {techs} show for {species} ({formula})?", " {parts}."),
        ],
    },
    "contrastive": {
        "statements": [
            S(
                "{names_comma} share the composition {formula} and cannot be told apart by chemistry alone. Their spectra differ:",
                " {lines}.",
            ),
            S("Same formula, {formula}; different Raman spectra —", " {lines}."),
            S(
                "{names} all have the composition {formula}; the Raman bands separate them:",
                " {lines}.",
            ),
            S(
                "Chemistry alone cannot distinguish {names} ({formula}). Raman can:",
                " {lines}.",
            ),
            S(
                "The {n} minerals of composition {formula} — {names_comma} — are separated by band detail:",
                " {lines}.",
            ),
            S(
                "Composition {formula} is shared by {names}. Their leading Raman bands:",
                " {lines}.",
            ),
            S(
                "Telling {names} apart ({formula}) comes down to the spectrum:",
                " {lines}.",
            ),
            S("{formula} occurs as {names}. Raman signatures:", " {lines}."),
            S(
                "A chemical analysis returning {formula} could be any of {names}; Raman resolves it:",
                " {lines}.",
            ),
            S("Distinguishing {names_comma}, all {formula}:", " {lines}."),
        ],
        "questions": [
            S(
                "Q: How are {names} ({formula}) distinguished spectroscopically?\nA:",
                " {lines}.",
            ),
            S("Which Raman bands separate {names_comma}?", " {lines}."),
            S(
                "Question: {formula} — how to tell {names} apart?\nAnswer:",
                " By their Raman bands: {lines}.",
            ),
            S("Give the Raman bands that discriminate {names}.", " {lines}."),
        ],
    },
    "structure": {
        "statements": [
            S("{sp_f}", " {clauses}.{tail}"),
            S("Structurally, {species} ({formula})", " {clauses}.{tail}"),
            S(
                "The crystal structure of {species} ({formula}):",
                " it {clauses}.{tail}",
            ),
            S(
                "{species} crystallises in the {system} system",
                "{sg_clause}; unit cell {cell}.{tail}",
            ),
            S("Cell data for {species} ({formula}):", " {system}, {cell}.{tail}"),
            S("{species} ({formula}) —", " {clauses}.{tail}"),
            S("In terms of structure, {species}", " {clauses}.{tail}"),
            S("Crystallography of {species} ({formula}):", " {clauses}.{tail}"),
            S(
                "{species} is a {system} mineral ({formula})",
                "{sg_clause} with cell {cell}.{tail}",
            ),
            S("Structure entry — {species}, {formula}:", " {clauses}.{tail}"),
            S("The mineral {species} ({formula})", " {clauses}.{tail}"),
        ],
        "questions": [
            S(
                "Q: What is the crystal system of {species}?\nA:",
                " {species} is {sys_sg}.",
            ),
            S("Give the unit cell of {species} ({formula}).", " {cell}."),
            S(
                "Question: crystal structure of {species}?\nAnswer:",
                " It {clauses}.{tail}",
            ),
            S(
                "Which crystal system does {species} ({formula}) belong to?",
                " The {system} system{sg_clause}.",
            ),
            S(
                "Describe the structure of {species}.",
                " {species} ({formula}) {clauses}.{tail}",
            ),
        ],
    },
    "polymorph": {
        "statements": [
            S(
                "{names_comma} all have the composition {formula}, so no chemical analysis can separate them. They are different structures, and the Raman spectrum follows the structure rather than the composition:",
                " {lines}.",
            ),
            S(
                "{names} are polymorphs of {formula}: one composition, different structures, different spectra.",
                " {lines}.",
            ),
            S(
                "Same composition, {formula}, but the structures differ, and so do the Raman spectra —",
                " {lines}.",
            ),
            S(
                "Because Raman reads bonding geometry rather than stoichiometry, the {formula} polymorphs {names} are distinguishable:",
                " {lines}.",
            ),
            S(
                "The polymorphs {names_comma} share {formula}; their structures and spectra do not:",
                " {lines}.",
            ),
            S(
                "{formula} takes several structures —",
                " {lines}. Composition cannot separate them; the spectrum can.",
            ),
            S("Polymorphism in {formula}: {names}.", " {lines}."),
            S(
                "Structure, not chemistry, tells {names} apart ({formula}):",
                " {lines}.",
            ),
            S(
                "A {formula} sample could be any of {names}. Structure and Raman decide:",
                " {lines}.",
            ),
            S(
                "{names}: identical composition {formula}, distinct structures, distinct Raman bands.",
                " {lines}.",
            ),
        ],
        "questions": [
            S(
                "Q: Why can Raman separate {names} when chemistry cannot?\nA:",
                " They share {formula} but are different structures, and Raman follows structure: {lines}.",
            ),
            S("How do the polymorphs {names_comma} ({formula}) differ?", " {lines}."),
            S(
                "Question: {formula} polymorphs — structures and bands?\nAnswer:",
                " {lines}.",
            ),
            S(
                "Distinguish {names} ({formula}) by structure and Raman spectrum.",
                " {lines}.",
            ),
        ],
    },
    "computed": {
        "statements": [
            S(
                "A first-principles calculation for {species} ({formula}){where} predicts Raman-active modes at",
                " {modes}. The strongest is {strongest} cm⁻¹. Calculated frequencies are systematically offset from measured ones and should be read as the pattern of a spectrum, not its exact positions.",
            ),
            S(
                "Ab-initio Raman modes for {species} ({formula}){where}:",
                " {modes}; strongest {strongest} cm⁻¹. Computed positions are offset from measurement — read the pattern, not the exact values.",
            ),
            S(
                "WURM's calculated Raman spectrum of {species} ({formula}) has its strongest mode at {strongest} cm⁻¹, with modes at",
                " {modes} (computed, so offset from measured positions).",
            ),
            S(
                "For {species} ({formula}){where} theory predicts",
                " {modes} — the strongest at {strongest} cm⁻¹; calculated, not measured.",
            ),
            S(
                "Computed ({n_modes} modes in total), the Raman-active vibrations of {species} ({formula}) include",
                " {modes}, strongest {strongest} cm⁻¹. Expect a systematic offset from experiment.",
            ),
            S(
                "Symmetry-labelled modes of {species} ({formula}){where}:",
                " {modes}. Calculated values; the pattern transfers to measurement, the exact positions do not.",
            ),
            S(
                "The irreducible representations of the strongest computed Raman modes of {species} ({formula}) are given by",
                " {modes}; the most intense is {strongest} cm⁻¹ (calculated).",
            ),
            S(
                "{species} ({formula}), from first principles{where}:",
                " Raman-active modes at {modes}, strongest {strongest} cm⁻¹. Computed frequencies carry a systematic offset.",
            ),
            S(
                "Which motions produce the bands of {species} ({formula})? The calculation assigns",
                " {modes}; the strongest band is the {strongest} cm⁻¹ mode. Calculated, so positions are approximate.",
            ),
            S(
                "Calculated Raman pattern for {species} ({formula}){where} —",
                " {modes}; strongest {strongest} cm⁻¹. Offset from measured positions is expected.",
            ),
        ],
        "questions": [
            S(
                "Q: Which Raman-active modes does theory predict for {species} ({formula})?\nA:",
                " {modes}; the strongest is {strongest} cm⁻¹. These are calculated and offset from measured positions.",
            ),
            S(
                "What symmetry labels do the computed Raman modes of {species} carry?",
                " {modes} (calculated; strongest {strongest} cm⁻¹).",
            ),
            S(
                "Question: strongest calculated Raman mode of {species}?\nAnswer:",
                " {strongest} cm⁻¹, among {modes} — computed, not measured.",
            ),
            S(
                "Give the ab-initio Raman modes of {species} ({formula}).",
                " {modes}; strongest {strongest} cm⁻¹. Calculated positions are systematically offset.",
            ),
        ],
    },
    "libs_lines": {
        "statements": [
            S(
                "In a LIBS measurement of {species} ({formula}) with the plasma near {T} K, the strongest expected emission lines are",
                " {body}. Numbers in brackets are relative intensities computed from the catalogued transition probability, upper-level energy and level degeneracy; they are comparable WITHIN each element only, since ranking one element against another additionally requires the partition functions and the ionisation balance. Wavelengths are air values above 200 nm, per the NIST convention.{ritz_note}",
            ),
            S(
                "LIBS lines for {species} ({formula}) at a plasma temperature of {T} K:",
                " {body}. Bracketed values are relative intensities within each element (Boltzmann, from NIST ASD constants); air wavelengths above 200 nm.{ritz_note}",
            ),
            S(
                "Ablate {species} ({formula}) and, at {T} K, the plasma emits most strongly at",
                " {body} — relative intensities in brackets, within-element only.{ritz_note}",
            ),
            S(
                "Expected LIBS emission from {species} ({formula}), {T} K plasma:",
                " {body}. Intensities (brackets) are derived from NIST transition data and compare lines of the same element only.{ritz_note}",
            ),
            S(
                "The element lines a LIBS spectrum of {species} ({formula}) would show at {T} K are",
                " {body}; relative intensities from the Boltzmann distribution, per element.{ritz_note}",
            ),
            S(
                "{species} ({formula}) — predicted LIBS lines ({T} K):",
                " {body}. Intensities within an element only; air wavelengths.{ritz_note}",
            ),
            S(
                "Under laser-induced breakdown at about {T} K, {species} ({formula}) gives",
                " {body} (bracketed relative intensities are comparable within each element).{ritz_note}",
            ),
            S(
                "Derived from NIST ASD for {species} ({formula}) at {T} K, the dominant emission lines are",
                " {body}.{ritz_note}",
            ),
            S(
                "A LIBS analyst looking at {species} ({formula}) near {T} K should expect",
                " {body}; intensities in brackets rank lines within an element.{ritz_note}",
            ),
            S(
                "Plasma emission of {species} ({formula}), {T} K:",
                " {body}. Within-element relative intensities, NIST air wavelengths.{ritz_note}",
            ),
        ],
        "questions": [
            S(
                "Q: Which emission lines dominate a LIBS spectrum of {species} ({formula}) at {T} K?\nA:",
                " {body}. Bracketed relative intensities compare lines within one element only.{ritz_note}",
            ),
            S(
                "Predict the LIBS lines of {species} at {T} K.",
                " {body} (within-element relative intensities in brackets).{ritz_note}",
            ),
            S(
                "Question: LIBS of {species} ({formula}), plasma {T} K?\nAnswer:",
                " {body}.{ritz_note}",
            ),
            S(
                "What would a LIBS spectrum of {species} show at {T} K?",
                " {body}; intensities are relative within each element.{ritz_note}",
            ),
        ],
    },
    "libs_temperature": {
        "statements": [
            S(
                "Plasma temperature changes which {species} ({formula}) lines dominate a LIBS spectrum, without anything about the mineral changing. At {t_cool} K the strongest lines are",
                " {cool}. At {t_hot} K they are {hot}. Transitions from higher upper levels gain relative strength as the plasma gets hotter, because the Boltzmann population of those levels rises. Two LIBS spectra of the same sample can therefore disagree on relative intensities without either being wrong.",
            ),
            S(
                "The same {species} ({formula}) sample, two plasma temperatures:",
                " at {t_cool} K, {cool}; at {t_hot} K, {hot}. Hotter plasmas populate higher upper levels, so those transitions strengthen — a nuisance variable, not a property of the mineral.",
            ),
            S(
                "Why do two LIBS spectra of {species} ({formula}) disagree on line intensities?",
                " Plasma temperature. At {t_cool} K: {cool}. At {t_hot} K: {hot}. Higher-lying transitions gain as the Boltzmann population of their upper levels rises.",
            ),
            S(
                "LIBS of {species} ({formula}) at {t_cool} K gives",
                " {cool}, but at {t_hot} K the ranking becomes {hot}. Temperature, not composition, moved the intensities.",
            ),
            S(
                "Temperature dependence of the {species} ({formula}) LIBS spectrum —",
                " {t_cool} K: {cool}. {t_hot} K: {hot}. Lines from higher upper levels strengthen with temperature.",
            ),
            S(
                "For {species} ({formula}), the dominant LIBS lines shift with plasma temperature:",
                " {cool} at {t_cool} K versus {hot} at {t_hot} K. Neither spectrum is wrong; the Boltzmann populations differ.",
            ),
            S(
                "A cool plasma ({t_cool} K) on {species} ({formula}) shows",
                " {cool}; a hot one ({t_hot} K) shows {hot}. The mineral is unchanged; the upper-level populations are not.",
            ),
            S(
                "Relative LIBS intensities of {species} ({formula}) are a property of the plasma as much as the mineral:",
                " {t_cool} K → {cool}; {t_hot} K → {hot}.",
            ),
            S(
                "Compare {species} ({formula}) at two plasma temperatures.",
                " {t_cool} K: {cool}. {t_hot} K: {hot}. Higher-energy upper levels gain relative strength as temperature rises.",
            ),
            S(
                "{species} ({formula}), LIBS, {t_cool} K then {t_hot} K:",
                " {cool} — then {hot}. The change is Boltzmann statistics, not chemistry.",
            ),
        ],
        "questions": [
            S(
                "Q: How does plasma temperature change the LIBS spectrum of {species} ({formula})?\nA:",
                " At {t_cool} K the strongest lines are {cool}; at {t_hot} K they are {hot}. Higher upper levels populate as the plasma heats, so those transitions gain relative strength.",
            ),
            S(
                "Why might two LIBS spectra of {species} differ in relative intensities?",
                " Plasma temperature: {t_cool} K gives {cool}, {t_hot} K gives {hot}. Neither is wrong.",
            ),
            S(
                "Question: {species} ({formula}) LIBS lines at {t_cool} K and {t_hot} K?\nAnswer:",
                " {cool}; then {hot}.",
            ),
            S(
                "What happens to the LIBS lines of {species} as the plasma heats from {t_cool} K to {t_hot} K?",
                " {cool} becomes {hot}; higher-lying transitions strengthen with temperature.",
            ),
        ],
    },
    "corroboration": {
        "statements": [
            S(
                "For {species}, {citation} reports a {tech} band at {reported}. The reference position is",
                " {reference}, {ref_desc} — the reported value {tol}.",
            ),
            S(
                "{citation} gives a {tech} band of {species} at {reported}; the reference library places it at",
                " {reference} ({ref_desc}), so the reported value is {tol}.",
            ),
            S(
                "Reported versus reference for {species}, {tech}: the paper says {reported}, the reference {ref_desc} says",
                " {reference} — {tol}.",
            ),
            S(
                "A {tech} band of {species} reported at {reported} in {citation} sits against a reference value of",
                " {reference}, {ref_desc}; the reported value {tol}.",
            ),
            S(
                "Checking {citation} against the reference: {species}, {tech}, reported {reported}, reference",
                " {reference} ({ref_desc}) — {tol}.",
            ),
            S(
                "{species}: the literature value {reported} ({tech}, {citation}) compares with the reference",
                " {reference}, {ref_desc}; it is {tol}.",
            ),
            S(
                "How well does {citation} agree with the reference on {species}? Its {tech} band at {reported} versus",
                " {reference} ({ref_desc}): {tol}.",
            ),
            S(
                "The {tech} band {citation} reports for {species} at {reported} corresponds to the reference band at",
                " {reference}, {ref_desc}; {tol}.",
            ),
            S(
                "Corroboration for {species} ({tech}): paper {reported}, reference",
                " {reference} — {ref_desc}. The reported value {tol}.",
            ),
            S(
                "In {citation}, {species} shows a {tech} band at {reported}; the reference {ref_desc} has it at",
                " {reference}. The reported value {tol}.",
            ),
        ],
        "questions": [
            S(
                "Q: {citation} reports a {tech} band of {species} at {reported}. Does the reference agree?\nA:",
                " The reference position is {reference}, {ref_desc}; the reported value {tol}.",
            ),
            S(
                "Compare the reported {tech} band of {species} at {reported} with the reference.",
                " Reference {reference} ({ref_desc}); {tol}.",
            ),
            S(
                "Question: is {reported} a reasonable {tech} band for {species}?\nAnswer:",
                " The reference {ref_desc} gives {reference}; the reported value {tol}.",
            ),
            S(
                "Where does the reference put the {species} {tech} band that {citation} reports at {reported}?",
                " At {reference}, {ref_desc} — {tol}.",
            ),
        ],
    },
    "ice_bandlist": {
        "statements": [
            S(
                "{phase} is a {desc} whose absorption band list is catalogued by SSHADE over",
                " {rng}{band}{temp}.",
            ),
            S(
                "SSHADE catalogues the absorption bands of {phase} ({desc}) across",
                " {rng}{band}{temp}.",
            ),
            S(
                "For the {desc} {phase}, SSHADE's band list covers",
                " {rng}{band}{temp}.",
            ),
            S(
                "{phase}, a {desc}, has catalogued absorption bands spanning",
                " {rng}{band}{temp}.",
            ),
            S("The SSHADE entry for {phase} ({desc}) spans", " {rng}{band}{temp}."),
            S("Absorption band catalogue — {phase} ({desc}):", " {rng}{band}{temp}."),
            S(
                "As a {desc}, {phase} is catalogued in SSHADE with bands over",
                " {rng}{band}{temp}.",
            ),
            S(
                "Band list coverage for {phase} ({desc}, SSHADE):",
                " {rng}{band}{temp}.",
            ),
            S("{phase}: {desc}; SSHADE band list", " {rng}{band}{temp}."),
            S(
                "In the SSHADE database, the {desc} {phase} is listed with absorption bands over",
                " {rng}{band}{temp}.",
            ),
        ],
        "questions": [
            S(
                "Q: Over what range does SSHADE catalogue the absorption bands of {phase}?\nA:",
                " {rng}{band}{temp}; it is a {desc}.",
            ),
            S(
                "What kind of material is {phase} in SSHADE, and what range is catalogued?",
                " A {desc}, with bands over {rng}{band}{temp}.",
            ),
            S(
                "Question: SSHADE band list for {phase}?\nAnswer:",
                " {rng}{band}{temp} ({desc}).",
            ),
            S("Give the SSHADE band-list coverage of {phase}.", " {rng}{band}{temp}."),
        ],
    },
    "pack_fact": {
        "statements": [
            S('In "{title}", the {key} is reported as', " {value}."),
            S('"{title}" reports {key}:', " {value}."),
            S('According to "{title}", {key} =', " {value}."),
            S('The study "{title}" gives the {key} as', " {value}."),
            S('{key}, as reported in "{title}":', " {value}."),
            S('From "{title}": {key} —', " {value}."),
            S('"{title}" — {key}:', " {value}."),
            S('The paper "{title}" states a {key} of', " {value}."),
            S('Reported {key} in "{title}":', " {value}."),
            S('For the work "{title}", the packed {key} reads', " {value}."),
        ],
        "questions": [
            S('Q: What {key} does "{title}" report?\nA:', " {value}."),
            S('In "{title}", what is the {key}?', " {value}."),
            S('Question: {key} in "{title}"?\nAnswer:', " {value}."),
            S('What value of {key} appears in "{title}"?', " {value}."),
        ],
    },
}

#: Held out of training entirely; the probes render these.
PROBE_FRAMES: dict[str, Frame] = {
    "formula": S("Reference entry. Species: {species}. Ideal formula:", " {formula}."),
    "raman_bands": S(
        "Reference {tech} peak positions for {species} ({formula}), {how}{sample_paren}:",
        " {bands}.",
    ),
    "inverse": S("Library match for {tech} bands {top4}:", " {species} ({formula})."),
    "ir_troughs": S(
        "Reference {tech} absorption positions for {species} ({formula}), {how}{sample_paren}:",
        " {bands}.",
    ),
    "cross_modal": S(
        "Multi-technique reference card, {species} ({formula}):", " {parts}."
    ),
    "contrastive": S(
        "Discrimination table for composition {formula} ({names_comma}):", " {lines}."
    ),
    "structure": S("Structural card — {species} ({formula}):", " {clauses}.{tail}"),
    "polymorph": S("Polymorph card for {formula} ({names_comma}):", " {lines}."),
    "computed": S(
        "Calculated-mode card for {species} ({formula}):",
        " {modes}; strongest {strongest} cm⁻¹ (computed, offset from measurement).",
    ),
    "libs_lines": S(
        "LIBS reference card, {species} ({formula}), {T} K:", " {body}.{ritz_note}"
    ),
    "libs_temperature": S(
        "LIBS temperature card, {species} ({formula}):",
        " {t_cool} K → {cool}; {t_hot} K → {hot}.",
    ),
    "corroboration": S(
        "Corroboration card — {species}, {tech}, reported {reported} ({citation}); reference:",
        " {reference}, {ref_desc}; {tol}.",
    ),
    "ice_bandlist": S("SSHADE card — {phase} ({desc}):", " {rng}{band}{temp}."),
    "pack_fact": S('Pack card — "{title}" — {key}:', " {value}."),
}

MIN_STATEMENTS = 10
MIN_QUESTIONS = 4


def frames_for(kind: str, questions: bool = True) -> list[Frame]:
    fr = FRAMES[kind]
    return list(fr["statements"]) + (list(fr["questions"]) if questions else [])


def _rank(fact_id: str, n: int, salt: str) -> list[int]:
    """A deterministic permutation of frame indices for this fact."""
    return sorted(
        range(n),
        key=lambda i: hashlib.sha256(f"{fact_id}|{salt}|{i}".encode()).hexdigest(),
    )


def pick_frames(
    fact_id: str, kind: str, k: int, *, questions: bool = True, salt: str = "v4"
) -> list[tuple[int, Frame]]:
    """`k` DISTINCT frames for one fact, chosen by stable hash — the same fact
    renders the same way on every build, and never twice through one frame."""
    frames = frames_for(kind, questions)
    order = _rank(fact_id, len(frames), salt)
    return [(i, frames[i]) for i in order[:k]]


def render(fact, frame: Frame) -> dict:
    f = fields(fact)
    if fact.kind == "structure":
        f["sg_clause"] = f" (space group {f['sg']})" if f.get("sg") else ""
    return frame.render(f)


def render_kind(fact, kind: str, frame: Frame) -> dict:
    """Render a fact through another kind's frames (inverse uses raman fields)."""
    f = fields(fact)
    return frame.render(f)
