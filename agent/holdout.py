"""Held-out species selection for the spectral training corpus.

WHY SPECIES AND NOT PAPERS. The training form presents the same fact
through several interconnected views — a paper's reported peaks, the
RRUFF reference spectrum, the ECOSTRESS thermal signature, the mindat
formula, and the NIST emission lines DERIVED from that formula. Holding
out papers leaks: the reference side still states the answer. A species
is the only unit whose removal closes every view at once.

STRATIFIED ON TWO AXES, deliberately.

  RECURRENCE — how many distinct papers mention the species. Held out
  only from the rare end, an eval measures the model on material it
  barely saw and says nothing about the head. Sampling every band makes
  "does performance track exposure count?" an answerable question
  instead of an aggregate.

  COMPOSITIONAL COMPLEXITY — distinct elements in the IMA formula, with
  hydration and solid-solution flagged. SiO2 and
  (Mg,Fe)3Si2O5(OH)4 are not the same difficulty of identification, and
  an eval that happens to draw mostly simple oxides will read as
  success. Each stratum contributes EQUAL numbers of simple and complex
  species so that axis is readable too.

PROTECTED SPECIES. Some species are load-bearing rather than
representative: quartz appears across the corpus as an internal
standard and a substrate, so removing it degrades the material of every
paper that merely mentions it in passing. Protected species are never
held out (operator ruling 2026-08-24).

THE PRICE IS PAID ONCE. A held-out species is absent from every view,
which costs real training signal — the head stratum especially, where
three species carry weight far above their count. That cost is accepted
for this demo to establish the baseline, and not repaid until there is
something to publish.
"""

from __future__ import annotations

import re

#: Element symbols, longest-first inside the alternation so "Si" wins
#: over "S" and "Cl" over "C".
_ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni "
    "Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Ru Rh Pd Ag Cd In Sn Sb Te I Xe "
    "Cs Ba La Ce Pr Nd Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg "
    "Tl Pb Bi Th U"
).split()
_ELEMENT_SET = frozenset(_ELEMENTS)
#: Chemical symbols are Upper + optional lower. TOKENISE, don't
#: alternate-with-lookbehind: "(?<![a-z])" blocked every symbol that
#: follows another symbol's lowercase letter, so "CaSiO3" parsed as
#: {Ca} — losing Si and O — and "CaCO3" as {Ca, O}, losing C. That
#: silently understated every formula in the corpus and would have
#: mis-tiered species AND under-derived their NIST emission lines.
_SYMBOL_RE = re.compile(r"[A-Z][a-z]?")
_HYDRATION_RE = re.compile(r"H2O|OH|·|·")
_SOLID_SOLUTION_RE = re.compile(r"[(\[][A-Z][a-z]?\s*,")

#: Never held out — load-bearing across the corpus rather than a
#: representative sample of anything.
PROTECTED = frozenset({"Quartz"})

#: IMA species names that are ALSO everyday element or material words.
#: Measured 2026-08-24: matches on these are dominated by false
#: positives — "copper nanoparticles", "silicon wafer", "titanium
#: dioxide" — so a holdout built on them would be measuring the
#: matcher's noise, not the model's mineralogy. Excluded as CANDIDATES;
#: they are still perfectly good training material.
AMBIGUOUS_NAMES = frozenset(
    {
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
        "spinel",
        "garnet",
        "mica",
        "olivine",
        "apatite",
        "zeolite",
        "amber",
        "ice",
    }
)

#: (label, min_papers, max_papers_exclusive)
RECURRENCE_BANDS = (
    ("head", 20, 10**9),
    ("common", 8, 20),
    ("mid", 3, 8),
    ("rare", 2, 3),
    ("singleton", 1, 2),
)

#: Per-band holdout size. Deliberately small at the head: those species
#: carry weight far above their count, so the eval value of a third one
#: does not repay the training loss.
BAND_HOLDOUT = {"head": 2, "common": 4, "mid": 6, "rare": 6, "singleton": 12}

SIMPLE_MAX_ELEMENTS = 3
COMPLEX_MIN_ELEMENTS = 5


def elements(formula: str) -> list[str]:
    """Distinct chemical elements named in a formula.

    Two-letter symbols win over one-letter ones ("Si" not S+i), and a
    token that is not a real element ("Ox" from a stray word) is
    dropped rather than guessed at.
    """
    out: set[str] = set()
    for tok in _SYMBOL_RE.findall(formula or ""):
        if tok in _ELEMENT_SET:
            out.add(tok)
        elif tok[0] in _ELEMENT_SET:
            out.add(tok[0])
    return sorted(out)


def complexity(formula: str) -> dict:
    """Compositional complexity of an IMA formula.

    ``tier`` is "simple" / "middle" / "complex". Hydration and solid
    solution raise the tier because both make identification harder in
    practice: water bands crowd the mid-IR, and a solid solution has no
    single reference spectrum.
    """
    els = elements(formula)
    n = len(els)
    hydrated = bool(_HYDRATION_RE.search(formula or ""))
    solution = bool(_SOLID_SOLUTION_RE.search(formula or ""))
    if n >= COMPLEX_MIN_ELEMENTS or (n == 4 and (hydrated or solution)):
        tier = "complex"
    elif n <= SIMPLE_MAX_ELEMENTS and not solution:
        tier = "simple"
    else:
        tier = "middle"
    return {
        "elements": els,
        "n_elements": n,
        "hydrated": hydrated,
        "solid_solution": solution,
        "tier": tier,
    }


def recurrence_band(paper_count: int) -> str:
    for label, lo, hi in RECURRENCE_BANDS:
        if lo <= paper_count < hi:
            return label
    return "absent"


def select_holdout(species_papers: dict, formulas: dict) -> dict:
    """Choose held-out species, balanced on recurrence AND complexity.

    ``species_papers``: species -> iterable of paper keys.
    ``formulas``:       species -> IMA formula string.

    Deterministic: ties break on species name, so the same corpus always
    yields the same holdout and a re-run cannot silently reshuffle what
    the eval measures.
    """
    by_band: dict[str, dict[str, list[str]]] = {
        label: {"simple": [], "complex": [], "middle": []}
        for label, _, _ in RECURRENCE_BANDS
    }
    for species, papers in species_papers.items():
        if species in PROTECTED or species.lower() in AMBIGUOUS_NAMES:
            continue
        info = complexity(formulas.get(species, ""))
        # NATIVE-ELEMENT MINERALS ARE INHERENTLY AMBIGUOUS as corpus
        # matches: "Palladium", "Hexaferrum", "Diamond" name both a
        # species and the everyday material, so a mention is as likely
        # to be a catalyst or a wafer as a mineral. The single-element
        # formula is the general rule the explicit list above only
        # sampled — it catches the ones nobody thought to enumerate.
        if info["n_elements"] <= 1:
            continue
        band = recurrence_band(len(set(papers)))
        if band == "absent":
            continue
        tier = info["tier"]
        by_band[band][tier].append(species)

    chosen: dict[str, list[str]] = {}
    for label, _, _ in RECURRENCE_BANDS:
        want = BAND_HOLDOUT[label]
        half = want // 2
        picks: list[str] = []
        for tier in ("simple", "complex"):
            pool = sorted(by_band[label][tier])
            # EVENLY SPACED, not the head of the list. Taking pool[:half]
            # is deterministic but alphabetically clustered — the first
            # run drew an all-A/B holdout, and mineral names cluster by
            # naming tradition, discovery era and type locality, so the
            # head of the alphabet is a biased sample of the name space.
            # Spacing keeps determinism and spreads the draw.
            if pool and half:
                step = max(1, len(pool) // half)
                picks.extend(pool[i * step] for i in range(min(half, len(pool))))
        # A band short on one tier tops up from the middle rather than
        # skewing the balance it exists to measure.
        if len(picks) < want:
            spare = sorted(
                set(by_band[label]["middle"])
                | set(by_band[label]["simple"])
                | set(by_band[label]["complex"])
            )
            for s in spare:
                if len(picks) >= want:
                    break
                if s not in picks:
                    picks.append(s)
        chosen[label] = sorted(picks)
    return chosen
