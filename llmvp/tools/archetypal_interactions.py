"""
Archetypal Interactions Tool

Deterministic lookup for Kipukas KEAL means damage bonuses.
The 15×15 Archetypal Interactions Table encodes how each attacker
archetype modifier applies against each defender archetype.

One tool is registered:

**keal_means_check** — KEAL means calculation: every attacker type ×
defender type, with itemised pairs and a summed total.  Works for both
single-pair lookups (arrays of length 1) and full multi-type matchups.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from tools.registry import Tool, get_registry

# ------------------------------------------------------------------
# Canonical archetype names (display order)
# ------------------------------------------------------------------
ARCHETYPES: List[str] = [
    "Cenozoic",
    "Decrepit",
    "Angelic",
    "Brutal",
    "Arboreal",
    "Astral",
    "Telekinetic",
    "Glitch",
    "Magic",
    "Endothermic",
    "Avian",
    "Mechanical",
    "Algorithmic",
    "Energetic",
    "Entropic",
]

# Fast lowercase → canonical lookup
_CANONICAL: Dict[str, str] = {a.lower(): a for a in ARCHETYPES}
# Common misspelling in the rules themselves
_CANONICAL["decript"] = "Decrepit"

# ------------------------------------------------------------------
# Interaction table  —  _TABLE[defender][attacker] = modifier
# Row = defender, Column = attacker (matches the published table)
# ------------------------------------------------------------------
_RAW: Dict[str, List[int]] = {
    #                        Cen  Dec  Ang  Bru  Arb  Ast  Tel  Gli  Mag  End  Avi  Mec  Alg  Ene  Ent
    "Cenozoic": [0, 3, 1, 1, -1, 2, 2, -1, -1, 2, -3, -1, -2, 1, -3],
    "Decrepit": [-3, 0, 1, 2, 1, -1, -3, -2, 3, 1, 2, -1, -1, -1, 2],
    "Angelic": [-1, -1, -3, 3, 2, -3, -2, 1, 1, 1, -1, 2, 1, -1, -1],
    "Brutal": [-1, -2, -3, 3, 2, -1, -2, 1, 2, -1, -1, 2, 1, 1, 1],
    "Arboreal": [1, -1, -2, -2, 0, -3, -1, -2, 2, 1, 3, 1, -1, 3, 1],
    "Astral": [-2, 1, 3, 1, 3, 0, -1, 1, -1, -3, -2, -1, 2, -2, 1],
    "Telekinetic": [-2, 3, 2, 2, 1, 1, 0, -3, -3, -1, -1, -1, 1, -1, 2],
    "Glitch": [1, 2, -1, -1, 2, -1, 3, 0, -3, -3, 2, 1, -2, 1, -1],
    "Magic": [1, -3, -1, -2, -2, 1, 3, 3, 0, -1, 2, 1, 1, -2, -1],
    "Endothermic": [-2, -1, -1, 1, -1, 3, 1, 3, 1, 0, 1, 2, -2, -3, -2],
    "Avian": [3, -2, 1, 1, -3, 2, 1, -2, -2, -1, 0, 3, -1, 1, -1],
    "Mechanical": [1, 1, -2, -2, -1, 1, 1, -1, -1, -2, -3, 0, 3, 2, 3],
    "Algorithmic": [2, 1, -1, -1, 1, -2, -1, 2, -1, 2, 1, -3, 0, 3, -3],
    "Energetic": [-1, 1, 1, -1, -3, 2, 1, -1, 2, 3, -1, -2, -3, 0, 2],
    "Entropic": [3, -2, 1, -1, -1, -1, -2, 1, 1, 2, 1, -3, 3, -2, 0],
}

# Build indexed lookup: TABLE[defender_canonical][attacker_canonical] -> int
TABLE: Dict[str, Dict[str, int]] = {}
for defender, row in _RAW.items():
    TABLE[defender] = {ARCHETYPES[i]: v for i, v in enumerate(row)}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _normalise(name: str) -> str:
    """Return canonical archetype name or raise ValueError."""
    key = name.strip().lower()
    canonical = _CANONICAL.get(key)
    if canonical is None:
        raise ValueError(
            f"Unknown archetype: {name!r}.  " f"Valid types: {', '.join(ARCHETYPES)}"
        )
    return canonical


# ------------------------------------------------------------------
# Core functions
# ------------------------------------------------------------------
def lookup_keal_damage_bonus(attacker: str, defender: str) -> int:
    """
    Single pair lookup.

    Returns the modifier applied to the attacker's roll when
    *attacker* archetype attacks *defender* archetype.
    """
    att = _normalise(attacker)
    dfd = _normalise(defender)
    return TABLE[dfd][att]


def calculate_keal_means_total(
    attacker_types: List[str],
    defender_types: List[str],
) -> Dict[str, Any]:
    """
    Full KEAL means interaction.

    Every attacker type is checked against every defender type.
    Returns ``{"pairs": [{"attacker": ..., "defender": ..., "modifier": int}, ...],
               "total_bonus": int}``.
    """
    pairs: List[Dict[str, Any]] = []
    total = 0
    for att_raw in attacker_types:
        att = _normalise(att_raw)
        for dfd_raw in defender_types:
            dfd = _normalise(dfd_raw)
            mod = TABLE[dfd][att]
            pairs.append({"attacker": att, "defender": dfd, "modifier": mod})
            total += mod
    return {"pairs": pairs, "total_bonus": total}


# ------------------------------------------------------------------
# Tool wrappers (string-in → string-out for registry)
# ------------------------------------------------------------------
def _exec_check(params: Dict[str, Any]) -> str:
    """Execute KEAL means check tool."""
    attacker_types = params.get("attacker_types", [])
    defender_types = params.get("defender_types", [])
    if not isinstance(attacker_types, list) or not isinstance(defender_types, list):
        return json.dumps({"error": "attacker_types and defender_types must be arrays"})
    try:
        result = calculate_keal_means_total(attacker_types, defender_types)
        return json.dumps(result)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})


# ------------------------------------------------------------------
# Self-registration
# ------------------------------------------------------------------
def register() -> None:
    """Register archetypal interaction tools with the global registry."""
    registry = get_registry()

    registry.register(
        Tool(
            name="keal_means_check",
            description=(
                "Look up or calculate the KEAL means damage bonus.  "
                "Each KEAL means has up to 3 archetypal types; every "
                "attacker type is compared against every defender type "
                "and the modifiers are summed.  For a single-pair lookup "
                "pass one-element arrays."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "attacker_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of attacker KEAL means archetypal "
                            "adaptations (1-3 types)"
                        ),
                    },
                    "defender_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of defender KEAL means archetypal "
                            "adaptations (1-3 types)"
                        ),
                    },
                },
                "required": ["attacker_types", "defender_types"],
            },
            execute=_exec_check,
        )
    )


# Auto-register on import
register()
