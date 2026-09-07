"""mindat prose fields -> stage-1 paragraphs (root venv). LICENCE-FLAGGED.

The structural fields of geomaterials.jsonl already reach training through
`reference_layer.load_mindat_structure` (v3 precedent). The PROSE fields —
`description_short`, `aboutname` (etymology), `occurrence`,
`otheroccurrence`, `commentcrystal`, `opticalr` and the IMA list's
`description_short` — are the encyclopaedic sentences a 1B model has no
other source for on 6,000+ species. Measured 2026-09-07: ~0.9M tokens over
the 65,070 geomaterial rows, ~0.19M over the IMA list. Mindat's API terms are
non-commercial; included for this private research run by operator decision,
every record tagged `license: restricted-mindat`.

A record is one paragraph per species, in the species' own words with a
lead-in so the name is bound to every sentence: "<Name>: <description>.
Name: <etymology>. Occurrence: <…>."
"""

from __future__ import annotations

import json
import os
import re

from reference_layer import REF_ROOT

GEO = os.path.join(REF_ROOT, "mindat", "geomaterials.jsonl")
IMA = os.path.join(REF_ROOT, "mindat", "minerals_ima.jsonl")
LICENSE = "restricted-mindat"

_FIELDS = (
    ("description_short", ""),
    ("aboutname", "Name"),
    ("occurrence", "Occurrence"),
    ("otheroccurrence", "Also occurs"),
    ("commentcrystal", "Crystals"),
    ("opticalr", "Optical"),
    ("mindat_formula_note", "Formula note"),
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(v) -> str:
    if not isinstance(v, str):
        return ""
    v = _TAG_RE.sub("", v).replace("\r", " ")
    v = re.sub(r"\s+", " ", v).strip()
    return v


def paragraph(rec: dict) -> str:
    name = _clean(rec.get("name"))
    if not name:
        return ""
    parts = []
    for key, lead in _FIELDS:
        v = _clean(rec.get(key))
        if len(v) < 20:
            continue
        v = v if v.endswith((".", "!", "?")) else v + "."
        parts.append(f"{lead}: {v}" if lead else v)
    if not parts:
        return ""
    return f"{name}: " + " ".join(parts)


def iter_paragraphs():
    """(species, text, source_file) across geomaterials, then IMA descriptions
    for species the geomaterials rows did not describe."""
    seen: set[str] = set()
    if os.path.exists(GEO):
        for line in open(GEO, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            text = paragraph(rec)
            if text:
                seen.add(_clean(rec.get("name")).lower())
                yield _clean(rec.get("name")), text, "geomaterials.jsonl"
    if os.path.exists(IMA):
        for line in open(IMA, encoding="utf-8"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            name = _clean(rec.get("name"))
            if not name or name.lower() in seen:
                continue
            d = _clean(rec.get("description_short"))
            if len(d) >= 20:
                yield name, f"{name}: {d if d.endswith('.') else d + '.'}", "minerals_ima.jsonl"
