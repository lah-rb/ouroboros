#!/usr/bin/env python3
"""Pack the §22 stage-2 stream: XML fill examples + plain XML bundles + carried
prose + replay (rock venv; reuses package.Packer and package_synth's budgeted
carry).

STREAM by realised tokens (--mix xml,paper,other,replay = 65,20,7,8):
  * xml_fim   — corpus_xml rows, prompt = fim_wrap(prefix, "", suffix) ending
                in <|fim_middle|>, completion = the blank. Default --fim-loss
                middle: Packer.add_example masks the prompt and trains the
                middle + EOS only (the blank is 3–40 of ~115 tokens; full
                loss would spend most of the gradient re-teaching the prefix
                and a context-free suffix — forward exposure, and a 20–30×
                dilution of the conditioning signal the stage exists to
                test). --fim-loss full trains every token (stage 0's regime)
                for a follow-up. Prompt and completion are tokenised
                VERBATIM — package.stage2's leading-space insertion does not
                apply: the sentinel is a hard token boundary.
  * xml_plain — the catalogue bundles ([source: reference/xml] tag inside),
                whole documents, full loss.
  * paper     — carried stage-1 paper prose (papers, supplements, binder),
                explicit share (§21c iv: the synth streams held 0.1 % paper
                prose and paid 5–6 % on the paper val loss).
  * other     — carried reference frames, hom, webmineral, mindat, packs.
  * replay    — v4 replay documents.
Carried prose is tagged and formula-normalised at packing (package_synth).

VAL: val-xml_fim (masked examples of the val species), val-xml_plain (their
bundles), and symlinks from --stage1 for val-paper_markdown, val-replay,
val-reference and val-fim (the sentinel skill's own loss).

  ./.venv/bin/python package_xml.py --corpus ~/corpora/rock-olmo-training/v6/stage2 \\
      --stage1 ~/corpora/rock-olmo-training/v6/stage1 --replay ~/corpora/rock-olmo-training/v4/replay/replay.jsonl \\
      --mix 65,20,7,8 --fim-loss middle
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

from package import (  # noqa: E402
    SEQ,
    Packer,
    _git_commit,
    load_tokenizer,
    read_jsonl,
    tokenize_docs,
    write_shards,
)
from package_synth import pick_to_token_budget, tagged  # noqa: E402

PAPER_FILES = ("papers.jsonl",)
OTHER_FILES = ("reference.jsonl", "hom.jsonl", "webmineral.jsonl", "mindat_prose.jsonl", "packs.jsonl")
BORROWED_VAL = ("paper_markdown", "replay", "reference", "fim")


def parse_mix(text: str) -> tuple[int, int, int, int]:
    parts = [int(x) for x in text.split(",")]
    if len(parts) != 4 or sum(parts) != 100:
        raise SystemExit("--mix must be four integers summing to 100 (xml,paper,other,replay)")
    return parts[0], parts[1], parts[2], parts[3]


def _pool(stage1: str, files: tuple[str, ...]) -> list[dict]:
    paths = [os.path.join(stage1, "docs", f) for f in files if os.path.exists(os.path.join(stage1, "docs", f))]
    return [d for d in read_jsonl(paths) if not d.get("val")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="stage-2 dir (docs/xml_fim.jsonl, docs/xml_plain.jsonl)")
    ap.add_argument("--stage1", default=os.path.expanduser("~/corpora/rock-olmo-training/v6/stage1"))
    ap.add_argument("--replay", default=os.path.expanduser("~/corpora/rock-olmo-training/v4/replay/replay.jsonl"))
    ap.add_argument("--mix", default="65,20,7,8")
    ap.add_argument("--fim-loss", choices=("middle", "full"), default="middle")
    ap.add_argument("--seq", type=int, default=SEQ)
    ap.add_argument("--seed", type=int, default=20260919)
    args = ap.parse_args()
    mix = parse_mix(args.mix)
    t0 = time.time()
    rng = random.Random(args.seed)
    tok = load_tokenizer()
    docs_dir = os.path.join(args.corpus, "docs")

    rows = read_jsonl([os.path.join(docs_dir, "xml_fim.jsonl")])
    bundles = read_jsonl([os.path.join(docs_dir, "xml_plain.jsonl")])
    P = tok([r["prompt"] for r in rows], add_special_tokens=False)["input_ids"]
    C = tok([r["completion"] for r in rows], add_special_tokens=False)["input_ids"]
    train_rows = [i for i, r in enumerate(rows) if not r.get("val")]
    val_rows = [i for i, r in enumerate(rows) if r.get("val")]
    fim_tokens = sum(len(P[i]) + len(C[i]) + 1 for i in train_rows)
    bundle_paras = tokenize_docs(tok, bundles)
    train_bundles = [i for i, b in enumerate(bundles) if not b.get("val")]
    val_bundles = [i for i, b in enumerate(bundles) if b.get("val")]
    plain_tokens = sum(sum(len(p) for p in bundle_paras[i]) + 1 for i in train_bundles)
    xml_tokens = fim_tokens + plain_tokens
    total = xml_tokens / (mix[0] / 100.0)
    budgets = {
        "xml_fim": fim_tokens,
        "xml_plain": plain_tokens,
        "paper": int(total * mix[1] / 100.0),
        "other": int(total * mix[2] / 100.0),
        "replay": int(total * mix[3] / 100.0),
        "total": int(total),
    }
    print(
        f"[{time.time()-t0:.0f}s] xml: {len(train_rows):,} fim rows ({fim_tokens:,} tokens, loss={args.fim_loss}) + "
        f"{len(train_bundles):,} bundles ({plain_tokens:,}); budgets {budgets}",
        flush=True,
    )

    paper_docs, paper_tokens = pick_to_token_budget(_pool(args.stage1, PAPER_FILES), budgets["paper"], rng, tok, normalise=True)
    other_docs, other_tokens = pick_to_token_budget(_pool(args.stage1, OTHER_FILES), budgets["other"], rng, tok, normalise=True)
    replay_pool = [d for d in read_jsonl([args.replay]) if not d.get("val")] if os.path.exists(args.replay) else []
    replay_docs, replay_tokens = pick_to_token_budget(replay_pool, budgets["replay"], rng, tok, normalise=False)
    print(
        f"[{time.time()-t0:.0f}s] carried paper {len(paper_docs):,} docs ({paper_tokens:,}); other {len(other_docs):,} ({other_tokens:,}); replay {len(replay_docs):,} ({replay_tokens:,})",
        flush=True,
    )

    # one shuffled stream of (role, payload)
    stream: list[tuple[str, object]] = (
        [("xml_fim", i) for i in train_rows]
        + [("xml_plain", i) for i in train_bundles]
        + [("paper", d) for d in paper_docs]
        + [("other", d) for d in other_docs]
        + [("replay", d) for d in replay_docs]
    )
    rng.shuffle(stream)
    carried = [(role, d) for role, d in stream if role in ("paper", "other", "replay")]
    carried_paras = tokenize_docs(
        tok,
        [{"doc_id": d["doc_id"], "text": tagged(d, normalise=role != "replay")} for role, d in carried],
    )
    carried_iter = iter(carried_paras)
    full_paras = None
    if args.fim_loss == "full":
        full_paras = tokenize_docs(tok, [{"doc_id": rows[i]["ex_id"], "text": rows[i]["prompt"] + rows[i]["completion"]} for i in train_rows])
        full_by_row = dict(zip(train_rows, full_paras))
    pk = Packer(args.seq)
    tokens_by_role: collections.Counter = collections.Counter()
    by_kind: collections.Counter = collections.Counter()
    dropped = 0
    for role, payload in stream:
        if role == "xml_fim":
            i = payload
            by_kind[rows[i]["kind"]] += 1
            if args.fim_loss == "middle":
                if not pk.add_example(rows[i]["ex_id"], P[i], C[i]):
                    dropped += 1
                    continue
                tokens_by_role[role] += len(P[i]) + len(C[i]) + 1
            else:
                paras = full_by_row[i]
                pk.add_document(rows[i]["ex_id"], paras, atomic=True)
                tokens_by_role[role] += sum(len(p) for p in paras) + 1
        elif role == "xml_plain":
            paras = bundle_paras[payload]
            pk.add_document(bundles[payload]["doc_id"], paras)
            tokens_by_role[role] += sum(len(p) for p in paras) + 1
        else:
            paras = next(carried_iter)
            pk.add_document(payload["doc_id"], paras)
            tokens_by_role[role] += sum(len(p) for p in paras) + 1
    pk.finish()
    os.makedirs(args.corpus, exist_ok=True)
    for old in glob.glob(os.path.join(args.corpus, "*.bin")) + glob.glob(os.path.join(args.corpus, "*.idx.json")):
        if not os.path.islink(old):
            os.remove(old)
    train_stats = write_shards(args.corpus, "train", pk, rng)
    total_real = sum(tokens_by_role.values()) or 1
    shares = {k: round(v / total_real, 4) for k, v in tokens_by_role.items()}
    print(
        f"[{time.time()-t0:.0f}s] train: {train_stats['blocks']} blocks, {total_real:,} tokens, pad {train_stats['pad_fraction']:.2%}, dropped {dropped}; shares {shares}",
        flush=True,
    )

    val_stats = {}
    vp = Packer(args.seq)
    for i in val_rows:
        vp.add_example(rows[i]["ex_id"], P[i], C[i])
    vp.finish()
    val_stats["xml_fim"] = write_shards(args.corpus, "val-xml_fim", vp, rng, shuffle=False)
    vb = Packer(args.seq)
    for i in val_bundles:
        vb.add_document(bundles[i]["doc_id"], bundle_paras[i])
    vb.finish()
    val_stats["xml_plain"] = write_shards(args.corpus, "val-xml_plain", vb, rng, shuffle=False)
    borrowed = []
    for v in BORROWED_VAL:
        ok = True
        for ext in ("bin", "mask.bin", "idx.json"):
            src = os.path.join(args.stage1, f"val-{v}-000.{ext}")
            dst = os.path.join(args.corpus, f"val-{v}-000.{ext}")
            if not os.path.exists(src):
                ok = False
                continue
            if os.path.islink(dst) or os.path.exists(dst):
                os.remove(dst)
            os.symlink(src, dst)
        if ok:
            borrowed.append(v)
    man = {
        "stage": "xml_fill",
        "seq": args.seq,
        "seed": args.seed,
        "git": _git_commit(),
        "fim_loss": args.fim_loss,
        "mix_target": {"xml": mix[0], "paper": mix[1], "other": mix[2], "replay": mix[3]},
        "budgets": budgets,
        "tokens_by_role": dict(tokens_by_role),
        "share_by_role": shares,
        "share_xml": round((tokens_by_role["xml_fim"] + tokens_by_role["xml_plain"]) / total_real, 4),
        "docs": {
            "xml_fim": len(train_rows),
            "xml_plain": len(train_bundles),
            "paper": len(paper_docs),
            "other": len(other_docs),
            "replay": len(replay_docs),
            "val_fim_rows": len(val_rows),
            "val_bundles": len(val_bundles),
        },
        "fim_rows_by_kind": dict(by_kind),
        "dropped_oversize": dropped,
        "train": train_stats,
        "val": val_stats,
        "borrowed_val": borrowed,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(man, open(os.path.join(args.corpus, "manifest.json"), "w"), indent=1)
    print(
        f"[{time.time()-t0:.0f}s] manifest written; val: xml_fim {val_stats['xml_fim']['blocks']} blocks, xml_plain {val_stats['xml_plain']['blocks']}, borrowed {borrowed}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
