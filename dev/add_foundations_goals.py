#!/usr/bin/env python3
"""Add the two FOUNDATIONS research goals to the live spectra mission.

Operator-directed (2026-08-29): the OLMo probe showed the trained model
failing to generalize — polymorphs unsolved for want of INPUT — so the
corpus gains (1) the physics of its own techniques and (2) mineral
formation/chemistry, plus a widened objective so the curator (which quotes
the objective verbatim as its corpus-fit test) can accept them.

Mechanics follow the only route that actually drives discovery
(dev exploration, 2026-08-29): hand-edit .agent/mission.json while the
mission is STOPPED — append AspectSpecs to research_plan.aspects AND
matching type:"discovery" GoalRecords whose finding_signature is
"aspect-discovery:" + slug(name), the same slug _aspect_slug computes.
(`mission reopen --add-goal` mints type:"functional", which the discovery
sweep skips; re-running the plan phase would REPLACE the hand-written
aspects wholesale.) The new goal records are cloned from an existing
discovery goal so every default-bearing field deserializes identically.

Usage:
  .venv/bin/python dev/add_foundations_goals.py          # dry run
  .venv/bin/python dev/add_foundations_goals.py --apply
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime, timezone

MISSION = os.path.expanduser("~/corpora/ouroboros-spectra/.agent/mission.json")

OBJECTIVE_OLD_SENTENCE = (
    "Papers that only discuss a technique, model it, or review it without "
    "showing measured data are\n  out of scope."
)

FOUNDATIONS_PARA = (
    "\n\nAlso collect, as FOUNDATIONS for the above: (1) papers on the "
    "physics of these techniques - the theory of Raman scattering and "
    "selection rules, the plasma physics and diagnostics underlying LIBS "
    "and spark emission (Boltzmann and Saha analysis, line broadening, "
    "matrix effects, calibration-free methods), diffraction physics and "
    "structure refinement, vibrational mode and factor-group analysis, "
    "luminescence mechanisms, and quantitative calibration and "
    "chemometrics theory; and (2) technical and geological papers on "
    "mineral formation and chemistry - phase stability and phase "
    "diagrams, polymorphism and phase transformations, crystal chemistry "
    "and solid solution, igneous, metamorphic and hydrothermal "
    "petrogenesis, alteration and weathering, and the geochemistry that "
    "controls mineral composition. A foundations paper earns its place "
    "by rigorous physical or chemical content that grounds the "
    "interpretation of measured spectra, even when it presents no new "
    "spectra of its own. Application surveys that merely mention a "
    "technique, with neither measured data nor foundational analysis, "
    "remain out of scope."
)

ASPECTS = [
    {
        "name": "Spectroscopy technique physics",
        "description": (
            "Foundational physics of the corpus's techniques: Raman "
            "scattering theory and selection rules, plasma physics and "
            "diagnostics for LIBS/spark emission (Boltzmann/Saha, line "
            "broadening, self-absorption, matrix effects, calibration-free "
            "analysis), X-ray diffraction physics and Rietveld refinement, "
            "vibrational mode and factor-group analysis of crystals, "
            "luminescence mechanisms, and chemometrics for spectral "
            "quantification. Theory and methods papers qualify without new "
            "measured spectra."
        ),
        "seed_queries": [
            "Raman scattering theory selection rules crystals",
            "laser induced plasma Boltzmann Saha line broadening",
            "LIBS matrix effects calibration free quantification",
            "X-ray diffraction structure factor Rietveld refinement theory",
            "infrared vibrational modes factor group analysis minerals",
            "luminescence mechanisms minerals activator centers",
        ],
        "coverage_target": 300,
        "last_have": 0,
        "dry_rounds": 0,
    },
    {
        "name": "Mineral formation and chemistry",
        "description": (
            "Mineral formation and crystal chemistry: phase stability and "
            "phase diagrams, polymorphism and phase transformations, solid "
            "solution and cation substitution, igneous/metamorphic/"
            "hydrothermal petrogenesis, ore formation, alteration and "
            "weathering sequences, and the geochemistry controlling "
            "mineral composition. Technical and geological depth valued "
            "over surveys; measured compositions, thermodynamic data and "
            "structural detail all count."
        ),
        "seed_queries": [
            "mineral phase diagram stability relations",
            "polymorphism phase transformation minerals",
            "crystal chemistry solid solution substitution minerals",
            "hydrothermal alteration mineral assemblage geochemistry",
            "magmatic crystallization sequence mineral chemistry",
            "clay mineral formation weathering products",
        ],
        "coverage_target": 300,
        "last_have": 0,
        "dry_rounds": 0,
    },
]


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.strip().lower()).strip("-")


def mission_is_running() -> bool:
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001
        return True
    return any(
        "ouroboros.py start" in ln for ln in out.splitlines() if "grep" not in ln
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if mission_is_running():
        print("REFUSING: mission process is running.")
        return 2

    with open(MISSION, encoding="utf-8") as fh:
        m = json.load(fh)

    changes: list[str] = []

    # 1. Objective widening — the curator quotes this verbatim.
    obj = m.get("objective") or ""
    if "FOUNDATIONS" in obj:
        changes.append("objective: already widened (skip)")
    else:
        if OBJECTIVE_OLD_SENTENCE.replace("\n  ", " ") in obj.replace("\n", " "):
            # normalize-agnostic replace: drop the exclusion sentence's
            # absolute form; the foundations paragraph restates it scoped.
            for form in (
                OBJECTIVE_OLD_SENTENCE,
                OBJECTIVE_OLD_SENTENCE.replace("\n  ", " "),
                "Papers that only discuss a technique, model it, or review "
                "it without showing measured data are out of scope.",
            ):
                if form in obj:
                    obj = obj.replace(
                        form,
                        "Papers that merely mention a technique in passing "
                        "are out of scope (see the foundations rule below "
                        "for theory papers).",
                    )
                    break
        obj = obj.rstrip() + FOUNDATIONS_PARA + "\n"
        m["objective"] = obj
        changes.append("objective: widened with the FOUNDATIONS paragraph")

    plan = m.get("research_plan") or {}
    aspects = plan.setdefault("aspects", [])
    existing_names = {str(a.get("name", "")).lower() for a in aspects}

    # Template goal: an existing design-origin discovery goal.
    goals = m.setdefault("goals", [])
    template = next(
        (
            g
            for g in goals
            if g.get("type") == "discovery" and g.get("origin") == "design"
        ),
        None,
    )
    if template is None:
        print("no discovery goal to clone — aborting")
        return 3
    existing_sigs = {g.get("finding_signature") for g in goals}

    for spec in ASPECTS:
        name = spec["name"]
        sig = "aspect-discovery:" + _slug(name)
        if name.lower() in existing_names:
            changes.append(f"aspect '{name}': already present (skip)")
        else:
            aspects.append(dict(spec))
            changes.append(
                f"aspect '{name}': appended (target {spec['coverage_target']})"
            )
        if sig in existing_sigs:
            changes.append(f"goal {sig}: already present (skip)")
            continue
        g = copy.deepcopy(template)
        g["id"] = uuid.uuid4().hex[:12]
        g["description"] = (
            f"Aspect '{name}': discover at least "
            f"{spec['coverage_target']} candidate papers"
        )
        g["type"] = "discovery"
        g["origin"] = "design"
        g["status"] = "incomplete"
        g["finding_signature"] = sig
        g["reports"] = []
        g["reports_archived"] = 0
        g["attempts_archived"] = 0
        g["failed_attempts"] = []
        # str, not None — GoalRecord.last_completed_at is `str = ""` and the
        # mission-doc round-trip gate validates the LIVE file (caught live).
        g["last_completed_at"] = ""
        goals.append(g)
        changes.append(f"goal {sig}: appended as {g['id']}")

    # 3. The corpus catalog goal must be open or new candidates pile into a
    # worklist no phase drains (research_plan_actions.py:528-533 hazard).
    for g in goals:
        if g.get("type") == "extraction" and g.get("status") != "incomplete":
            g["status"] = "incomplete"
            changes.append(f"catalog goal {g.get('id')}: reopened")

    for c in changes:
        print(" ", c)
    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = MISSION + f".bak-{stamp}"
    shutil.copy2(MISSION, backup)
    tmp = MISSION + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, MISSION)
    print(f"written; backup at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
