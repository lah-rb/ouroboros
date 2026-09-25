#!/usr/bin/env python3
"""XML records with fill-in-the-middle blanks for the v3 stage-2 anneal
(PROCEDURE.md §22; root venv for the render — the facts layer — and
importable from the rock venv for the probe, the facts import is lazy).

WHY XML. §19–§21c: the model learns forward facts from prose frames, and a
single exact key reverses (formula → species 0.97–0.99), but every approximate
NUMERIC key (bands, cell parameters) stays ≤ 0.055 backward. The operator's
question: is a tag-delimited record — one attribute per value, the bands in
INTENSITY order, one blank per record filled through the FIM sentinels the
stage-0 primer taught — more communicative and reversible than "shows Raman
bands at …"? Stage 2 is CONFINED to this schema (numeric-key mitigations are
deferred so fill is tested against the previous strategy without confound).

ONE CANONICAL RECORD PER SPECIES (one paragraph, no blank lines, so the
packer never cuts inside it):

    <mineral species="Calcite" formula="CaCO3" system="trigonal">
    <raman laser_nm="532"><top>1086</top><next>282</next><next>712</next><next>156</next></raman>
    <libs><line>393.37</line><line>396.85</line><line>422.67</line><line>445.48</line></libs>
    </mineral>

  * species, formula (formula_norm spelling, from the IMA fact), system
    (crystal system, attribute omitted when unknown);
  * <raman>: the FOUR STRONGEST canonical bands by relative intensity
    (probe_scoring.strongest_bands; `templates.top4` is by POSITION), integer
    cm-1, <top> first then <next> ×3; laser_nm when the record has one;
  * <libs>: the four strongest predicted LIBS lines (strongest_lines), 2 dp.

DIVERSITY without leaving the schema: attribute order ×2 (species formula
system | system species formula — species and formula always adjacent) ×
block order ×2 (raman, libs | libs, raman) = 4 trained permutations; a
never-rendered PROBE permutation (formula species system, raman first) is the
held-out frame. Two variants: `full` and `raman_only` (no <libs>).

BLANKS, one per record, middle = the exact substring (fim_transform.fim_wrap,
PSM and SPM): species (symbolic backward, formula visible), identity (the
adjacent species + formula attributes — the NUMERIC backward test: only the
system and the bands [+ lines] are visible), formula, raman (the inner
content), libs (inner), top (one band given the other three), system. Per
species: (7 full + 5 raman_only kinds) × 4 perms × 2 orders = 96 FIM
examples, plus 6 plain appearances (3 seeded catalogue bundles of 8 records
per variant, `[source: reference/xml]` tag line).

SPECIES. Eligible = formula + canonical Raman with ≥ 4 bands and aligned
intensities + a LIBS fact. The probe_species.json species (U) are NEVER
rendered — the untouched population. 1 % of trained species (seeded, the
corpus_stage1 hash) are val: every record and blank of theirs goes to
val-xml_plain / val-xml_fim.

  ../../.venv/bin/python corpus_xml.py --root ~/corpora/rock-olmo-training/v6 [--dry-run]
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fim_transform import fim_wrap  # noqa: E402
from probe_scoring import strongest_bands, strongest_lines  # noqa: E402

SEED = 20260824  # corpus_stage1.SEED: the same hash family as every other split
VAL_FRACTION = 0.01
ATTR_ORDERS = (("species", "formula", "system"), ("system", "species", "formula"))
PROBE_ATTR_ORDER = ("formula", "species", "system")  # never rendered for training
N_PERMS = 4  # perm p: attribute order p % 2, block order p // 2
PROBE_PERM = 4
VARIANTS = ("full", "raman_only")
KINDS_BY_VARIANT = {
    "full": ("species", "identity", "formula", "raman", "libs", "top", "system"),
    "raman_only": ("species", "identity", "formula", "raman", "top"),
}
ORDERS = ("psm", "spm")
N_BUNDLES = 3
BUNDLE_SIZE = 8
SOURCE_TAG = "[source: reference/xml]"
N_BANDS = 4
N_LINES = 4


def is_val(key: str, fraction: float = VAL_FRACTION) -> bool:
    return (
        int(hashlib.sha256(f"{SEED}|{key}".encode()).hexdigest()[:8], 16) % 10_000
        < fraction * 10_000
    )


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@dataclass
class XmlRecord:
    species: str
    formula: str
    system: str  # "" when unknown
    bands: list[int]  # strongest first
    laser_nm: str
    libs: list[float]  # strongest first, nm


# ── records from the facts layer ─────────────────────────────────────
def eligible_records(facts) -> tuple[list[XmlRecord], dict]:
    """Every species with a formula, a canonical Raman fact (≥ 4 bands with
    aligned intensities) and a LIBS fact; plus the census of the rejects."""
    by: dict[str, dict] = {}
    for f in facts:
        if not isinstance(f.species, str):
            continue
        if f.kind == "raman_bands" and not f.canonical:
            continue
        by.setdefault(f.kind, {})[f.species] = f
    stats: collections.Counter = collections.Counter()
    out: list[XmlRecord] = []
    for sp, r in sorted(by.get("raman_bands", {}).items()):
        fo = by.get("formula", {}).get(sp)
        lf = by.get("libs_lines", {}).get(sp)
        st = by.get("structure", {}).get(sp)
        bands = r.payload.get("bands_cm1") or []
        rel = r.payload.get("rel") or []
        if not fo or not fo.formula:
            stats["no_formula"] += 1
            continue
        if len(bands) < N_BANDS or len(rel) != len(bands):
            stats["bands_short_or_unaligned"] += 1
            continue
        if not lf:
            stats["no_libs"] += 1
            continue
        out.append(
            XmlRecord(
                species=sp,
                formula=fo.formula,
                system=((st.payload.get("crystal_system") or "") if st else "").lower(),
                bands=[int(round(b)) for b in strongest_bands(bands, rel, N_BANDS)],
                laser_nm=str(r.payload.get("laser_nm") or ""),
                libs=strongest_lines(lf.payload["groups"], N_LINES),
            )
        )
        stats["eligible"] += 1
    return out, dict(stats)


def build_records(holdout_path: str) -> tuple[list[XmlRecord], list[XmlRecord], dict]:
    """(trained, untouched, stats) from the live facts layer (root venv)."""
    from facts import build_facts  # lazy: the rock venv has no facts layer

    holdout = load_holdout(holdout_path)
    records, stats = eligible_records(build_facts(set()))
    trained = [r for r in records if r.species not in holdout]
    untouched = [r for r in records if r.species in holdout]
    stats["holdout_listed"] = len(holdout)
    stats["untouched_with_facts"] = len(untouched)
    return trained, untouched, stats


def load_holdout(path: str) -> set[str]:
    d = json.load(open(path))
    names = d if isinstance(d, list) else d.get("species") or d.get("names") or []
    return {n if isinstance(n, str) else n.get("species", "") for n in names}


# ── rendering ────────────────────────────────────────────────────────
def _attrs(rec: XmlRecord, order: tuple[str, ...]) -> str:
    parts = []
    for a in order:
        v = getattr(rec, a)
        if a == "system" and not v:
            continue
        parts.append(f'{a}="{_esc(v)}"')
    return " ".join(parts)


def raman_block(rec: XmlRecord) -> str:
    head = f'<raman laser_nm="{_esc(rec.laser_nm)}">' if rec.laser_nm else "<raman>"
    inner = f"<top>{rec.bands[0]}</top>" + "".join(f"<next>{b}</next>" for b in rec.bands[1:])
    return f"{head}{inner}</raman>"


def libs_block(rec: XmlRecord) -> str:
    return "<libs>" + "".join(f"<line>{x:.2f}</line>" for x in rec.libs) + "</libs>"


def render_record(rec: XmlRecord, perm: int = 0, variant: str = "full") -> str:
    """One record; perm 0–3 trained, PROBE_PERM the held-out frame."""
    if perm == PROBE_PERM:
        attr_order, libs_first = PROBE_ATTR_ORDER, False
    else:
        attr_order, libs_first = ATTR_ORDERS[perm % 2], perm // 2 == 1
    blocks = [raman_block(rec)]
    if variant == "full":
        blocks.append(libs_block(rec))
        if libs_first:
            blocks.reverse()
    return "\n".join([f"<mineral {_attrs(rec, attr_order)}>", *blocks, "</mineral>"])


_ATTR_RE = {a: re.compile(rf'\b{a}="([^"]*)"') for a in ("species", "formula", "system")}
_INNER_RE = {
    "raman": re.compile(r"<raman[^>]*>(.*?)</raman>", re.S),
    "libs": re.compile(r"<libs>(.*?)</libs>", re.S),
    "top": re.compile(r"<top>([^<]*)</top>"),
}


def blank(text: str, kind: str) -> tuple[str, str, str]:
    """(prefix, middle, suffix) with prefix + middle + suffix == text."""
    if kind in _ATTR_RE:
        m = _ATTR_RE[kind].search(text)
        if not m:
            raise ValueError(f"no {kind} attribute in record")
        return text[: m.start(1)], m.group(1), text[m.end(1) :]
    if kind == "identity":
        ms = [_ATTR_RE[a].search(text) for a in ("species", "formula")]
        if not all(ms):
            raise ValueError("identity needs species and formula")
        a, b = sorted(ms, key=lambda m: m.start())
        if text[a.end() : b.start()] != " ":
            raise ValueError("species and formula are not adjacent")
        return text[: a.start()], text[a.start() : b.end()], text[b.end() :]
    if kind in _INNER_RE:
        m = _INNER_RE[kind].search(text)
        if not m:
            raise ValueError(f"no <{kind}> block in record")
        return text[: m.start(1)], m.group(1), text[m.end(1) :]
    raise ValueError(f"unknown blank kind {kind!r}")


BLANK = "\x00"  # placeholder for the one blank in a stripped record


def digit_str(v) -> str:
    """A number written digit by digit (§22i): '493' -> '4 9 3', '422.67' -> '4 2 2 . 6 7'.
    OLMo-2 writes every three-digit number as ONE token, so 493 and 495 share nothing on
    the surface; spaced, each digit is its own token and neighbours share their prefix."""
    return " ".join(str(v))


def stripped_record(
    rec: XmlRecord,
    known: set[str] | frozenset[str],
    target: str,
    *,
    formula_first: bool = False,
    libs_first: bool = False,
    raman_resolution: int | None = None,
    digits: bool = False,
    sorted_bands: bool = False,
) -> str:
    """The §22 schema holding ONLY the known fields plus one BLANK (§22g; shared by the
    granular stage 2 and probe_chains so train and probe records are byte-identical).

    `known` fields: "name", "formula"; "bands" (the four strongest), "bands1" / "bands2" /
    "bands3" (the k strongest, strongest first); "lines" (all four); "top" / "line" (the
    single value an earlier turn answered). `target` ∈ name, formula, top, line. No crystal
    system and no laser are ever shown: absent fields are omitted, never blanked.
    `raman_resolution` (§22h) writes `<raman resolution_cm1="G">`: the grid the band values
    were rounded to, so the model can read how far a value may sit from the reference.
    `digits` (§22i) writes band and line values digit by digit (digit_str).
    `sorted_bands` (§22k) writes EVERY band in rec.bands as a <band> element in
    ascending position; no band is marked strongest:
        <raman resolution_cm1="5"><band>1 5 5</band><band>2 8 0</band>...</raman>"""
    fmt_b = digit_str if digits else str
    fmt_l = (lambda x: digit_str(f"{x:.2f}")) if digits else (lambda x: f"{x:.2f}")
    attrs = []
    order = (("formula", "formula"), ("species", "name")) if formula_first else (("species", "name"), ("formula", "formula"))
    for attr, key in order:
        if key == target:
            attrs.append(f'{attr}="{BLANK}"')
        elif key in known:
            attrs.append(f'{attr}="{_esc(getattr(rec, "species" if key == "name" else "formula"))}"')
    head = "<mineral" + ("" if not attrs else " " + " ".join(attrs)) + ">"
    b = rec.bands
    k = 4 if "bands" in known else next((int(x[-1]) for x in ("bands1", "bands2", "bands3") if x in known), 0)
    rhead = f'<raman resolution_cm1="{raman_resolution}">' if raman_resolution else "<raman>"
    raman = ""
    if sorted_bands and "bands" in known:
        raman = rhead + "".join(f"<band>{fmt_b(x)}</band>" for x in sorted(b)) + "</raman>"
    elif k:
        raman = rhead + f"<top>{fmt_b(b[0])}</top>" + "".join(f"<next>{fmt_b(x)}</next>" for x in b[1:k]) + "</raman>"
    elif target == "top":
        raman = f"{rhead}<top>{BLANK}</top></raman>"
    elif "top" in known:
        raman = f"{rhead}<top>{fmt_b(b[0])}</top></raman>"
    ls = rec.libs
    libs = ""
    if "lines" in known:
        libs = "<libs>" + "".join(f"<line>{fmt_l(x)}</line>" for x in ls) + "</libs>"
    elif target == "line":
        libs = f"<libs><line>{BLANK}</line></libs>"
    elif "line" in known:
        libs = f"<libs><line>{fmt_l(ls[0])}</line></libs>"
    blocks = [x for x in ((libs, raman) if libs_first else (raman, libs)) if x]
    return "\n".join([head, *blocks, "</mineral>"])


def raman_inner(bands, *, digits: bool = False) -> str:
    """The inside of a <raman> block in intensity order: <top>..</top><next>..</next>.."""
    fmt_b = digit_str if digits else str
    return f"<top>{fmt_b(bands[0])}</top>" + "".join(f"<next>{fmt_b(x)}</next>" for x in bands[1:])


def denoise_record(observed: XmlRecord, *, raman_resolution: int | None = None, digits: bool = False) -> str:
    """§22l: a measured spectrum and a BLANK reference block. The model fills in the
    species' canonical strongest four from a noisy observation (the first half of a
    two-step identification; canonical bands -> name is the stripped bands > name pair):

        <mineral>
        <raman resolution_cm1="5"><top>1 0 1 0</top><next>4 9 5</next>...</raman>
        <reference>BLANK</reference>
        </mineral>"""
    rhead = f'<raman resolution_cm1="{raman_resolution}">' if raman_resolution else "<raman>"
    raman = rhead + raman_inner(observed.bands, digits=digits) + "</raman>"
    return "\n".join(["<mineral>", raman, f"<reference>{BLANK}</reference>", "</mineral>"])


def stripped_answer(rec: XmlRecord, target: str) -> str:
    return {"name": rec.species, "formula": rec.formula, "top": str(rec.bands[0]), "line": f"{rec.libs[0]:.2f}"}[target]


def kinds_for(rec: XmlRecord, variant: str) -> tuple[str, ...]:
    return tuple(k for k in KINDS_BY_VARIANT[variant] if k != "system" or rec.system)


def fim_example(rec: XmlRecord, kind: str, variant: str, perm: int, order: str) -> dict:
    text = render_record(rec, perm, variant)
    p, m, s = blank(text, kind)
    return {
        "ex_id": f"xml:{rec.species}:{variant}:{kind}:p{perm}:{order}",
        "species": rec.species,
        "kind": kind,
        "variant": variant,
        "perm": perm,
        "order": order,
        "prompt": fim_wrap(p, "", s, order),  # ends with <|fim_middle|>
        "completion": m,
    }


def _perm_for(species: str, variant: str, bundle: int) -> int:
    h = hashlib.sha256(f"{SEED}|perm|{species}|{variant}|{bundle}".encode()).hexdigest()
    return int(h[:8], 16) % N_PERMS


def bundles(records: list[XmlRecord], variant: str, *, val: bool) -> list[dict]:
    """N_BUNDLES seeded catalogue passes over the records, BUNDLE_SIZE per
    document, every record once per pass in a hashed permutation."""
    out = []
    for b in range(N_BUNDLES):
        rng = random.Random(f"{SEED}|bundle|{variant}|{b}|{val}")
        order = list(records)
        rng.shuffle(order)
        for i in range(0, len(order), BUNDLE_SIZE):
            chunk = order[i : i + BUNDLE_SIZE]
            texts = [render_record(r, _perm_for(r.species, variant, b), variant) for r in chunk]
            out.append(
                {
                    "doc_id": f"xml:bundle:{variant}:{b}:{i // BUNDLE_SIZE}{':val' if val else ''}",
                    "source": "xml_plain",
                    "text": SOURCE_TAG + "\n" + "\n\n".join(texts),
                    "license": "reference-mixed",
                    "provenance": {"variant": variant, "bundle": b, "species": [r.species for r in chunk]},
                    "val": val,
                    "max_repeats": 1,
                }
            )
    return out


def fim_rows(records: list[XmlRecord], *, val: bool, perms: int = N_PERMS) -> list[dict]:
    rows = []
    for rec in records:
        for variant in VARIANTS:
            for kind in kinds_for(rec, variant):
                for perm in range(perms):
                    for order in ORDERS:
                        row = fim_example(rec, kind, variant, perm, order)
                        row["val"] = val
                        rows.append(row)
    return rows


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/rock-olmo-training/v6"))
    ap.add_argument("--holdout", default=os.path.join(HERE, "probe_species.json"))
    ap.add_argument("--perms", type=int, default=N_PERMS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    t0 = time.time()
    trained, untouched, stats = build_records(args.holdout)
    val_sp = {r.species for r in trained if is_val(f"xml:{r.species}")}
    train_recs = [r for r in trained if r.species not in val_sp]
    val_recs = [r for r in trained if r.species in val_sp]
    plain = bundles(train_recs, "full", val=False) + bundles(train_recs, "raman_only", val=False)
    plain += bundles(val_recs, "full", val=True) + bundles(val_recs, "raman_only", val=True)
    fim = fim_rows(train_recs, val=False, perms=args.perms) + fim_rows(val_recs, val=True, perms=args.perms)
    by_kind = collections.Counter(r["kind"] for r in fim)
    est_tokens = int(sum(len(r["prompt"]) + len(r["completion"]) for r in fim) / 3.0) + int(
        sum(len(d["text"]) for d in plain) / 3.0
    )
    man = {
        "seed": SEED,
        "perms": args.perms,
        "holdout": args.holdout,
        "holdout_sha256": _sha(args.holdout),
        "stats": stats,
        "species_trained": len(trained),
        "species_val": sorted(val_sp),
        "species_untouched": len(untouched),
        "untouched": [r.species for r in untouched],
        "bundles": len(plain),
        "fim_rows": len(fim),
        "fim_rows_by_kind": dict(by_kind),
        "fim_rows_val": sum(r["val"] for r in fim),
        "tokens_est_chars_over_3": est_tokens,
        "schema_example": render_record(trained[0], 0, "full") if trained else "",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    print(
        f"[{time.time()-t0:.0f}s] species trained {len(trained)} (val {len(val_sp)}), untouched {len(untouched)}; "
        f"bundles {len(plain)}, fim rows {len(fim)} {dict(by_kind)}; ~{est_tokens/1e6:.1f} M tokens (chars/3)",
        flush=True,
    )
    print(man["schema_example"])
    if args.dry_run:
        return 0
    docs = os.path.join(args.root, "stage2", "docs")
    os.makedirs(docs, exist_ok=True)
    with open(os.path.join(docs, "xml_plain.jsonl"), "w", encoding="utf-8") as fh:
        for d in plain:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    with open(os.path.join(docs, "xml_fim.jsonl"), "w", encoding="utf-8") as fh:
        for r in fim:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    json.dump(
        {"trained": [asdict(r) for r in trained], "untouched": [asdict(r) for r in untouched]},
        open(os.path.join(docs, "xml_records.json"), "w"),
        ensure_ascii=False,
        indent=0,
    )
    json.dump(man, open(os.path.join(args.root, "stage2", "docs_manifest.json"), "w"), indent=1)
    print(f"-> {docs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
