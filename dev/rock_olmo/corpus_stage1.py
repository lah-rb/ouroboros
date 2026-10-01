#!/usr/bin/env python3
"""Render the stage-1 (continued-pretraining) documents for corpus v4 (root venv).

WHAT STAGE 1 IS. Prose, at document granularity, for full-parameter
continued pretraining: the accepted papers with their figure readings
packed back in, the binder shelf, pack prose, every reference fact rendered
as statements in several frames, and the three licence-flagged encyclopaedic
sources (HOM sheets, webmineral pages, mindat prose). Replay is sampled
separately (replay.py). Packaging, repetition and token-weighted mixing are
package.py's job; this module only renders and labels.

ORDER OF OPERATIONS, because the probe set depends on the text:
  1. prose sources that do not depend on the probe set (papers, binder,
     packs, HOM, webmineral, mindat prose) are rendered;
  2. probe_species.verify ranks the probe candidates by how many documents
     name them and keeps the least-exposed hundred (nothing is withheld);
  3. facts are built with NO exclusion and rendered as reference prose — N_REFERENCE_FRAMES distinct statement frames per fact
     (operator ruling 2026-09-07: reference data bears repetition, delivered
     as distinct framings, never copies).

RECORD. {doc_id, source, text, license, provenance, val, max_repeats,
tokens_est}. `val` is a seeded document-level split per source, the same
in both stages: 1 % by default, larger for the small sources
(VAL_FRACTION_BY_SOURCE) so every val-<source> clears the ~20-block floor
§19 found necessary (binder's val set was two documents, mindat's three
blocks). The 1 % split is nested inside the larger ones, so the v4 val
papers stay val. `max_repeats` is the copy count the packager may apply
(binder and the encyclopaedic sheets 2, everything else 1 — §19: four
binder copies memorised, "≤ 2 copies, more distinct documents"; templated
facts are already expanded into distinct frames).

ROOT. Default v4; `--root ~/corpora/rock-olmo-training/v6` renders the v3
pass's stage-1 pile beside it (v4 artefacts stay byte-identical).

Papers: review_status == "accepted" and a markdown path; the English
translation (`md_en_path`) is preferred when it exists, matching the
curator's own view of the paper. Pipeline per paper: emit._drop_degenerate
-> figtext_inline -> emit.normalize_decimals (after inlining, so the comma
evidence bar sees the whole document).

SUPPLEMENTS. A supplement child record (papers.jsonl `record_kind ==
"supplement"`, `supplement_of` = the parent key; spawned by
tools/supplement_records.py) takes the same pipeline but is LABELLED
`supplement_markdown`, carries the parent in its provenance, and takes its
val split from the PARENT key so a paper and its supplementary material
never straddle train and val. Repeat 1: the supplement is more of the
paper, not a reference sheet.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import time

import emit
from figtext_inline import InlineStats, inline_figtext, read_sidecar

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
DEFAULT_ROOT = os.path.expanduser("~/corpora/rock-olmo-training/v4")
ROOT = DEFAULT_ROOT
V4 = ROOT  # historical name, kept for callers
DOCS = os.path.join(ROOT, "stage1", "docs")
SEED = 20260824
VAL_FRACTION = 0.01
# §19: a val set under ~20 blocks is noise; small sources draw more (nested
# in the 1 % split, so a 1 % val document is val at every larger fraction)
VAL_FRACTION_BY_SOURCE = {
    "hom": 0.03,
    "webmineral": 0.02,
    "mindat_prose": 0.08,
    "binder_markdown": 0.10,
}
N_REFERENCE_FRAMES = 4  # distinct statement frames per fact (the "4x")
N_INVERSE_FRAMES = 2
CHARS_PER_TOKEN = 3.5
REPEATS = {
    "paper_markdown": 1,
    "supplement_markdown": 1,
    "binder_markdown": 2,  # v4 ran 4; §19 measured memorisation (val −58 %)
    "pack_prose": 1,
    "reference": 1,
    "hom": 2,  # v4 ran 3
    "webmineral": 2,  # v4 ran 3
    "mindat_prose": 2,  # v4 ran 3
}


def set_root(root: str) -> None:
    """Point the renderer at another corpus root (v6 for the v3 pass)."""
    global ROOT, V4, DOCS
    ROOT = V4 = os.path.expanduser(root)
    DOCS = os.path.join(ROOT, "stage1", "docs")


LICENSE = {
    "hom": "restricted-hom",
    "webmineral": "restricted-webmineral",
    "mindat_prose": "restricted-mindat",
    "reference": "reference-mixed",
    "pack_prose": "corpus",
}


def is_val(doc_id: str, fraction: float = VAL_FRACTION) -> bool:
    return (
        int(hashlib.sha256(f"{SEED}|{doc_id}".encode()).hexdigest()[:8], 16) % 10_000
        < fraction * 10_000
    )


def record(
    doc_id: str,
    source: str,
    text: str,
    license_: str,
    provenance: dict,
    *,
    val_key: str | None = None,
    closed: bool = False,
) -> dict:
    """One rendered document. `val_key` (default: the doc_id) is the string
    the val split is drawn from — a supplement passes its PARENT's doc_id so
    both land on the same side of the split."""
    return {
        "doc_id": doc_id,
        "source": source,
        "text": text,
        "license": license_,
        "provenance": provenance,
        "val": is_val(
            val_key or doc_id, VAL_FRACTION_BY_SOURCE.get(source, VAL_FRACTION)
        ),
        "max_repeats": REPEATS.get(source, 1),
        "tokens_est": int(len(text) / CHARS_PER_TOKEN),
        # From the CLOSED SHELF (tools/closed_shelf.py): owned copies of books
        # that are not openly licensed. Never in a build without --closed-root.
        "closed_material": closed,
    }


def _last_rows(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            k = r.get("paper_key")
            if k:
                out[k] = r
    return out


def merged_databank(corpus: str | None = None) -> dict[str, dict]:
    corpus = corpus or CORPUS
    papers = _last_rows(os.path.join(corpus, "databank", "papers.jsonl"))
    ext_path = os.path.join(corpus, "databank", "extraction.jsonl")
    # A shelf that has not been OCR'd yet has no extraction sidecar.
    ext = _last_rows(ext_path) if os.path.exists(ext_path) else {}
    return {k: {**v, **ext.get(k, {})} for k, v in papers.items()}


# ── renderers ────────────────────────────────────────────────────────
def render_papers(
    db: dict[str, dict],
    *,
    cap_ratio: float = 1.0,
    limit: int = 0,
    corpus: str | None = None,
    closed: bool = False,
) -> tuple[list[dict], dict]:
    """Accepted papers' markdown (figtext inlined). `corpus` defaults to the
    open workspace; `closed=True` renders the CLOSED SHELF at `corpus`: every
    record there is a binder book, doc ids are `closed:<key>`, and each doc is
    marked closed_material."""
    corpus = corpus or CORPUS
    binder = (
        emit.binder_keys(os.path.join(corpus, "databank", "papers.jsonl"))
        if closed
        else emit.binder_keys()
    )
    stats = {
        "papers": 0,
        "supplements": 0,
        "en_md": 0,
        "missing_md": 0,
        "figtext": InlineStats().__dict__.copy(),
        "normalization": collections.Counter(),
    }
    fig = InlineStats()
    out: list[dict] = []
    for key, rec in sorted(db.items()):
        if rec.get("review_status") != "accepted":
            continue
        path = rec.get("md_en_path") or rec.get("md_path") or ""
        if not path:
            continue
        fp = os.path.join(corpus, path) if not os.path.isabs(path) else path
        if not os.path.exists(fp):
            alt = os.path.join(corpus, rec.get("md_path") or "")
            if rec.get("md_path") and os.path.exists(alt):
                fp = alt
            else:
                stats["missing_md"] += 1
                continue
        if rec.get("md_en_path") and fp.endswith(".en.md"):
            stats["en_md"] += 1
        text = open(fp, errors="ignore").read()
        if not text.strip():
            continue
        text = emit._drop_degenerate(text)
        text, st = inline_figtext(
            text, key, read_sidecar(corpus, key), cap_ratio=cap_ratio
        )
        for k, v in st.__dict__.items():
            if isinstance(v, int):
                setattr(fig, k, getattr(fig, k) + v)
        text, norm = emit.normalize_decimals(text)
        for k, v in norm.items():
            if v:
                stats["normalization"][f"docs_{k}"] += 1
                stats["normalization"][f"subs_{k}"] += v
        provenance = {
            "paper_key": key,
            "identifier": rec.get("doi") or rec.get("identifier") or "",
            "english_translation": fp.endswith(".en.md"),
            "figs_inlined": st.inlined,
            "figs_junk": st.junk,
            "figs_echo": st.echo_stripped,
        }
        val_key = None
        if rec.get("record_kind") == "supplement":
            parent = str(rec.get("supplement_of") or "")
            prec = db.get(parent) or {}
            source = "supplement_markdown"
            provenance["supplement_of"] = parent
            provenance["supplement_form"] = rec.get("supplement_form") or ""
            provenance["identifier"] = (
                prec.get("doi") or rec.get("parent_doi") or provenance["identifier"]
            )
            val_key = f"paper:{parent}"
            stats["supplements"] += 1
        else:
            source = "binder_markdown" if key in binder else "paper_markdown"
        if closed:
            provenance["closed_shelf"] = corpus
        out.append(
            record(
                f"closed:{key}" if closed else f"paper:{key}",
                source,
                text,
                rec.get("license") or "unknown",
                provenance,
                val_key=val_key,
                closed=closed,
            )
        )
        stats["papers"] += 1
        if limit and stats["papers"] >= limit:
            break
    stats["figtext"] = {k: v for k, v in fig.__dict__.items() if isinstance(v, int)}
    stats["normalization"] = dict(stats["normalization"])
    return out, stats


def render_packs(corpus: str | None = None, closed: bool = False) -> list[dict]:
    """Pack prose splits WITH its paper (§19: 12 of 13 val papers had their
    pack in train, so the paper val loss read the pack's paraphrase).
    `closed=True` renders the closed shelf's packs at `corpus`."""
    out = []
    dataset_dir = os.path.join(corpus, "databank", "dataset") if corpus else None
    for r in emit.paper_records(set(), dataset_dir=dataset_dir):
        key = (r.get("provenance") or {}).get("paper_key", "")
        out.append(
            record(
                f"closed-pack:{key}" if closed else f"pack:{key}",
                "pack_prose",
                r["text"],
                (r.get("provenance") or {}).get("license") or "unknown",
                {"paper_key": key, **({"closed_shelf": corpus} if closed else {})},
                val_key=f"closed:{key}" if closed else f"paper:{key}",
                closed=closed,
            )
        )
    return out


CLOSED_MARKER = "CLOSED_MATERIAL"  # tools/closed_shelf.py writes it at the shelf root
CONTAINS_CLOSED = (
    "CONTAINS_CLOSED_MATERIAL"  # written into a training root that took any
)


def closed_guard(docs_dir: str, closed_root: str | None) -> None:
    """Refuse a build that would mix closed material in by accident.

    A root rendered once WITH --closed-root keeps closed_*.jsonl in its docs
    dir, and every later build reads every *.jsonl there -- so an "open"
    rebuild of the same root would still train on the closed shelf. An open
    build must use a root that never took closed material. And --closed-root
    must point at a real shelf (its marker), not at an arbitrary directory.
    """
    if closed_root:
        if not os.path.isfile(os.path.join(closed_root, CLOSED_MARKER)):
            raise SystemExit(
                f"--closed-root {closed_root} has no {CLOSED_MARKER} marker; "
                "it is not a closed shelf"
            )
        return
    stale = sorted(
        f
        for f in (os.listdir(docs_dir) if os.path.isdir(docs_dir) else [])
        if f.startswith("closed_") and f.endswith(".jsonl")
    )
    if stale:
        raise SystemExit(
            f"{docs_dir} holds closed-shelf docs ({', '.join(stale)}) from an earlier "
            "--closed-root build: render the open corpus into a root that never took "
            "closed material, or pass --closed-root to build the private variant"
        )


def render_hom() -> list[dict]:
    from hom_text import LICENSE as L, iter_sheets

    return [
        record(
            f"hom:{sp}",
            "hom",
            emit.normalize_decimals(tx)[0],
            L,
            {"species": sp, "file": os.path.basename(p)},
        )
        for sp, tx, p in iter_sheets()
    ]


def render_webmineral() -> list[dict]:
    from webmineral_text import LICENSE as L, iter_pages

    return [
        record(
            f"webmineral:{sp}",
            "webmineral",
            emit.normalize_decimals(tx)[0],
            L,
            {"species": sp, "file": os.path.basename(p)},
        )
        for sp, tx, p in iter_pages()
    ]


def render_mindat_prose() -> list[dict]:
    from mindat_prose import LICENSE as L, iter_paragraphs

    return [
        record(
            f"mindat_prose:{name}",
            "mindat_prose",
            tx,
            L,
            {"species": name, "file": src},
        )
        for name, tx, src in iter_paragraphs()
    ]


def render_reference(exclude: set[str]) -> tuple[list[dict], dict]:
    from facts import build_facts, census
    from templates import pick_frames, render, render_kind

    facts = build_facts(exclude)
    out: list[dict] = []
    for f in facts:
        if f.kind == "pack_fact":
            continue  # stage-2 material; the pack prose already covers it here
        for i, frame in pick_frames(
            f.fact_id, f.kind, N_REFERENCE_FRAMES, questions=False, salt="s1"
        ):
            text, _ = emit.normalize_decimals(render(f, frame)["text"], commas=False)
            out.append(
                record(
                    f"ref:{f.fact_id}:f{i}",
                    "reference",
                    text,
                    LICENSE["reference"],
                    {
                        "fact_id": f.fact_id,
                        "kind": f.kind,
                        "frame": i,
                        "source": f.source,
                        "canonical": f.canonical,
                    },
                )
            )
        if f.kind == "raman_bands" and f.canonical:
            for i, frame in pick_frames(
                f.fact_id, "inverse", N_INVERSE_FRAMES, questions=False, salt="s1inv"
            ):
                text, _ = emit.normalize_decimals(
                    render_kind(f, "inverse", frame)["text"], commas=False
                )
                out.append(
                    record(
                        f"ref:{f.fact_id}:inv{i}",
                        "reference",
                        text,
                        LICENSE["reference"],
                        {
                            "fact_id": f.fact_id,
                            "kind": "inverse",
                            "frame": i,
                            "source": f.source,
                            "canonical": True,
                        },
                    )
                )
    return out, census(facts)


def write(docs: list[dict], name: str) -> str:
    os.makedirs(DOCS, exist_ok=True)
    path = os.path.join(DOCS, f"{name}.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for d in docs:
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    return path


def summarize(docs: list[dict]) -> dict:
    by = collections.defaultdict(
        lambda: {
            "docs": 0,
            "chars": 0,
            "tokens_est": 0,
            "val_docs": 0,
            "weighted_tokens_est": 0,
        }
    )
    for d in docs:
        b = by[d["source"]]
        b["docs"] += 1
        b["chars"] += len(d["text"])
        b["tokens_est"] += d["tokens_est"]
        b["val_docs"] += d["val"]
        b["weighted_tokens_est"] += d["tokens_est"] * d["max_repeats"]
    return dict(by)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("prose", "reference", "all"), default="all")
    ap.add_argument("--limit-papers", type=int, default=0)
    ap.add_argument("--cap-ratio", type=float, default=1.0)
    ap.add_argument(
        "--skip",
        nargs="*",
        default=[],
        help="sources to skip (hom webmineral mindat_prose packs)",
    )
    ap.add_argument(
        "--root",
        default=DEFAULT_ROOT,
        help="corpus root (v4 default; v6 for the v3 pass)",
    )
    ap.add_argument(
        "--closed-root",
        default=None,
        help="ALSO render the closed shelf (tools/closed_shelf.py) into this build: "
        "owned, not-openly-licensed books. Off by default; a build that takes it "
        "is marked CONTAINS_CLOSED_MATERIAL and must not be published",
    )
    args = ap.parse_args()
    set_root(args.root)
    closed_root = os.path.expanduser(args.closed_root) if args.closed_root else None
    closed_guard(DOCS, closed_root)
    t0 = time.time()
    manifest: dict = {
        "seed": SEED,
        "root": ROOT,
        "val_fraction": VAL_FRACTION,
        "val_fraction_by_source": VAL_FRACTION_BY_SOURCE,
        "repeats": REPEATS,
        "sources": {},
    }
    mpath = os.path.join(ROOT, "stage1", "docs_manifest.json")
    if os.path.exists(mpath):
        manifest = json.load(open(mpath))

    if args.phase in ("prose", "all"):
        db = merged_databank()
        papers, pstats = render_papers(
            db, cap_ratio=args.cap_ratio, limit=args.limit_papers
        )
        write(papers, "papers")
        manifest["papers"] = pstats
        print(
            f"[{time.time()-t0:5.0f}s] papers: {len(papers)} docs (en {pstats['en_md']}, figtext inlined {pstats['figtext']['inlined']}, junk {pstats['figtext']['junk']}, echo {pstats['figtext']['echo_stripped']}, capped {pstats['figtext']['capped']})",
            flush=True,
        )
        if "packs" not in args.skip:
            packs = render_packs()
            write(packs, "packs")
            print(f"[{time.time()-t0:5.0f}s] packs: {len(packs)}", flush=True)
        if closed_root:
            cdb = merged_databank(closed_root)
            cpapers, cstats = render_papers(
                cdb, cap_ratio=args.cap_ratio, corpus=closed_root, closed=True
            )
            write(cpapers, "closed_binder")
            cpacks = render_packs(closed_root, closed=True)
            write(cpacks, "closed_packs")
            manifest["closed_root"] = closed_root
            manifest["closed_papers"] = cstats
            with open(os.path.join(ROOT, CONTAINS_CLOSED), "w") as fh:
                fh.write(
                    f"This training root includes closed-shelf material from {closed_root}.\n"
                    "Do not publish it or anything trained on it as open.\n"
                )
            print(
                f"[{time.time()-t0:5.0f}s] CLOSED shelf: {len(cpapers)} binder docs, {len(cpacks)} packs",
                flush=True,
            )
        if "hom" not in args.skip:
            hom = render_hom()
            write(hom, "hom")
            print(f"[{time.time()-t0:5.0f}s] hom: {len(hom)}", flush=True)
        if "webmineral" not in args.skip:
            wm = render_webmineral()
            write(wm, "webmineral")
            print(f"[{time.time()-t0:5.0f}s] webmineral: {len(wm)}", flush=True)
        if "mindat_prose" not in args.skip:
            mp = render_mindat_prose()
            write(mp, "mindat_prose")
            print(f"[{time.time()-t0:5.0f}s] mindat_prose: {len(mp)}", flush=True)
        # probe set verification against everything rendered so far
        import probe_species

        probe = probe_species.verify([DOCS], json.load(open(probe_species.OUT)))
        json.dump(probe, open(probe_species.OUT, "w"), indent=1)
        manifest["probe_species"] = {
            "n": len(probe["species"]),
            "swapped_out": probe.get("swapped_out", {}),
            "swapped_in": probe.get("swapped_in", []),
        }
        print(
            f"[{time.time()-t0:5.0f}s] probe species verified: {len(probe['species'])} (swapped {len(probe.get('swapped_in', []))})",
            flush=True,
        )

    if args.phase in ("reference", "all"):
        import probe_species

        # Everything trains (operator ruling): no species is excluded from the
        # reference facts. The probe set is an EVALUATION list, ranked by
        # exposure, not a holdout.
        ref, fcensus = render_reference(set())
        write(ref, "reference")
        manifest["facts"] = fcensus
        print(
            f"[{time.time()-t0:5.0f}s] reference: {len(ref)} docs from {fcensus['total']} facts",
            flush=True,
        )

    docs = []
    for f in sorted(os.listdir(DOCS)):
        if f.endswith(".jsonl"):
            docs += [
                json.loads(line)
                for line in open(os.path.join(DOCS, f), encoding="utf-8")
            ]
    manifest["sources"] = summarize(docs)
    manifest["license_shares"] = collections.Counter()
    for d in docs:
        manifest["license_shares"][d["license"]] += d["tokens_est"] * d["max_repeats"]
    manifest["license_shares"] = dict(manifest["license_shares"])
    manifest["closed_material_tokens"] = sum(
        d["tokens_est"] * d["max_repeats"] for d in docs if d.get("closed_material")
    )
    manifest["contains_closed_material"] = manifest["closed_material_tokens"] > 0
    json.dump(manifest, open(mpath, "w"), indent=1)
    tot = sum(s["weighted_tokens_est"] for s in manifest["sources"].values())
    print(f"\nSTAGE 1 DOCS: {len(docs)} | weighted tokens (est) {tot:,}")
    for s, v in sorted(
        manifest["sources"].items(), key=lambda kv: -kv[1]["weighted_tokens_est"]
    ):
        print(
            f"  {s:16s} docs={v['docs']:6d} tokens~{v['tokens_est']:11,d} x{REPEATS.get(s,1)} = {v['weighted_tokens_est']:11,d} ({100*v['weighted_tokens_est']/max(tot,1):4.1f}%) val={v['val_docs']}"
        )
    print(f"manifest -> {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
