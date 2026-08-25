"""Emit the training file: interconnects + paper text + reference domains.

WHAT THIS ADDS over the assembler. The assembler produces the
interconnected reference records. This combines them with the paper
corpus, adds the two domains that join on something other than a mineral
species, applies weighting, separates the holdout, and writes shards.

WEIGHTING IS VIEWS-AS-WEIGHT, still. A "4x" dataset is presented four
times only if it can support four DIFFERENT framings; nothing is padded
to hit a multiplier. The weight parameter therefore controls how many
times a source's records enter the shuffle, not how many copies of one
sentence get written — and it is applied per SOURCE, so a species with
two views contributes two, not four.

THE HOLDOUT IS VERIFIED, NOT ASSUMED. A held-out species is only held
out if its name appears in no emitted train record — the paper text
included, since a paper can mention a species it did not measure. The
emitter refuses to write if that check fails, because a leak makes the
eval meaningless in a way that is invisible afterwards.

SSHADE IS A SEPARATE DOMAIN. Molecular ices join on species_inchikey,
not on an IMA mineral name, and their spectral ranges are catalogued in
Hz. Presenting them inside the mineral schema would assert a join that
does not exist, so they get their own record shape.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import json
import os
import random
import re
from typing import Iterator

from assemble import assemble, load_ima, stable_choice
from libs_layer import (
    DEFAULT_T,
    TEMPERATURE_PAIR,
    predict_species_lines,
)
from holdout import elements, select_holdout
from interconnect import view_libs_predicted, view_libs_temperature
from reference_layer import REF_ROOT, load_asd_lines
from training_form import canonicalize

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
DATASET = os.path.join(CORPUS, "databank", "dataset")
MARKDOWN = os.path.join(CORPUS, "databank", "markdown")

#: Speed of light in cm/s — SSHADE catalogues spectral range in Hz, and
#: wavenumber is the unit the field reads. cm-1 = Hz / c. This is an
#: exact conversion of a CATALOGUE FIELD for presentation, not a
#: rewriting of a measured value inside an artifact.
_C_CM_S = 2.99792458e10

#: Per-source weight — how many times a source's records enter the
#: shuffle. Operator-directed: the reference spectral datasets are worth
#: repeated presentation, the paper text is already the bulk.
SOURCE_WEIGHT = {
    "interconnect": 4,
    "sshade_ice": 4,
    "nist_libs": 4,
    "paper_text": 1,
    "paper_markdown": 1,
}


def _slug(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def species_mentioned(text: str, names: dict[str, str]) -> list[str]:
    """Held-out species named in ``text``, matched on WORD BOUNDARIES.

    Bare substring matching flagged "Natrojarosite" as leaking
    "Jarosite" and "Clinoatacamite" as leaking "Atacamite" — different
    species whose names merely contain a held-out one. Excluding those
    would discard legitimate training data for a name collision.
    Boundaries keep the check honest in both directions.
    """
    low = text.lower()
    out = []
    for key, name in names.items():
        if re.search(rf"(?<![a-z]){re.escape(key)}(?![a-z])", low):
            out.append(name)
    return sorted(set(out))


# ── SSHADE ices ──────────────────────────────────────────────────────
def sshade_records() -> list[dict]:
    """Molecular-ice band-list catalogue entries as training records.

    The 68-row bandlist CSV carries species, phase, temperature and
    spectral range. Individual band positions live in VOTable granules
    that were not mirrored, so no band position is asserted here — the
    records state what the catalogue states.
    """
    path = os.path.join(REF_ROOT, "sshade", "sshade_bandlist.csv")
    if not os.path.exists(path):
        return []
    out: list[dict] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            name = _slug(row.get("species_name"))
            if not name:
                continue
            phase = _slug(row.get("alt_target_name")) or name
            cls = [c for c in _slug(row.get("sample_classification")).split("#") if c]
            band = "/".join(c for c in _slug(row.get("waveband")).split("#") if c)
            temp = _slug(row.get("temperature"))
            try:
                lo = float(row["spectral_range_min"]) / _C_CM_S
                hi = float(row["spectral_range_max"]) / _C_CM_S
            except (KeyError, TypeError, ValueError):
                lo = hi = 0.0
            desc = ", ".join(cls[:3])
            rng = f"{lo:.0f}-{hi:.0f} cm-1" if hi > lo else ""
            text = (
                f"{phase} is a {desc} whose absorption band list is catalogued by "
                f"SSHADE over {rng}"
                + (f" in the {band}" if band else "")
                + (f", measured at {temp} K" if temp else "")
                + "."
            )
            out.append(
                {
                    "view": "forward",
                    "domain": "molecular_ice",
                    "species": name,
                    "inchikey": _slug(row.get("species_inchikey")),
                    "text": text,
                    "provenance": {
                        "source": "SSHADE",
                        "granule": _slug(row.get("granule_uid")),
                        "publisher": _slug(row.get("publisher")),
                    },
                    "split": "train",
                    "origin": "reference_only",
                }
            )
            if rng:
                out.append(
                    {
                        "view": "inverse",
                        "domain": "molecular_ice",
                        "species": name,
                        "inchikey": _slug(row.get("species_inchikey")),
                        "text": (
                            f"An absorption band list spanning {rng}"
                            + (f" in the {band}" if band else "")
                            + (f" at {temp} K" if temp else "")
                            + f", for a {desc}, corresponds to {phase}."
                        ),
                        "provenance": {
                            "source": "SSHADE",
                            "granule": _slug(row.get("granule_uid")),
                        },
                        "split": "train",
                        "origin": "reference_only",
                    }
                )
    return out


# ── NIST-derived LIBS expectations ───────────────────────────────────
def libs_records(
    species_formulas: dict[str, str], holdout: set[str], top: int = 6
) -> list[dict]:
    """species -> the LIBS lines it should show, DERIVED from its formula.

    This is the mindat bridge: a mineral's formula gives its elements, and
    the elements give their emission lines. The derivation is the lesson —
    it is how LIBS identification actually works — so records say they are
    derived rather than implying the lines were measured on the mineral.

    Now carries RELATIVE INTENSITIES as well as positions. The previous
    version listed wavelengths only, which does not say which line is
    strong, and strength is most of what makes a LIBS spectrum usable.
    The literature we collected cannot supply it — of 126 LIBS papers in
    the databank, essentially none carry intensity tables — but NIST ASD
    carries the transition probability, upper-level energy and degeneracy
    needed to compute it under LTE. See libs_layer.
    """
    out: list[dict] = []
    for species, formula in sorted(species_formulas.items()):
        if species in holdout:
            continue
        groups = predict_species_lines(formula, DEFAULT_T, max_elements=top)
        if len(groups) < 2:
            continue
        base = {"domain": "libs_derived", "split": "train", "origin": "reference_only"}
        v = view_libs_predicted(species, formula, groups, DEFAULT_T)
        if v:
            out.append({**v, **base})
        # The temperature pair is the part a flat line list cannot teach:
        # the same mineral legitimately gives different relative
        # intensities at different plasma temperatures.
        t_cool, t_hot = TEMPERATURE_PAIR
        vt = view_libs_temperature(
            species,
            formula,
            predict_species_lines(formula, t_cool, max_elements=top),
            predict_species_lines(formula, t_hot, max_elements=top),
            t_cool,
            t_hot,
        )
        if vt:
            out.append({**vt, **base})
    return out


# ── paper text ───────────────────────────────────────────────────────
def paper_records(holdout: set[str]) -> list[dict]:
    """One record per packed paper: its canonical data as prose.

    A paper is dropped entirely if it names a held-out species, because
    the paper's own text would otherwise teach what the eval means to
    test. That is a real cost — a paper mentioning quartz in passing is
    lost too — and it is the price of a clean split.
    """
    lowered = {s.lower(): s for s in holdout}
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(DATASET, "*.json"))):
        if path.endswith("key_registry.json"):
            continue
        try:
            art = json.load(open(path))
        except Exception:
            continue
        data = canonicalize(art.get("data") or {})
        summary = _slug((art.get("review") or {}).get("summary"))
        if not summary:
            continue
        facts = []
        for key, value in data.items():
            if key.endswith("_as_packed") or value in (None, "", [], {}):
                continue
            facts.append(
                f"{key.replace('_', ' ')}: {json.dumps(value, ensure_ascii=False)[:160]}"
            )
        text = summary + ("\n" + "\n".join(facts[:24]) if facts else "")
        # CHECK WHAT IS EMITTED, not a proxy for it. The first version
        # scanned title+data while emitting summary+facts, so a review
        # summary naming a held-out species passed the check and leaked
        # anyway — 23 records, every remaining leak after the sibling and
        # citation fixes. The text the model reads is the only text worth
        # checking.
        leaked = species_mentioned(text + " " + str(art.get("title", "")), lowered)
        out.append(
            {
                "view": "paper",
                "domain": "literature",
                "species": None,
                "text": text,
                "provenance": {
                    "source": "corpus",
                    "paper_key": art.get("paper_key", ""),
                    "identifier": art.get("identifier") or art.get("doi", ""),
                    "license": art.get("license", ""),
                },
                "split": "holdout" if leaked else "train",
                "holdout_species": leaked,
                "origin": "paper_backed",
            }
        )
    return out


# ── leak verification ────────────────────────────────────────────────
def verify_no_leak(records: list[dict], holdout: set[str]) -> list[dict]:
    """Held-out species that appear in a TRAIN record. Must be empty.

    Checks the rendered TEXT, not the metadata, because that is what the
    model actually reads. Substring matching on a species name is
    deliberately blunt: a false alarm costs a record, a missed leak
    costs the eval.
    """
    lowered = {s.lower(): s for s in holdout}
    hits: dict[str, int] = {}
    for rec in records:
        if rec.get("split") != "train":
            continue
        for name in species_mentioned(rec.get("text") or "", lowered):
            hits[name] = hits.get(name, 0) + 1
    return [{"species": k, "train_records": v} for k, v in sorted(hits.items())]


def emit_corpus(
    out_dir: str, shards: int = 8, seed: int = 20260824, include_markdown: bool = True
) -> dict:
    """Assemble every source, weight, verify, shuffle, shard, write."""
    ima = load_ima()
    species_papers = json.load(
        open(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "species_papers.json"
            )
        )
    )["papers"]
    holdout = {
        s for picks in select_holdout(species_papers, ima).values() for s in picks
    }

    inter = assemble()["records"]
    for rec in inter:
        rec.setdefault("domain", "mineral")
        rec["source"] = "interconnect"
    sshade = sshade_records()
    for rec in sshade:
        rec["source"] = "sshade_ice"
    libs = libs_records({s: ima.get(s, "") for s in species_papers}, holdout)
    for rec in libs:
        rec["source"] = "nist_libs"
    papers = paper_records(holdout)
    for rec in papers:
        rec["source"] = "paper_text"
    md = markdown_records(holdout) if include_markdown else []

    everything = inter + sshade + libs + papers + md
    train = [r for r in everything if r.get("split") == "train"]
    held = [r for r in everything if r.get("split") != "train"]

    leaks = verify_no_leak(train, holdout)

    weighted: list[dict] = []
    for rec in train:
        for i in range(SOURCE_WEIGHT.get(rec["source"], 1)):
            copy = dict(rec)
            copy["presentation"] = i
            weighted.append(copy)

    rng = random.Random(seed)
    rng.shuffle(weighted)

    report = {
        "leaks": leaks,
        "holdout_species": sorted(holdout),
        "counts": {
            "train_unweighted": len(train),
            "train_weighted": len(weighted),
            "holdout": len(held),
            "by_source": {
                s: sum(1 for r in train if r["source"] == s)
                for s in sorted({r["source"] for r in train})
            },
            "tokens_weighted_est": sum(len(r["text"]) // 4 for r in weighted),
        },
    }
    if leaks:
        report["written"] = False
        return report

    os.makedirs(out_dir, exist_ok=True)
    for i in range(shards):
        with open(os.path.join(out_dir, f"train-{i:03d}.jsonl"), "w") as fh:
            for rec in weighted[i::shards]:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "holdout.jsonl"), "w") as fh:
        for rec in held:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(report, fh, indent=1)
    report["written"] = True
    return report


# ── full paper markdown ──────────────────────────────────────────────
#: Target chunk size in characters. OLMo 2 1B has a 4,096-token window;
#: at ~4 chars/token a 12k-character chunk leaves headroom for the
#: tokenizer running long on formulae and tables, which it does.
MARKDOWN_CHUNK_CHARS = 12_000

#: A paragraph repeated this many times is paddle degeneration, not
#: emphasis. Known corpus damage (see the qwen/paddle degeneration
#: memory): the extractor occasionally loops a passage, and training on
#: it teaches the loop.
_MAX_PARAGRAPH_REPEATS = 3


def _drop_degenerate(text: str) -> str:
    """Remove paragraphs the extractor duplicated into a loop.

    Cheap and conservative: exact-match repeats of substantial
    paragraphs only. A paper legitimately repeating a short line (a
    table header, a figure label) is untouched.
    """
    seen: dict[str, int] = {}
    kept: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        key = para.strip()
        if len(key) < 120:
            kept.append(para)
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= _MAX_PARAGRAPH_REPEATS:
            kept.append(para)
    return "\n\n".join(kept)


def _chunk(text: str, size: int = MARKDOWN_CHUNK_CHARS) -> Iterator[str]:
    """Split on paragraph boundaries, packing up to ``size``.

    Never mid-sentence: a chunk boundary inside a band list would teach
    a truncated number, which is the one thing this corpus exists not to
    do.
    """
    buf: list[str] = []
    used = 0
    for para in re.split(r"(?<=\n)\n+", text):
        if used + len(para) > size and buf:
            yield "".join(buf)
            buf, used = [], 0
        buf.append(para)
        used += len(para)
    if buf:
        tail = "".join(buf).strip()
        if tail:
            yield tail


def markdown_records(holdout: set[str]) -> list[dict]:
    """Full paper markdown, chunked, held-out papers removed entirely.

    THE WHOLE PAPER GOES OR STAYS. A paper naming a held-out species is
    dropped in full rather than having the offending sentence excised:
    a paper that measured antigorite teaches antigorite throughout, and
    surgical removal leaves the surrounding discussion intact and still
    leaking. That costs real training text — papers mentioning a
    held-out species in passing go too — and it is the price of a split
    that means anything.
    """
    lowered = {s.lower(): s for s in holdout}
    base = os.path.expanduser("~/corpora/ouroboros-spectra/")
    recs: dict[str, dict] = {}
    for fn in ("papers.jsonl", "extraction.jsonl"):
        for line in open(os.path.join(base, "databank", fn)):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            key = row.get("paper_key")
            if key:
                recs.setdefault(key, {}).update(
                    {k: v for k, v in row.items() if v not in (None, "")}
                )
    out: list[dict] = []
    for key, rec in sorted(recs.items()):
        if rec.get("review_status") != "accepted" or not rec.get("md_path"):
            continue
        path = os.path.join(base, rec["md_path"])
        if not os.path.exists(path):
            continue
        try:
            text = open(path, errors="ignore").read()
        except OSError:
            continue
        if not text.strip():
            continue
        leaked = species_mentioned(text, lowered)
        text = _drop_degenerate(text)
        for i, chunk in enumerate(_chunk(text)):
            out.append(
                {
                    "view": "paper_full",
                    "domain": "literature",
                    "species": None,
                    "text": chunk,
                    "provenance": {
                        "source": "corpus_markdown",
                        "paper_key": key,
                        "identifier": rec.get("identifier") or rec.get("doi", ""),
                        "license": rec.get("license", ""),
                        "chunk": i,
                    },
                    "split": "holdout" if leaked else "train",
                    "holdout_species": leaked,
                    "origin": "paper_backed",
                    "source": "paper_markdown",
                }
            )
    return out
