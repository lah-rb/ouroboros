#!/usr/bin/env python3
"""Write spec/spec.json for the `synth` flow set (root venv).

THE VENV BOUNDARY MADE CONCRETE. The agent (agent/actions/synth_actions.py)
never imports dev/rock_olmo; it reads the JSON this script writes. The spec
carries everything the template generator and its gate need to know about
the facts layer WITHOUT the facts layer:

  kinds[kind].slots            every slot a template of this kind may use --
                               templates.fields(fact) keys plus templates.
                               SYNTH_SLOTS -- typed, described, with one real
                               example expansion each
  kinds[kind].subject_slots    the slots that NAME the subject
  kinds[kind].answer_slots     forward: the value slots; backward: the naming
                               slots (the direction gate reads these)
  kinds[kind].sample_fills     three real fills so the gate's render check
                               runs str.format exactly as the filler will
  kinds[kind].families         the topic-coverage plan (PROCEDURE §21 / the
                               plan's §5.2): direction(s), form(s), target
                               count per cell, a brief, the slot subset the
                               prompt table shows
  forbidden_signatures[kind]   every v4 FRAME and PROBE frame signature: the
                               bank must not re-contain the frames the probes
                               are measured against
  forbidden_openings[kind]     their openings, for the prompt's avoid block
  known_entities               single-word IMA names (lower-cased) so the gate
                               rejects a literal mineral name in a template

    ../../.venv/bin/python synth_export.py --out ~/corpora/ouroboros-synth/spec/spec.json
    ../../.venv/bin/python synth_export.py --dry-run          # tables to stdout
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from agent.actions.synth_gate import signature  # noqa: E402
from templates import FRAMES, PROBE_FRAMES, SYNTH_SLOTS, fields  # noqa: E402

SPEC_VERSION = 1

# Kinds the pilot synthesises. libs_temperature stays out (the variance_note
# family carries plasma temperature through {libs_T}); computed/corroboration/
# ice/pack are outside the Raman-vs-LIBS question.
KIND_DESCRIBE = {
    "formula": "a mineral species and its ideal (IMA) chemical formula",
    "structure": "a species' crystal system, space group, unit cell, class, density and hardness",
    "raman_bands": "a species' Raman band list as measured on one archived sample",
    "libs_lines": "a species' predicted LIBS emission lines by ionisation stage at a plasma temperature",
    "ir_troughs": "a species' reflectance minima (absorption positions) in the infrared",
    "contrastive": "two or more species of the SAME composition told apart by their Raman bands",
    "polymorph": "polymorphs of one composition: structure and bands side by side",
    "cross_modal": "one species seen through two techniques at once",
}

# slot -> (type, description). Slots absent here fall back to ("text", slot).
SLOT_DESCRIBE: dict[str, tuple[str, str]] = {
    "species": ("entity", "the mineral name"),
    "formula": ("text", "the ideal formula, plain textbook spelling"),
    "sp_f": ("entity", "name with formula in parentheses"),
    "tech": ("text", "the technique name"),
    "bands": ("number-list", "band positions with unit, position-sorted"),
    "top4": ("number-list", "the first four listed positions with unit"),
    "top3": ("number-list", "the first three listed positions with unit"),
    "first": ("number", "the first listed position, bare number"),
    "n": ("number", "how many bands are listed"),
    "how": ("phrase", "derivation phrase: how the positions were obtained"),
    "sample": ("text", "the archive sample id, or empty"),
    "sample_paren": ("phrase", "leading-space sample id in parentheses, or empty"),
    "laser": ("phrase", "leading-space excitation phrase, or empty"),
    "archive": ("text", "the archive the measurement comes from"),
    "parts": ("number-list", "per-technique position lists joined by semicolons"),
    "n_tech": ("number", "how many techniques"),
    "techs": ("text", "the technique names joined"),
    "names": ("text", "the member species joined with 'and'"),
    "names_comma": ("text", "the member species, comma-separated"),
    "lines": (
        "number-list",
        "per-member band lists (contrastive) or structure+bands sentences (polymorph)",
    ),
    "system": ("text", "crystal system, lower-case"),
    "sys_sg": ("text", "crystal system with space group in parentheses"),
    "sg": ("text", "space group symbol, or empty"),
    "cell": ("number-list", "unit-cell parameters"),
    "geometry": ("text", "coordination and shortest-bond phrase"),
    "strunz": ("text", "Strunz/anion class, or empty"),
    "strunz_art": ("text", "'a' or 'an' before the class word"),
    "density": ("number", "calculated density, g/cm3"),
    "hardness": ("number", "Mohs hardness (max)"),
    "tail": (
        "phrase",
        "leading-space sentence with class, density, hardness, or empty",
    ),
    "clauses": ("phrase", "the structural clauses: system, cell, geometry"),
    "sg_clause": ("phrase", "leading-space space-group parenthesis, or empty"),
    "T": ("number", "plasma temperature in K, formatted"),
    "body": ("number-list", "LIBS lines by ionisation stage: nm (relative intensity)"),
    "ritz_note": ("phrase", "a note about Ritz wavelengths, or empty"),
}

# Family plan (the plan's §5.2). kinds -> which fact kinds the family renders;
# slots -> the subset the prompt table shows (None = every slot of the kind).
INSTRUMENT_SLOTS = [
    "instrument_class",
    "laser",
    "laser_nm",
    "tol",
    "calibration",
    "window",
]
FAMILIES: dict[str, dict] = {
    "definition": {
        "kinds": ["formula"],
        "directions": ["forward", "backward"],
        "forms": ["statement", "question"],
        "target": 20,
        "brief": "A species and its formula, stated or asked in as many genres as a mineralogist meets them: glossary line, exam item, museum label, referee aside, database row, dialogue.",
        "slots": ["species", "formula", "sp_f"],
    },
    "structure_card": {
        "kinds": ["structure"],
        "directions": ["forward", "backward"],
        "forms": ["statement", "question"],
        "target": 20,
        "brief": "Crystal system, space group, cell, class, density and hardness of one species — a structural card in many voices; backward templates recover the species from its structural description.",
        "slots": [
            "species",
            "sp_f",
            "formula",
            "system",
            "sys_sg",
            "sg",
            "cell",
            "geometry",
            "strunz",
            "strunz_art",
            "density",
            "hardness",
            "tail",
            "clauses",
        ],
    },
    "measurement_report": {
        "kinds": ["raman_bands", "libs_lines"],
        "directions": ["forward"],
        "forms": ["statement"],
        "target": 20,
        "brief": "A MEASUREMENT narrative: an instrument of a stated class, an excitation line or plasma temperature, a calibration, a sample, and then the peak or line list it produced. Field notebook, methods paragraph, instrument log, results sentence, lab report.",
        "slots": [
            "species",
            "sp_f",
            "peak_list",
            "bands",
            "band_strongest",
            "n",
            "how",
            "sample",
            "sample_paren",
            "body",
            "libs_lines",
            "libs_top3",
            "libs_T",
            "resolution",
        ]
        + INSTRUMENT_SLOTS,
    },
    "identification": {
        "kinds": ["raman_bands", "libs_lines"],
        "directions": ["backward"],
        "forms": ["statement", "question"],
        "target": 20,
        "brief": "The SEEKER schema: a peak list (or LIBS line list) from an instrument of unknown make comes first and the species is the answer. Library match, unknown-sample query, quiz, database lookup, a colleague asking what this spectrum is. {also} may follow the answer with other candidates.",
        "slots": [
            "species",
            "sp_f",
            "peak_list",
            "bands",
            "n",
            "also",
            "body",
            "libs_lines",
            "libs_top3",
            "libs_T",
        ]
        + INSTRUMENT_SLOTS,
    },
    "catalogue_entry": {
        "kinds": ["raman_bands", "libs_lines", "ir_troughs"],
        "directions": ["forward", "backward"],
        "forms": ["statement"],
        "target": 15,
        "brief": "Reference-archive voice: a catalogue card, a database record, a table row, an index line — the archive, the sample and the locality beside the positions.",
        "slots": [
            "species",
            "sp_f",
            "formula",
            "tech",
            "bands",
            "peak_list",
            "top3",
            "first",
            "n",
            "how",
            "sample",
            "sample_paren",
            "archive",
            "locality",
            "body",
            "libs_T",
        ],
    },
    "variance_note": {
        "kinds": ["raman_bands", "libs_lines"],
        "directions": ["forward"],
        "forms": ["statement"],
        "target": 15,
        "brief": "How the SAME species looks on different instruments: the archive list ({bands}) beside a measured list ({peak_list}) with the tolerance, excitation, calibration and instrument class that explain the shifts, lost weak bands and cut-offs. For LIBS: positions fixed, intensity ratios moving with plasma temperature, matrix and gating, self-absorbed resonance lines, a spectral window that drops lines.",
        "slots": [
            "species",
            "sp_f",
            "bands",
            "peak_list",
            "band_strongest",
            "n",
            "body",
            "libs_lines",
            "libs_T",
            "resolution",
        ]
        + INSTRUMENT_SLOTS,
    },
    "contrast": {
        "kinds": ["contrastive", "polymorph"],
        "directions": ["forward", "backward"],
        "forms": ["statement", "question"],
        "target": 12,
        "brief": "Two or more species of one composition told apart: what separates them in the spectrum or the structure. Discrimination table, exam question, identification note; backward templates give the lists and ask which is which.",
        "slots": None,
    },
    "qa": {
        "kinds": ["formula", "structure", "raman_bands", "libs_lines"],
        "directions": ["forward", "backward"],
        "forms": ["question"],
        "target": 15,
        "brief": "Short question and answer pairs in many registers: student to teacher, analyst to database, quiz card, viva question, help-desk ticket. The question is the prompt; the completion is the answer alone.",
        "slots": None,
    },
    "tabular": {
        "kinds": ["formula", "structure", "raman_bands", "libs_lines"],
        "directions": ["forward"],
        "forms": ["statement"],
        "target": 12,
        "brief": "Structured renderings: a markdown table row with a header, a key: value list, a JSON-like record, a CSV line, a figure caption with a legend. The prompt is the header/label part, the completion the value cell(s).",
        "slots": None,
    },
    "group_membership": {
        "kinds": ["structure"],
        "directions": ["forward"],
        "forms": ["statement"],
        "target": 12,
        "brief": "Relational: the species placed in its class ({group}) beside related species ({neighbours}) that share the class or crystal system, with its own structural facts.",
        "slots": [
            "species",
            "sp_f",
            "formula",
            "group",
            "neighbours",
            "system",
            "sys_sg",
            "strunz",
            "strunz_art",
            "clauses",
        ],
    },
    "polymorph_family": {
        "kinds": ["polymorph"],
        "directions": ["forward", "backward"],
        "forms": ["statement"],
        "target": 12,
        "brief": "Relational: one composition, several structures — name the family ({names}), give each member's structure and bands ({lines}), or recover the members from the description.",
        "slots": None,
    },
    "band_neighbourhood": {
        "kinds": ["raman_bands"],
        "directions": ["forward", "backward"],
        "forms": ["statement"],
        "target": 12,
        "brief": "Relational: species whose strongest bands sit close together ({neighbours}) and what separates them from this species' full list; backward: given the list and the neighbourhood, which species.",
        "slots": [
            "species",
            "sp_f",
            "bands",
            "peak_list",
            "band_strongest",
            "n",
            "neighbours",
            "how",
        ],
    },
    "cross_instrument": {
        "kinds": ["raman_bands"],
        "directions": ["forward", "backward"],
        "forms": ["statement"],
        "target": 12,
        "brief": "Two instruments in one passage: the species' Raman list ({bands} or {peak_list}) AND its LIBS lines ({libs_lines}, {libs_top3}) — a multi-technique report; backward: both lists given, the species recovered.",
        "slots": [
            "species",
            "sp_f",
            "formula",
            "bands",
            "peak_list",
            "libs_lines",
            "libs_top3",
            "libs_T",
        ]
        + INSTRUMENT_SLOTS,
    },
    "provenance": {
        "kinds": ["raman_bands"],
        "directions": ["forward"],
        "forms": ["statement"],
        "target": 12,
        "brief": "Where the measurement comes from: the archive, the sample id, the locality, a corpus paper that measured the species ({paper_title}) — then the positions. Curatorial voice, acknowledgement line, data-availability statement, specimen label.",
        "slots": [
            "species",
            "sp_f",
            "bands",
            "peak_list",
            "how",
            "sample",
            "sample_paren",
            "archive",
            "locality",
            "paper_title",
        ],
    },
}

SUBJECT_SLOTS = {
    "default": ["species", "sp_f"],
    "contrastive": ["names", "names_comma", "species", "sp_f", "formula"],
    "polymorph": ["names", "names_comma", "species", "sp_f", "formula"],
}
FORWARD_ANSWERS = {
    "formula": ["formula"],
    "structure": [
        "clauses",
        "system",
        "sys_sg",
        "cell",
        "tail",
        "strunz",
        "density",
        "hardness",
        "sg",
        "geometry",
    ],
    "raman_bands": ["bands", "peak_list", "top3", "top4", "first", "band_strongest"],
    "libs_lines": ["body", "libs_lines", "libs_top3"],
    "ir_troughs": ["bands", "top4", "first"],
    "contrastive": ["lines"],
    "polymorph": ["lines"],
    "cross_modal": ["parts"],
}
# element-metal IMA names that are ordinary chemistry words: not entities here
ENTITY_STOP = {
    "copper",
    "silicon",
    "titanium",
    "silver",
    "gold",
    "iron",
    "nickel",
    "zinc",
    "lead",
    "tin",
    "diamond",
    "graphite",
    "sulphur",
    "sulfur",
    "carbon",
    "aluminium",
    "aluminum",
    "chromium",
    "platinum",
    "arsenic",
    "antimony",
    "bismuth",
    "mercury",
    "cobalt",
    "manganese",
    "selenium",
    "tellurium",
    "perovskite",
    "ice",
    "salt",
    "ruby",
    "jade",
}


def _git() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def kind_frames(kind: str):
    frames = list(FRAMES.get(kind, {}).get("statements", [])) + list(
        FRAMES.get(kind, {}).get("questions", [])
    )
    if kind in PROBE_FRAMES:
        frames.append(PROBE_FRAMES[kind])
    if kind == "raman_bands":  # the inverse frames render raman facts
        frames += list(FRAMES["inverse"]["statements"]) + list(
            FRAMES["inverse"]["questions"]
        )
        frames.append(PROBE_FRAMES["inverse"])
    return frames


def forbidden(kind: str) -> tuple[list[str], list[str]]:
    sigs, openings = [], []
    for fr in kind_frames(kind):
        sigs.append(signature(fr.prompt, fr.completion))
        head = " ".join(fr.prompt.split()[:6])
        if head and head not in openings:
            openings.append(head)
    return sorted(set(sigs)), openings


def synth_examples() -> dict[str, str]:
    return {name: ex for name, (_t, _d, ex) in SYNTH_SLOTS.items()}


def sample_fills(
    kind: str, facts: list, n: int = 3, prefer: set | None = None
) -> list[dict]:
    """Real fills for the render check: fields(fact) with every synth slot
    filled by its example so a template using any allowed slot renders."""
    pool = [f for f in facts if f.kind == kind]
    if prefer:
        # id-based split: dataclass `in` on a list compares nested payloads
        # and went quadratic over the 8.8k Raman facts (minutes per kind)
        pref = [
            f
            for f in pool
            if (f.species if isinstance(f.species, str) else f.species[0]) in prefer
        ]
        pref_ids = {id(f) for f in pref}
        pool = pref + [f for f in pool if id(f) not in pref_ids]
    fills = []
    for f in pool[:n]:
        fill = {k: ("" if v is None else str(v)) for k, v in fields(f).items()}
        if kind == "structure":
            fill["sg_clause"] = f" (space group {fill['sg']})" if fill.get("sg") else ""
        for name, ex in synth_examples().items():
            if not fill.get(name):
                fill[name] = ex
        fills.append(fill)
    return fills


def kind_slots(kind: str, fills: list[dict]) -> dict[str, dict]:
    names = set(SYNTH_SLOTS)
    for fill in fills:
        names.update(fill)
    out = {}
    for name in sorted(names):
        if name in SYNTH_SLOTS:
            typ, desc, ex = SYNTH_SLOTS[name]
        else:
            typ, desc = SLOT_DESCRIBE.get(name, ("text", name))
            ex = next((fill[name] for fill in fills if fill.get(name)), "")
        out[name] = {"type": typ, "describe": desc, "example": ex}
    return out


def families_for(kind: str, kind_slot_names: set[str] | None = None) -> dict[str, dict]:
    """The family plan restricted to this kind. A family's slot table is
    written once for every kind it spans (measurement_report names both the
    Raman peak list and the LIBS body), so the per-kind entry keeps only the
    slots this kind can fill."""
    out = {}
    for name, fam in FAMILIES.items():
        if kind not in fam["kinds"]:
            continue
        slots = None
        if fam["slots"]:
            slots = [
                s
                for s in fam["slots"]
                if kind_slot_names is None or s in kind_slot_names
            ]
        out[name] = {
            "directions": list(fam["directions"]),
            "forms": list(fam["forms"]),
            "target": int(fam["target"]),
            "brief": fam["brief"],
            "slots": slots,
            "mirror": [d for d in ("forward", "backward") if d not in fam["directions"]]
            == [],
        }
    return out


def known_entities(ima: dict[str, str]) -> list[str]:
    out = set()
    for name in ima:
        w = name.strip().lower()
        if " " in w or "-" in w or not w.isalpha() or len(w) < 4 or w in ENTITY_STOP:
            continue
        out.add(w)
    return sorted(out)


def build_spec(facts: list, ima: dict[str, str], targets: set | None = None) -> dict:
    kinds = {}
    for kind, describe in KIND_DESCRIBE.items():
        fills = sample_fills(kind, facts, prefer=targets)
        if not fills:
            continue
        sigs, openings = forbidden(kind)
        slots = kind_slots(kind, fills)
        kinds[kind] = {
            "describe": describe,
            "slots": slots,
            "subject_slots": SUBJECT_SLOTS.get(kind, SUBJECT_SLOTS["default"]),
            "answer_slots": {
                "forward": FORWARD_ANSWERS.get(kind, []),
                "backward": SUBJECT_SLOTS.get(kind, SUBJECT_SLOTS["default"]),
            },
            "sample_fills": fills,
            "families": families_for(kind, set(slots)),
        }
    return {
        "version": SPEC_VERSION,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git": _git(),
        "kinds": kinds,
        "forbidden_signatures": {k: forbidden(k)[0] for k in kinds},
        "forbidden_openings": {k: forbidden(k)[1] for k in kinds},
        "known_entities": known_entities(ima),
    }


def cell_table(spec: dict) -> list[tuple[str, str, str, str, int]]:
    rows = []
    for kind, ks in spec["kinds"].items():
        for fam, fs in ks["families"].items():
            for d in fs["directions"]:
                for form in fs["forms"]:
                    rows.append((kind, fam, d, form, fs["target"]))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out", default=os.path.expanduser("~/corpora/ouroboros-synth/spec/spec.json")
    )
    ap.add_argument("--species", default=os.path.join(HERE, "synth_species.json"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from assemble import load_ima
    from facts import build_facts

    t0 = time.time()
    facts = build_facts()
    ima = load_ima()
    targets = None
    if os.path.exists(args.species):
        g = json.load(open(args.species))["groups"]
        targets = set(g["T_R"]) | set(g["T_L"]) | set(g["T_RL"])
    spec = build_spec(facts, ima, targets)
    rows = cell_table(spec)
    print(
        f"[{time.time()-t0:.0f}s] {len(facts):,} facts -> {len(spec['kinds'])} kinds, {len(rows)} cells, {sum(r[4] for r in rows)} templates at target, {len(spec['known_entities']):,} entities"
    )
    for kind, ks in spec["kinds"].items():
        print(
            f"  {kind:12} slots {len(ks['slots']):3}  families {', '.join(ks['families'])}"
        )
        print(
            f"  {'':12} forbidden {len(spec['forbidden_signatures'][kind])} frames; sample fill keys {len(ks['sample_fills'][0])}"
        )
    if args.dry_run:
        return 0
    os.makedirs(os.path.dirname(os.path.expanduser(args.out)), exist_ok=True)
    json.dump(
        spec, open(os.path.expanduser(args.out), "w"), indent=1, ensure_ascii=False
    )
    print("->", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
