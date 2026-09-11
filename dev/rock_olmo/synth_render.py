#!/usr/bin/env python3
"""Fill the template bank from the facts layer -> synthetic docs (root venv).

THE FILLER IS THE ONLY PLACE VALUES ENTER. The bank (flows/synth) holds
wording with {slot}s; this script renders every in-scope fact of every
target species through many templates, filling slots from templates.fields
(the fact), synth_variance (a sampled instrument) and the fact graph
(relations, provenance). It re-runs the gate on every template and asserts
per document that every number in the fill survived verbatim, so no
document can state a value the reference data does not hold.

SCOPE PER GROUP (synth_species.json):
  all targets   identity facts: formula, structure
  T_R, T_RL     Raman families (raman_bands, contrastive, polymorph, ir_troughs
                where the species has them); T_RL adds cross_instrument
  T_L, T_RL     LIBS families (libs_lines)
  C             nothing -- controls get only the carried v4 reference docs

EXPOSURES. `--exposures K` templates per fact per direction per variant,
stratified over the kind's families by a stable hash (a fact never renders
twice through one template within a variant); `--variants V` re-picks the
templates and re-draws the instrument, so a fact is seen ~K*2*V times in
distinct wordings (Allen-Zhu & Li's regime; Chang et al.: paraphrase decays
slower than duplication). `--relational R` relational docs per species per
variant. Held-out templates (sha256(template_id) % --holdout-mod == 0) are
never trained; they render once per fact per direction into
docs/synth_holdout.jsonl (val: true) -- the unseen-framing instrument.

    ../../.venv/bin/python synth_render.py --bank ~/corpora/ouroboros-synth/bank \
        --spec ~/corpora/ouroboros-synth/spec/spec.json --species synth_species.json \
        --out ~/corpora/rock-olmo-training/v5/synth_pilot [--exposures 12 --relational 10 --variants 3] \
        [--limit-docs 2000 --dry-run]
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

from agent.actions.synth_gate import (  # noqa: E402
    normalise_slots,
    numbers_survive,
    validate_template,
)
from templates import SYNTH_SLOTS, _libs_body, _and, fields  # noqa: E402
import synth_variance as sv  # noqa: E402

LICENSE = "reference-mixed"
CHARS_PER_TOKEN = 3.5
IDENTITY_KINDS = ("formula", "structure")
RAMAN_KINDS = ("raman_bands", "contrastive", "polymorph", "ir_troughs")
LIBS_KINDS = ("libs_lines",)
RELATIONAL = {
    "group_membership": "structure",
    "polymorph_family": "polymorph",
    "band_neighbourhood": "raman_bands",
    "cross_instrument": "raman_bands",
    "provenance": "raman_bands",
}
SOURCE_OF_KIND = {
    "formula": "synthetic/identity",
    "structure": "synthetic/identity",
    "raman_bands": "synthetic/raman",
    "contrastive": "synthetic/raman",
    "polymorph": "synthetic/raman",
    "ir_troughs": "synthetic/ir",
    "libs_lines": "synthetic/libs",
}


def _h(*parts) -> int:
    return int(
        hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16], 16
    )


# ── bank ─────────────────────────────────────────────────────────────


def load_bank(
    bank_dir: str, spec: dict, near_dup: float = 0.6
) -> tuple[list[dict], collections.Counter]:
    """Accepted rows from templates*.jsonl, re-gated against the spec (defence
    in depth: the bank on disk may predate a gate fix). Returns rows, reasons."""
    rows: list[dict] = []
    reasons: collections.Counter = collections.Counter()
    for path in sorted(glob.glob(os.path.join(bank_dir, "templates*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001 -- a torn tail is skipped
                reasons["unparseable line"] += 1
                continue
            if r.get("kind") not in spec["kinds"]:
                reasons["kind not in spec"] += 1
                continue
            probs = validate_template(
                r,
                spec["kinds"][r["kind"]],
                known_entities=set(spec.get("known_entities") or []),
                forbidden_sigs=set(
                    (spec.get("forbidden_signatures") or {}).get(r["kind"]) or []
                ),
                near_dup=near_dup,
            )
            if probs:
                for p in probs:
                    reasons[p.split(" (")[0][:40]] += 1
                continue
            rows.append(r)
    # duplicates across files (a CLI bank beside the lane bank)
    seen = set()
    uniq = []
    for r in rows:
        if r["template_id"] in seen:
            reasons["duplicate template_id"] += 1
            continue
        seen.add(r["template_id"])
        uniq.append(r)
    return uniq, reasons


def is_holdout(row: dict, mod: int) -> bool:
    return (
        mod > 0
        and int(hashlib.sha256(row["template_id"].encode()).hexdigest()[:8], 16) % mod
        == 0
    )


def index_bank(
    rows: list[dict], mod: int
) -> dict[str, dict[str, dict[str, list[dict]]]]:
    """kind -> direction -> family -> rows, split train/holdout."""
    idx: dict = {
        "train": collections.defaultdict(
            lambda: collections.defaultdict(lambda: collections.defaultdict(list))
        ),
        "holdout": collections.defaultdict(
            lambda: collections.defaultdict(lambda: collections.defaultdict(list))
        ),
    }
    for r in rows:
        split = "holdout" if is_holdout(r, mod) else "train"
        idx[split][r["kind"]][r["direction"]][r["family"]].append(r)
    return idx


def pick_templates(by_family: dict[str, list[dict]], k: int, *parts) -> list[dict]:
    """k rows stratified over families: rank every row by a stable hash, then
    take round-robin across families in that order."""
    ranked = {
        fam: sorted(rows, key=lambda r: _h(*parts, r["template_id"]))
        for fam, rows in by_family.items()
        if rows
    }
    order = sorted(ranked, key=lambda f: _h(*parts, f))
    out: list[dict] = []
    i = 0
    while len(out) < k and any(ranked.values()):
        fam = order[i % len(order)]
        if ranked[fam]:
            out.append(ranked[fam].pop(0))
        i += 1
        if i > 10_000:
            break
    return out


# ── the fact graph ───────────────────────────────────────────────────


class Graph:
    """What the relational slots are filled from: per-species structure,
    canonical Raman, LIBS, polymorph membership, class/band neighbourhoods,
    a corpus paper title."""

    def __init__(
        self, facts: list, titles: dict[str, str], species_papers: dict[str, list[str]]
    ):
        self.formula: dict[str, str] = {}
        self.structure: dict[str, object] = {}
        self.raman: dict[str, object] = {}
        self.libs: dict[str, object] = {}
        self.members: dict[str, list] = collections.defaultdict(
            list
        )  # species -> polymorph/contrastive facts
        self.extra: dict[str, list] = collections.defaultdict(
            list
        )  # species -> ir/cross facts
        for f in facts:
            if isinstance(f.species, list):
                if f.kind in ("polymorph", "contrastive"):
                    for sp in f.species:
                        self.members[sp].append(f)
                continue
            if f.kind == "formula":
                self.formula[f.species] = f.formula
            elif f.kind == "structure":
                self.structure[f.species] = f
            elif f.kind == "raman_bands" and f.canonical:
                self.raman[f.species] = f
            elif f.kind == "libs_lines":
                self.libs[f.species] = f
            elif f.kind in ("ir_troughs",) and f.canonical:
                self.extra[f.species].append(f)
        self.titles = titles
        self.species_papers = species_papers
        self._by_class: dict[tuple, list[str]] = collections.defaultdict(list)
        for sp, st in self.structure.items():
            key = (
                st.payload.get("strunz_class") or "",
                (st.payload.get("crystal_system") or "").lower(),
            )
            self._by_class[key].append(sp)
        self._strongest: dict[str, float] = {}
        for sp, rf in self.raman.items():
            bands, rel = rf.payload.get("bands_cm1") or [], rf.payload.get("rel") or []
            if bands:
                self._strongest[sp] = (
                    max(zip(bands, rel), key=lambda br: br[1])[0]
                    if rel and len(rel) == len(bands)
                    else bands[0]
                )

    def group(self, sp: str) -> str:
        st = self.structure.get(sp)
        cls = (st.payload.get("strunz_class") if st else "") or ""
        if cls:
            return cls
        from assemble import _implied_class

        return _implied_class(self.formula.get(sp, ""))

    def class_neighbours(self, sp: str, n: int = 3) -> list[str]:
        st = self.structure.get(sp)
        if not st:
            return []
        key = (
            st.payload.get("strunz_class") or "",
            (st.payload.get("crystal_system") or "").lower(),
        )
        cands = [x for x in self._by_class.get(key, []) if x != sp]
        cands.sort(key=lambda x: _h("nbr", sp, x))
        return cands[:n]

    def band_neighbours(self, sp: str, tol: float = 10.0, n: int = 3) -> list[str]:
        b = self._strongest.get(sp)
        if b is None:
            return []
        cands = [x for x, y in self._strongest.items() if x != sp and abs(y - b) <= tol]
        cands.sort(key=lambda x: (abs(self._strongest[x] - b), x))
        return cands[:n]

    def also(self, sp: str, tol: float = 5.0, n: int = 2) -> str:
        names = self.band_neighbours(sp, tol, n)
        return f" (also consistent with {_and(names)})" if names else ""

    def polymorphs(self, sp: str) -> str:
        for f in self.members.get(sp, []):
            if f.kind == "polymorph":
                return _and(
                    [m["species"] for m in f.payload["members"] if m["species"] != sp]
                )
        return ""

    def paper_title(self, sp: str) -> str:
        keys = self.species_papers.get(sp) or []
        for k in sorted(keys, key=lambda x: _h("title", sp, x)):
            t = self.titles.get(k)
            if t:
                return t[:160]
        return ""


def load_titles(databank: str) -> dict[str, str]:
    path = os.path.join(databank, "papers.jsonl")
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if d.get("paper_key") and d.get("title"):
            out[d["paper_key"]] = str(d["title"]).strip()
    return out


# ── filling ──────────────────────────────────────────────────────────


def instrument_fill(
    fact, graph: Graph, rng, want_libs: bool
) -> tuple[dict, dict, list[float]]:
    """Sampled instrument slots for this fact. Returns (slots, instrument
    record, position deltas realised for the Raman list)."""
    slots: dict = {}
    record: dict = {}
    deltas: list[float] = []
    p = fact.payload
    if fact.kind == "raman_bands":
        inst = sv.sample_raman(rng)
        bands, rel = p.get("bands_cm1") or [], p.get("rel") or []
        pairs = sv.perturb_bands(rng, bands, rel, inst)
        slots.update(inst.slots())
        if p.get("laser_nm"):  # the archive's own excitation wins when it is known
            slots["laser"] = f" at {p['laser_nm']} nm excitation"
            slots["laser_nm"] = str(p["laser_nm"])
        slots["peak_list"] = sv.fmt_peaks(pairs)
        if pairs:
            slots["band_strongest"] = f"{max(pairs, key=lambda x: x[1])[0]:g}"
        for b, _r in pairs:
            nearest = min(bands, key=lambda x: abs(x - b)) if bands else b
            deltas.append(abs(b - nearest))
        record = sv.instrument_record(inst)
    elif fact.kind == "libs_lines":
        inst = sv.sample_libs(rng)
        groups = sv.perturb_libs(rng, p.get("groups") or [], inst)
        slots.update(inst.slots())
        slots["libs_lines"] = (
            _libs_body(groups) if groups else _libs_body(p.get("groups") or [])
        )
        lines = sorted(
            (ln for g in groups for ln in g["lines"]), key=lambda ln: -ln["rel"]
        )
        slots["libs_top3"] = (
            ", ".join(f"{ln['nm']:.2f}" for ln in lines[:3]) + " nm" if lines else ""
        )
        record = sv.instrument_record(inst)
    if want_libs and fact.kind == "raman_bands":
        sp = fact.species
        lf = graph.libs.get(sp)
        if lf is not None:
            inst = sv.sample_libs(rng)
            groups = sv.perturb_libs(rng, lf.payload.get("groups") or [], inst)
            slots["libs_lines"] = (
                _libs_body(groups) if groups else _libs_body(lf.payload["groups"])
            )
            lines = sorted(
                (ln for g in groups for ln in g["lines"]), key=lambda ln: -ln["rel"]
            )
            slots["libs_top3"] = (
                ", ".join(f"{ln['nm']:.2f}" for ln in lines[:3]) + " nm"
                if lines
                else ""
            )
            slots["libs_T"] = inst.slots()["libs_T"]
    return slots, record, deltas


def relational_fill(fact, graph: Graph) -> dict:
    sp = fact.species if isinstance(fact.species, str) else fact.species[0]
    out = {
        "group": graph.group(sp),
        "neighbours": (
            _and(graph.class_neighbours(sp))
            if fact.kind == "structure"
            else _and(graph.band_neighbours(sp))
        ),
        "polymorphs": graph.polymorphs(sp),
        "paper_title": graph.paper_title(sp),
        "also": graph.also(sp) if fact.kind == "raman_bands" else "",
        "archive": fact.provenance.get("source", "the reference"),
    }
    if fact.kind == "raman_bands":
        out["locality"] = str(fact.payload.get("locality") or "")
    return out


def render_one(row: dict, fill: dict) -> str | None:
    """prompt + completion with the fill; None when a slot the template needs
    is empty for this fact (the template does not apply to this fact)."""
    try:
        text = normalise_slots(row["prompt"]).format(**fill) + normalise_slots(
            row["completion"]
        ).format(**fill)
    except (KeyError, IndexError, ValueError):
        return None
    for slot in row.get("slots") or []:
        if fill.get(slot, "") == "" and slot not in (
            "also",
            "sample_paren",
            "laser",
            "tail",
            "sg_clause",
            "ritz_note",
            "where",
            "band",
            "temp",
        ):
            return None
    return text


def doc(doc_id: str, source: str, text: str, provenance: dict, val: bool) -> dict:
    return {
        "doc_id": doc_id,
        "source": source,
        "text": text,
        "license": LICENSE,
        "provenance": provenance,
        "val": val,
        "max_repeats": 1,
        "tokens_est": int(len(text) / CHARS_PER_TOKEN),
    }


# ── the render ───────────────────────────────────────────────────────


class Stats:
    def __init__(self) -> None:
        self.docs = collections.Counter()
        self.tokens = collections.Counter()
        self.by_group = collections.Counter()
        self.by_cell = collections.Counter()
        self.exposures = collections.Counter()  # fact_id -> docs (train)
        self.templates_used = set()
        self.skipped = collections.Counter()
        self.deltas: list[float] = []
        self.numbers_failed = 0
        self.holdout = 0

    def summary(self) -> dict:
        ex = sorted(self.exposures.values())
        q = lambda xs, p: (
            xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0.0
        )  # noqa: E731
        return {
            "docs_by_source": dict(self.docs),
            "tokens_by_source": dict(self.tokens),
            "docs_by_group": dict(self.by_group),
            "docs_by_cell": dict(self.by_cell),
            "templates_used": len(self.templates_used),
            "skipped": dict(self.skipped),
            "holdout_docs": self.holdout,
            "numbers_failed": self.numbers_failed,
            "exposures_per_fact": {
                "facts": len(ex),
                "min": ex[0] if ex else 0,
                "median": q(ex, 0.5),
                "max": ex[-1] if ex else 0,
            },
            "raman_delta_quantiles_cm1": (
                {
                    str(p): round(q(sorted(self.deltas), p), 2)
                    for p in (0.5, 0.75, 0.9, 0.95)
                }
                if self.deltas
                else {}
            ),
        }


def facts_in_scope(group: str, sp: str, graph: Graph) -> list:
    out = []
    for kind in IDENTITY_KINDS:
        if kind == "formula" and sp in graph.formula:
            from facts import Fact

            out.append(
                Fact(
                    f"formula:{sp}",
                    "formula",
                    sp,
                    graph.formula[sp],
                    {},
                    {"source": "IMA"},
                    "mindat",
                )
            )
        elif kind == "structure" and sp in graph.structure:
            out.append(graph.structure[sp])
    if group in ("T_R", "T_RL"):
        if sp in graph.raman:
            out.append(graph.raman[sp])
        out += graph.extra.get(sp, [])
        out += [
            f
            for f in graph.members.get(sp, [])
            if f.kind in ("contrastive", "polymorph")
        ]
    if group in ("T_L", "T_RL"):
        if sp in graph.libs:
            out.append(graph.libs[sp])
    return out


def render(
    bank_idx: dict,
    spec: dict,
    graph: Graph,
    groups: dict[str, list[str]],
    *,
    exposures: int,
    relational: int,
    variants: int,
    salt: str = "synth",
    limit_docs: int = 0,
    sink=None,
) -> Stats:
    st = Stats()
    n_out = 0

    def emit(d: dict, holdout: bool = False) -> bool:
        nonlocal n_out
        if sink is not None:
            sink(d)
        n_out += 1
        return not limit_docs or n_out < limit_docs

    for group in ("T_RL", "T_L", "T_R"):
        for sp in groups.get(group, []):
            for fact in facts_in_scope(group, sp, graph):
                kind = fact.kind
                fid = fact.fact_id
                base = {
                    k: ("" if v is None else str(v)) for k, v in fields(fact).items()
                }
                if kind == "structure":
                    base["sg_clause"] = (
                        f" (space group {base['sg']})" if base.get("sg") else ""
                    )
                rel_fill = relational_fill(fact, graph)
                # which families apply to this fact for this group
                relational_ok = {
                    "group_membership": kind == "structure"
                    and bool(rel_fill["neighbours"]),
                    "polymorph_family": kind == "polymorph",
                    "band_neighbourhood": kind == "raman_bands"
                    and bool(rel_fill["neighbours"]),
                    "cross_instrument": kind == "raman_bands"
                    and group == "T_RL"
                    and sp in graph.libs,
                    "provenance": kind == "raman_bands" and bool(base.get("sample")),
                }
                for variant in range(variants):
                    rng = sv.rng_for(fid, salt, variant)
                    inst_slots, inst_rec, deltas = instrument_fill(
                        fact, graph, rng, want_libs=(group == "T_RL")
                    )
                    st.deltas.extend(deltas)
                    fill = dict(base)
                    fill.update(rel_fill)
                    fill.update(
                        {k: v for k, v in inst_slots.items() if v not in (None, "")}
                    )
                    for name in SYNTH_SLOTS:
                        fill.setdefault(name, "")
                    for direction in ("forward", "backward"):
                        fams_all = bank_idx["train"].get(kind, {}).get(direction, {})
                        fams = {
                            f: rows
                            for f, rows in fams_all.items()
                            if f not in RELATIONAL or relational_ok.get(f)
                        }
                        core = {
                            f: rows for f, rows in fams.items() if f not in RELATIONAL
                        }
                        rel = {f: rows for f, rows in fams.items() if f in RELATIONAL}
                        picks = pick_templates(
                            core, exposures, fid, salt, variant, direction
                        )
                        if rel and relational:
                            picks += pick_templates(
                                rel,
                                max(1, relational // 2),
                                fid,
                                salt,
                                variant,
                                direction,
                                "rel",
                            )
                        for row in picks:
                            text = render_one(row, fill)
                            if text is None:
                                st.skipped[f"{kind}/{row['family']}: slot empty"] += 1
                                continue
                            if not numbers_survive(
                                text, {k: fill[k] for k in row.get("slots") or []}
                            ):
                                st.numbers_failed += 1
                                continue
                            src = (
                                "synthetic/relational"
                                if row["family"] in RELATIONAL
                                else SOURCE_OF_KIND.get(kind, "synthetic/other")
                            )
                            d = doc(
                                f"synth:{fid}:{row['template_id']}:v{variant}",
                                src,
                                text,
                                {
                                    "fact_id": fid,
                                    "kind": kind,
                                    "template_id": row["template_id"],
                                    "family": row["family"],
                                    "direction": direction,
                                    "form": row.get("form", "statement"),
                                    "variant": variant,
                                    "group": group,
                                    "species": sp,
                                    "instrument": inst_rec,
                                    "fill_sha": hashlib.sha256(
                                        json.dumps(fill, sort_keys=True).encode()
                                    ).hexdigest()[:12],
                                },
                                False,
                            )
                            st.docs[src] += 1
                            st.tokens[src] += d["tokens_est"]
                            st.by_group[group] += 1
                            st.by_cell[
                                f"{kind}/{row['family']}/{direction}/{row.get('form', 'statement')}"
                            ] += 1
                            st.exposures[fid] += 1
                            st.templates_used.add(row["template_id"])
                            if not emit(d):
                                return st
                        # held-out framings: one per fact per direction, variant 0 only
                        if variant == 0:
                            hold_fams = (
                                bank_idx["holdout"].get(kind, {}).get(direction, {})
                            )
                            hold_fams = {
                                f: rows
                                for f, rows in hold_fams.items()
                                if f not in RELATIONAL or relational_ok.get(f)
                            }
                            for row in pick_templates(
                                hold_fams, 1, fid, salt, "holdout", direction
                            ):
                                text = render_one(row, fill)
                                if text is None or not numbers_survive(
                                    text, {k: fill[k] for k in row.get("slots") or []}
                                ):
                                    continue
                                d = doc(
                                    f"synth_holdout:{fid}:{row['template_id']}",
                                    "synthetic/holdout",
                                    text,
                                    {
                                        "fact_id": fid,
                                        "kind": kind,
                                        "template_id": row["template_id"],
                                        "family": row["family"],
                                        "direction": direction,
                                        "group": group,
                                        "species": sp,
                                    },
                                    True,
                                )
                                st.holdout += 1
                                if not emit(d, holdout=True):
                                    return st
    return st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--bank", default=os.path.expanduser("~/corpora/ouroboros-synth/bank")
    )
    ap.add_argument(
        "--spec", default=os.path.expanduser("~/corpora/ouroboros-synth/spec/spec.json")
    )
    ap.add_argument("--species", default=os.path.join(HERE, "synth_species.json"))
    ap.add_argument(
        "--databank", default=os.path.expanduser("~/corpora/ouroboros-spectra/databank")
    )
    ap.add_argument(
        "--out",
        default=os.path.expanduser("~/corpora/rock-olmo-training/v5/synth_pilot"),
    )
    ap.add_argument("--exposures", type=int, default=12)
    ap.add_argument("--relational", type=int, default=10)
    ap.add_argument("--variants", type=int, default=3)
    ap.add_argument(
        "--holdout-mod",
        type=int,
        default=10,
        help="template_id hash % mod == 0 -> held out (10 = 10%%)",
    )
    ap.add_argument("--limit-docs", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from facts import build_facts

    t0 = time.time()
    spec = json.load(open(args.spec))
    rows, reasons = load_bank(args.bank, spec)
    idx = index_bank(rows, args.holdout_mod)
    n_hold = sum(
        len(r) for k in idx["holdout"].values() for d in k.values() for r in d.values()
    )
    print(
        f"[{time.time()-t0:.0f}s] bank: {len(rows)} templates pass the gate ({n_hold} held out); re-gate rejects {dict(reasons)}"
    )
    facts = build_facts()
    sp_papers = json.load(open(os.path.join(HERE, "species_papers.json")))["papers"]
    graph = Graph(facts, load_titles(args.databank), sp_papers)
    groups = json.load(open(args.species))["groups"]
    print(
        f"[{time.time()-t0:.0f}s] {len(facts):,} facts; groups {[(g, len(v)) for g, v in groups.items()]}"
    )

    docs_dir = os.path.join(args.out, "docs")
    handles: dict[str, object] = {}
    if not args.dry_run:
        os.makedirs(docs_dir, exist_ok=True)
        for old in glob.glob(os.path.join(docs_dir, "synth*.jsonl")):
            os.remove(old)

    def sink(d: dict) -> None:
        if args.dry_run:
            return
        name = (
            "synth_holdout"
            if d["source"] == "synthetic/holdout"
            else "synthetic_" + d["source"].split("/", 1)[1]
        )
        fh = handles.get(name)
        if fh is None:
            fh = handles[name] = open(
                os.path.join(docs_dir, name + ".jsonl"), "w", encoding="utf-8"
            )
        fh.write(json.dumps(d, ensure_ascii=False) + "\n")

    # species -> every fact_id that names it (targets AND controls), so the
    # packager can carry both groups' v4 reference docs (measurement-keyed
    # reference docs carry no species in their provenance).
    carry: dict[str, list[str]] = collections.defaultdict(list)
    wanted = {sp for v in groups.values() for sp in v}
    for f in facts:
        names = [f.species] if isinstance(f.species, str) else list(f.species)
        for sp in names:
            if sp in wanted:
                carry[sp].append(f.fact_id)
    if not args.dry_run:
        os.makedirs(args.out, exist_ok=True)
        json.dump(
            {"groups": groups, "fact_ids": {k: sorted(v) for k, v in carry.items()}},
            open(os.path.join(args.out, "carry_facts.json"), "w"),
            indent=1,
        )
    print(
        f"[{time.time()-t0:.0f}s] carry map: {len(carry)} species, {sum(len(v) for v in carry.values()):,} fact ids"
    )

    st = render(
        idx,
        spec,
        graph,
        groups,
        exposures=args.exposures,
        relational=args.relational,
        variants=args.variants,
        limit_docs=args.limit_docs,
        sink=sink,
    )
    for fh in handles.values():
        fh.close()
    summary = st.summary()
    summary.update(
        {
            "bank_templates": len(rows),
            "bank_rejects": dict(reasons),
            "exposures_arg": args.exposures,
            "relational_arg": args.relational,
            "variants": args.variants,
            "holdout_mod": args.holdout_mod,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "dry_run": args.dry_run,
            "limit_docs": args.limit_docs,
        }
    )
    print(
        json.dumps({k: v for k, v in summary.items() if k != "docs_by_cell"}, indent=1)[
            :4000
        ]
    )
    short = sorted((c for c in st.by_cell.items()), key=lambda kv: kv[1])[:8]
    print("thinnest cells:", short)
    if st.numbers_failed:
        print(
            f"ERROR: {st.numbers_failed} documents lost a number in rendering",
            file=sys.stderr,
        )
        return 1
    if not args.dry_run:
        os.makedirs(args.out, exist_ok=True)
        json.dump(
            summary, open(os.path.join(args.out, "docs_manifest.json"), "w"), indent=1
        )
        print("->", docs_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
