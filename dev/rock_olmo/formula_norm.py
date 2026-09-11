#!/usr/bin/env python3
"""One spelling for chemical formulas: plain textbook style, the way OLMo was pretrained.

WHY (PROCEDURE.md §20, 2026-09-10). The corpus spells the same formula in at least
thirteen conventions — RRUFF `Fe^2+^Fe^3+^2O4`, mindat `Fe<sup>2+</sup>…<sub>4</sub>`,
webmineral `Fe++Fe+++2O4`, ROD/CIF `Fe3 O4`, HOM `Fe2 O3` (pdftotext), papers
`Fe_{3}O_{4}` / `Fe₃O₄` / `$_3$` — and the base model's own likelihood ranks plain
`Fe3O4` (4 tokens, 1.4 nats) far ahead of every one of them (RRUFF 14 tokens, 39.6
nats; mindat 29 tokens, 32.3 nats). Across 3,395 species shared by ≥ 3 sources the
average was 3.2 spellings each. A 1.5B model should see one.

TARGET SPELLING (decided with the operator): elements with inline digit subscripts;
NO oxidation-state charges inside a formula (`Fe3O4`, not `Fe2+Fe3+2O4`); `·` for
hydration with no spaces (`CaSO4·2H2O`); parentheses only where a group carries a
multiplier or a comma (`Ca5(PO4)3(OH)` stays, `Ca(CO3)` → `CaCO3`); vacancy symbols
dropped; ranges and `x` kept as ASCII; Greek prefixes kept (`α-Fe2O3`).

THREE ENTRY POINTS
- normalize_formula(s, keep_charges=False)  — a formula STRING in any convention.
- species_formula(name)                        — the canonical formula for a named species
  (mindat IMA formula, else RRUFF ideal), normalised; None if unknown. Table cached at
  ~/corpora/rock-olmo-training/formula_table.json (build_species_table()).
- normalize_text_formulas(text)                — PROSE: rewrites formula spans in place
  (LaTeX / HTML / unicode subscripts, OCR spacing, hydration dots), converts ion charges
  to one ASCII spelling (`Fe3+`), and leaves equation LaTeX alone. A span is rewritten
  only if it parses to ≥ 2 element symbols (or one element with a subscript ≥ 2).

Stdlib only; runs under either venv.
"""

from __future__ import annotations

import html
import json
import os
import re
from typing import Iterable

ELEMENTS = set(
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr "
    "Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu "
    "Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr "
    "Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og REE Ln".split()
)
# CIF sums are alphabetical; textbook order is cations first, then C, H, then the rest, O last.
_LATE = ["C", "H", "B", "Si", "Ge", "P", "As", "Sb", "N", "S", "Se", "Te", "F", "Cl", "Br", "I", "O"]
_SUB = str.maketrans("₀₁₂₃₄₅₆₇₈₉ₓₙ₋₊", "0123456789xn-+")
_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")
_VACANCY = ("[box]", "□", "◻", "◽", "▢")
_DASHES = str.maketrans("–—−‐", "----")
_OXYANIONS = ("CO3", "SO4", "PO4", "NO3", "SiO4", "AsO4", "VO4", "CrO4", "WO4", "MoO4", "BO3", "SeO4", "SO3")

_HTML_SUP = re.compile(r"<sup>\s*([^<]*?)\s*</sup>")
_HTML_SUB = re.compile(r"<sub>\s*([^<]*?)\s*</sub>")
_CARET = re.compile(r"\^([^^]{0,6})\^")  # RRUFF ^2+^
_TEX_WRAP = re.compile(r"\\(?:mathrm|text|ce|mathit|rm)\s*\{([^{}]*)\}")
_TEX_SUP = re.compile(r"\$?\^\{\s*([0-9]*\s*[+-]|[0-9]+)\s*\}\$?|\$?\^([0-9]?[+-]|[0-9])\$?")
_TEX_SUB = re.compile(r"\$?_\{\s*([^{}$]*?)\s*\}?\$?|\$?_([0-9]+(?:\.[0-9]+)?|[xyzn]|[0-9.]+-[0-9.]+)\$?")
_UNI_SUP_GROUP = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]*[⁺⁻]")
_WM_CHARGE = re.compile(r"(?<=[A-Za-z\)\]])(\+{1,8}|-{1,8})")  # webmineral Ni++ / Cl-- / Fe+++2O4
_ASCII_CHARGE_IN_FORMULA = re.compile(r"(?<=[A-Za-z\)\]])(\d?[+-])(?=[A-Z(\[),\]·]|$)")  # Fe3+Fe2+2O4 → Fe3O4
_HYDRATE = re.compile(r"(?:·|\.(?=\d*\(?H2O))(\d*\.?\d*|[xn]|\d+\.\d+-\d+\.?\d*)(\(?H2O\)?)")
_CIF_SUM = re.compile(r"^(?:[A-Z][a-z]?\d*(?:\.\d+)?)(?: [A-Z][a-z]?\d*(?:\.\d+)?)+$")
_PLAIN_OK = re.compile(r"^[A-Za-zα-ω0-9()\[\],·\-+x.]+$")


def _charge_text(raw: str) -> str:
    """'2+' / '+2' / '++' / 'III' → '2+'; '' → ''."""
    r = raw.strip().replace(" ", "")
    if not r:
        return ""
    if set(r) <= {"+"}:
        return f"{len(r)}+" if len(r) > 1 else "+"
    if set(r) <= {"-"}:
        return f"{len(r)}-" if len(r) > 1 else "-"
    m = re.fullmatch(r"([0-9]*)([+-])", r) or re.fullmatch(r"([+-])([0-9]*)", r[::-1])
    if m:
        d, s = (m.group(1), m.group(2)) if r[-1] in "+-" else (m.group(1)[::-1], m.group(2))
        return f"{d}{s}"
    return ""


def _reorder_cif_sum(s: str) -> str:
    parts = re.findall(r"([A-Z][a-z]?)(\d*(?:\.\d+)?)", s)
    if not parts or not all(el in ELEMENTS for el, _ in parts):
        return s.replace(" ", "")
    cations = sorted([p for p in parts if p[0] not in _LATE], key=lambda p: p[0])
    late = sorted([p for p in parts if p[0] in _LATE], key=lambda p: _LATE.index(p[0]))
    return "".join(el + ("" if n in ("", "1") else n) for el, n in cations + late)


def normalize_formula(s: str, *, keep_charges: bool = False) -> str:
    """A formula string in any corpus convention → plain textbook spelling."""
    if not s:
        return ""
    t = html.unescape(s).strip()
    t = re.split(r"\s;\s|;\s+(?=[a-z])", t)[0]  # RRUFF measured chemistry carries commentary after ';'
    t = t.translate(_DASHES)
    for v in _VACANCY:
        t = t.replace(v, "")
    t = _TEX_WRAP.sub(r"\1", t)
    keep = keep_charges

    def sup(m):
        c = _charge_text(m.group(1) if m.group(1) is not None else (m.group(2) or ""))
        return c if keep else ""
    t = _HTML_SUP.sub(lambda m: (_charge_text(m.group(1)) if keep else ""), t)
    t = _CARET.sub(lambda m: (_charge_text(m.group(1)) if keep else ""), t)
    t = _TEX_SUP.sub(sup, t)
    t = _HTML_SUB.sub(r"\1", t)
    t = _TEX_SUB.sub(lambda m: (m.group(1) if m.group(1) is not None else m.group(2)) or "", t)
    t = _UNI_SUP_GROUP.sub(lambda m: (_charge_text(m.group(0).translate(_SUP)) if keep else ""), t)
    t = t.translate(_SUB).translate(_SUP)
    t = t.replace("$", "").replace("{", "").replace("}", "")
    t = re.sub(r"[•∙⋅]", "·", t)
    if _CIF_SUM.match(t.strip()):
        t = _reorder_cif_sum(t.strip())
    t = re.sub(r"\s+", "", t)
    t = _WM_CHARGE.sub(lambda m: (_charge_text(m.group(1)) if keep else ""), t)
    if not keep:
        t = _ASCII_CHARGE_IN_FORMULA.sub("", t)
    t = _HYDRATE.sub(lambda m: "·" + (m.group(1) or "") + m.group(2).strip("()"), t)
    t = re.sub(r"\(\(H2O\)\)", "(H2O)", t)
    # unwrap a LONE oxyanion group with no multiplier: Ca(CO3) → CaCO3, Ca(SO4)·2H2O → CaSO4·2H2O;
    # multi-group formulas keep their parentheses (K(UO2)(AsO4)·4H2O)
    if t.count("(") == 1:
        t = re.sub(r"\((%s)\)(?![0-9x.])" % "|".join(_OXYANIONS), r"\1", t)
    t = t.replace(";", ",").strip(" .,;:")
    return t


# ── species → canonical formula ───────────────────────────────────────────────
TABLE_PATH = os.path.expanduser("~/corpora/rock-olmo-training/formula_table.json")
_TABLE: dict[str, str] | None = None


def build_species_table(path: str = TABLE_PATH) -> dict[str, str]:
    """mindat IMA formula first (6k IMA species), RRUFF ideal formula for the rest."""
    import sys

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import reference_layer as rl  # noqa: E402

    table: dict[str, str] = {}
    rruff: dict[str, str] = {}
    for r in rl.iter_rruff("excellent_unoriented.zip"):
        f = normalize_formula(r.get("ideal_formula") or "")
        if f and r["species"].lower() not in rruff:
            rruff[r["species"].lower()] = f
    mindat_path = os.path.expanduser("~/corpora/mineral-refs/mindat/geomaterials.jsonl")
    if os.path.exists(mindat_path):
        for line in open(mindat_path, errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            name = (d.get("name") or "").strip().lower()
            raw = d.get("ima_formula") or d.get("mindat_formula") or ""
            f = normalize_formula(raw)
            if name and f and _PLAIN_OK.match(f):
                table[name] = f
    for k, v in rruff.items():
        table.setdefault(k, v)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(table, open(path, "w"))
    return table


def species_formula(name: str) -> str | None:
    global _TABLE
    if _TABLE is None:
        _TABLE = json.load(open(TABLE_PATH)) if os.path.exists(TABLE_PATH) else build_species_table()
    return _TABLE.get((name or "").strip().lower())


# ── prose pass ────────────────────────────────────────────────────────────────
# a candidate span: starts with an element-looking capital, continues through formula characters,
# decorations included; spaces only in the OCR form "Fe 3 O 4" handled separately
_SPAN = re.compile(r"(?<![A-Za-z])([A-Z][A-Za-z0-9_{}$()\[\]<>/₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻^+\-·•∙⋅.,]*[A-Za-z0-9)\]}$>₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻])")
_DECORATED = re.compile(r"[_{}$<^₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻•∙⋅]|\+\+")
_OCR_SPACED = re.compile(r"\b([A-Z][a-z]?) (\d) ([A-Z][a-z]?) (\d)\b")
_TOKENS = re.compile(r"([A-Z][a-z]?)(\d*)")


def _looks_chemical(plain: str) -> bool:
    body = re.sub(r"[·().,\-+x]|\d+\.\d+", "", plain)
    toks = _TOKENS.findall(body)
    if not toks or "".join(el + n for el, n in toks) != re.sub(r"[^A-Za-z0-9]", "", body):
        return False
    els = [el for el, _ in toks]
    if not all(el in ELEMENTS for el in els):
        return False
    if len(set(els)) >= 2:
        return True
    return any(n and n not in ("1",) for _, n in toks)  # O2, N2, H2 …


def normalize_text_formulas(text: str) -> str:
    """Rewrite decorated formula spans in prose; equation LaTeX and ordinary words untouched."""
    def fix(m):
        span = m.group(1)
        if not _DECORATED.search(span):
            return span
        stripped = span.rstrip(".,;:")
        tail = span[len(stripped):]
        plain = normalize_formula(stripped, keep_charges=True)
        if not plain or not _looks_chemical(plain):
            return span
        return plain + tail

    out = _SPAN.sub(fix, text)
    out = _OCR_SPACED.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + m.group(4)
                          if m.group(1) in ELEMENTS and m.group(3) in ELEMENTS else m.group(0), out)
    return out


def residual_markup(s: str) -> list[str]:
    """Which conventions survive in a string (for the sampler's statistics)."""
    out = []
    if re.search(r"<su[bp]>|&#\d+;", s): out.append("html")
    if re.search(r"\^[^^]{0,6}\^", s): out.append("caret")
    if re.search(r"_\{|\$_|_[0-9]", s): out.append("latex")
    if re.search(r"[₀-₉⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]", s): out.append("unicode")
    if re.search(r"[A-Za-z]\+\+", s): out.append("plusplus")
    if re.search(r"[•∙⋅]", s): out.append("bullet")
    if any(v in s for v in _VACANCY): out.append("vacancy")
    if re.search(r"[A-Z][a-z]?\d* [A-Z][a-z]?\d", s): out.append("spaced")
    return out
