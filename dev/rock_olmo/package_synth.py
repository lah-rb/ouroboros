#!/usr/bin/env python3
"""Pack the synthetic-corpus pilot (rock venv; reuses package.Packer).

STREAM = synthetic docs (every variant; val:false) + CARRIED v4 prose + replay,
mixed by TOKENS to --mix (default 60/20/20) and packed as whole documents
(all tokens trained -- pretraining-style knowledge augmentation, Allen-Zhu &
Li's setting), EOS between documents, seeded global shuffle so a fact's
exposures are spread over the run.

CARRIED PROSE, in this order: (1) EVERY v4 reference doc whose fact names a
target OR a control species (carry_facts.json from synth_render.py) -- both
groups keep identical natural exposure, so the manipulation is purely
additive; (2) random stage-1 prose (papers, hom, webmineral, mindat_prose,
packs) up to the carry budget; then (3) replay up to its budget.

EVERY DOCUMENT GETS A SOURCE TAG LINE first: ``[source: synthetic/raman]``,
``[source: reference/rruff]``, ``[source: paper]``, ``[source: replay/pes2o]``
-- Allen-Zhu & Li 3.3: a domain token lets the model down-weight junk and
recovers most of the capacity mixing costs. Carried prose also passes through
formula_norm.normalize_text_formulas here, so v4 artefacts on disk stay
byte-identical.

VAL: val-synth_holdout (held-out framings on trained facts, from
docs/synth_holdout.jsonl) plus, symlinked from v4, val-paper_markdown,
val-replay, val-reference (forgetting) and stage2's val-shapes as
val-shapes_v4 (interference). train_full.py globs val-*.bin.

    ./.venv/bin/python package_synth.py --corpus ~/corpora/rock-olmo-training/v5/synth_pilot \
        [--stage1 .../v4/stage1 --stage2 .../v4/stage2 --replay .../v4/replay/replay.jsonl --mix 60,20,20]
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from formula_norm import normalize_text_formulas  # noqa: E402

PROSE_FILES = (
    "papers.jsonl",
    "hom.jsonl",
    "webmineral.jsonl",
    "mindat_prose.jsonl",
    "packs.jsonl",
)
CHARS_PER_TOKEN = 3.5


def source_tag(d: dict) -> str:
    """The [source: …] tag for a document record."""
    src = str(d.get("source") or "")
    if src.startswith("synthetic/") or src.startswith("replay/"):
        return src
    if src == "reference":
        return "reference/" + str(
            (d.get("provenance") or {}).get("source") or "mixed"
        ).lower().replace(" ", "_")
    return {
        "paper_markdown": "paper",
        "binder_markdown": "paper",
        "pack_prose": "pack",
    }.get(src, src or "unknown")


def tagged(d: dict, *, normalise: bool) -> str:
    text = d["text"]
    if normalise:
        text = normalize_text_formulas(text)
    return f"[source: {source_tag(d)}]\n{text}"


def plan_budgets(synth_tokens: int, mix: tuple[int, int, int]) -> dict[str, int]:
    """Token budgets for carry and replay from the synthetic total and the
    target shares (synthetic, carry, replay)."""
    s, c, r = mix
    total = synth_tokens / (s / 100.0) if s else synth_tokens
    return {
        "synthetic": synth_tokens,
        "carry": int(total * c / 100.0),
        "replay": int(total * r / 100.0),
        "total": int(total),
    }


def est_tokens(d: dict) -> int:
    return int(d.get("tokens_est") or len(d.get("text") or "") / CHARS_PER_TOKEN)


def pick_to_budget(docs: list[dict], budget: int, rng: random.Random) -> list[dict]:
    """Whole documents, shuffled, until the budget is filled (skip oversize)."""
    docs = list(docs)
    rng.shuffle(docs)
    out, used = [], 0
    for d in docs:
        n = est_tokens(d)
        if used + n > budget:
            continue
        out.append(d)
        used += n
        if used >= budget * 0.98:
            break
    return out


def parse_mix(text: str) -> tuple[int, int, int]:
    parts = [int(x) for x in text.split(",")]
    if len(parts) != 3 or sum(parts) != 100:
        raise SystemExit(
            "--mix must be three integers summing to 100 (synthetic,carry,replay)"
        )
    return parts[0], parts[1], parts[2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument(
        "--stage1", default=os.path.expanduser("~/corpora/rock-olmo-training/v4/stage1")
    )
    ap.add_argument(
        "--stage2", default=os.path.expanduser("~/corpora/rock-olmo-training/v4/stage2")
    )
    ap.add_argument(
        "--replay",
        default=os.path.expanduser(
            "~/corpora/rock-olmo-training/v4/replay/replay.jsonl"
        ),
    )
    ap.add_argument("--mix", default="60,20,20")
    ap.add_argument("--seed", type=int, default=20260911)
    args = ap.parse_args()
    from package import (
        SEQ,
        Packer,
        _git_commit,
        load_tokenizer,
        read_jsonl,
        tokenize_docs,
        write_shards,
    )

    mix = parse_mix(args.mix)
    t0 = time.time()
    rng = random.Random(args.seed)
    tok = load_tokenizer()
    docs_dir = os.path.join(args.corpus, "docs")

    synth = [
        d
        for d in read_jsonl(
            sorted(glob.glob(os.path.join(docs_dir, "synthetic_*.jsonl")))
        )
        if not d.get("val")
    ]
    holdout = (
        read_jsonl([os.path.join(docs_dir, "synth_holdout.jsonl")])
        if os.path.exists(os.path.join(docs_dir, "synth_holdout.jsonl"))
        else []
    )
    carry_map = json.load(open(os.path.join(args.corpus, "carry_facts.json")))
    wanted_fact_ids = {fid for v in carry_map["fact_ids"].values() for fid in v}
    synth_tokens = sum(est_tokens(d) for d in synth)
    budgets = plan_budgets(synth_tokens, mix)
    print(
        f"[{time.time()-t0:.0f}s] synthetic {len(synth):,} docs ~{synth_tokens:,} tokens; budgets {budgets}",
        flush=True,
    )

    # carried v4 prose: every reference doc of the 1,000 species, then random prose
    ref_docs = [
        d
        for d in read_jsonl([os.path.join(args.stage1, "docs", "reference.jsonl")])
        if not d.get("val")
    ]
    carried_ref = [
        d
        for d in ref_docs
        if (d.get("provenance") or {}).get("fact_id") in wanted_fact_ids
    ]
    ref_tokens = sum(est_tokens(d) for d in carried_ref)
    prose = [
        d
        for d in read_jsonl(
            [
                os.path.join(args.stage1, "docs", f)
                for f in PROSE_FILES
                if os.path.exists(os.path.join(args.stage1, "docs", f))
            ]
        )
        if not d.get("val")
    ]
    carried_prose = pick_to_budget(prose, max(0, budgets["carry"] - ref_tokens), rng)
    replay_docs = (
        pick_to_budget(
            [d for d in read_jsonl([args.replay]) if not d.get("val")],
            budgets["replay"],
            rng,
        )
        if os.path.exists(args.replay)
        else []
    )
    print(
        f"[{time.time()-t0:.0f}s] carried reference {len(carried_ref):,} docs ~{ref_tokens:,} tokens; prose {len(carried_prose):,}; replay {len(replay_docs):,}",
        flush=True,
    )

    stream = (
        [(d, "synthetic", False) for d in synth]
        + [(d, "carry_reference", True) for d in carried_ref]
        + [(d, "carry_prose", True) for d in carried_prose]
        + [(d, "replay", False) for d in replay_docs]
    )
    rng.shuffle(stream)
    texts = [
        {"doc_id": d["doc_id"], "text": tagged(d, normalise=norm)}
        for d, _, norm in stream
    ]
    per_doc = tokenize_docs(tok, texts)
    pk = Packer(SEQ)
    tokens_by_tag: collections.Counter = collections.Counter()
    tokens_by_role: collections.Counter = collections.Counter()
    docs_by_group: collections.Counter = collections.Counter()
    tokens_by_group: collections.Counter = collections.Counter()
    exposures: collections.Counter = collections.Counter()
    for (d, role, _n), paras in zip(stream, per_doc):
        n = sum(len(p) for p in paras) + 1
        pk.add_document(d["doc_id"], paras)
        tokens_by_tag[source_tag(d)] += n
        tokens_by_role[role] += n
        if role == "synthetic":
            g = (d.get("provenance") or {}).get("group", "?")
            docs_by_group[g] += 1
            tokens_by_group[g] += n
            exposures[(d.get("provenance") or {}).get("fact_id", "?")] += 1
    pk.finish()
    train_stats = write_shards(args.corpus, "train", pk, rng)
    total = sum(tokens_by_role.values()) or 1
    shares = {k: round(v / total, 4) for k, v in tokens_by_role.items()}
    print(
        f"[{time.time()-t0:.0f}s] train: {len(stream):,} docs, {total:,} tokens, {train_stats['blocks']} blocks, pad {train_stats['pad_fraction']:.2%}; shares {shares}",
        flush=True,
    )

    val_stats = {}
    if holdout:
        vt = [
            {"doc_id": d["doc_id"], "text": tagged(d, normalise=False)} for d in holdout
        ]
        vp = Packer(SEQ)
        for d, paras in zip(holdout, tokenize_docs(tok, vt)):
            vp.add_document(d["doc_id"], paras)
        vp.finish()
        val_stats["synth_holdout"] = write_shards(
            args.corpus, "val-synth_holdout", vp, rng, shuffle=False
        )
        print(
            f"[{time.time()-t0:.0f}s] val-synth_holdout: {len(holdout)} docs, {val_stats['synth_holdout']['blocks']} blocks",
            flush=True,
        )
    for v in ("paper_markdown", "replay", "reference"):
        for ext in ("bin", "mask.bin", "idx.json"):
            src = os.path.join(args.stage1, f"val-{v}-000.{ext}")
            dst = os.path.join(args.corpus, f"val-{v}-000.{ext}")
            if os.path.exists(src) and not os.path.exists(dst):
                os.symlink(src, dst)
    for ext in ("bin", "mask.bin", "idx.json"):
        src = os.path.join(args.stage2, f"val-shapes-000.{ext}")
        dst = os.path.join(args.corpus, f"val-shapes_v4-000.{ext}")
        if os.path.exists(src) and not os.path.exists(dst):
            os.symlink(src, dst)

    ex = sorted(exposures.values())
    man = {
        "stage": "synth_pilot",
        "seq": SEQ,
        "seed": args.seed,
        "git": _git_commit(),
        "mix_target": {"synthetic": mix[0], "carry": mix[1], "replay": mix[2]},
        "budgets": budgets,
        "tokens_by_role": dict(tokens_by_role),
        "share_by_role": shares,
        "share_carry_total": round(
            (tokens_by_role["carry_reference"] + tokens_by_role["carry_prose"]) / total,
            4,
        ),
        "tokens_by_tag": dict(tokens_by_tag),
        "docs": {
            "synthetic": len(synth),
            "carry_reference": len(carried_ref),
            "carry_prose": len(carried_prose),
            "replay": len(replay_docs),
            "holdout": len(holdout),
        },
        "docs_by_group": dict(docs_by_group),
        "tokens_by_group": dict(tokens_by_group),
        "exposures_per_fact": {
            "facts": len(ex),
            "min": ex[0] if ex else 0,
            "median": ex[len(ex) // 2] if ex else 0,
            "max": ex[-1] if ex else 0,
        },
        "carry_species": len(carry_map["fact_ids"]),
        "train": train_stats,
        "val": val_stats,
        "borrowed_val": ["paper_markdown", "replay", "reference", "shapes_v4"],
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(man, open(os.path.join(args.corpus, "manifest.json"), "w"), indent=1)
    print(
        f"[{time.time()-t0:.0f}s] manifest written; val sets: {sorted(os.path.basename(p)[4:-8] for p in glob.glob(os.path.join(args.corpus, 'val-*-000.bin')))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
