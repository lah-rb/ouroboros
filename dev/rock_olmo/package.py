#!/usr/bin/env python3
"""Tokenise, mix, and pack corpus v4 into fixed-length blocks (rock venv).

WHY PACKING, AND WHY BOUNDARY-AWARE. Run 1 truncated every record at the
window and lost 30 % of the corpus (§19). Packing concatenates documents
with EOS between them and cuts the stream into `seq`-token blocks, so
nothing is lost and no window is wasted. Cuts fall on PARAGRAPH boundaries:
a paragraph that would overflow a block starts the next one and the block is
padded — the same rule as emit._chunk, because a cut inside "464.2" teaches
a truncated number. Cost measured in the manifest as `pad_fraction`; a
single paragraph longer than a block (a giant table) is hard-cut.

MIXING IS BY TOKENS AND BY REPEATS. The renderer labels each document with
`max_repeats`; the mix spec may lower it or cap a source's tokens (replay).
Copies are separate documents spread through the shuffle. The manifest
reports unique and weighted tokens per source and per licence, so the
realised mix is a fact, not an intention.

STAGE 2 uses the same block format with the mask set only on the completion
(and its EOS): examples are prompt+completion+EOS, never split across blocks;
an example longer than a block is dropped and counted. `--carry-docs` pulls
whole stage-1 documents (mask on every token) to keep prose in the anneal.

  ./.venv/bin/python package.py stage1 --docs v4/stage1/docs --replay v4/replay/replay.jsonl --out v4/stage1
  ./.venv/bin/python package.py stage2 --shapes v4/stage2/docs/shapes.jsonl --carry-docs v4/stage1/docs \\
        --carry-tokens 3000000 --replay v4/replay/replay.jsonl --replay-tokens 1000000 --out v4/stage2
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import subprocess
import time

import numpy as np

from packed_dataset import EOS_ID, PAD_ID

TOK = os.path.expanduser("~/models/OLMo-2-0425-1B")
SEQ = 4096
BLOCKS_PER_SHARD = 2048  # 2048 x 4096 x 4 B = 32 MiB per shard


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def load_tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(TOK)


def read_jsonl(paths: list[str]) -> list[dict]:
    out = []
    for p in paths:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


# ── tokenisation ─────────────────────────────────────────────────────
def paragraphs(text: str) -> list[str]:
    return [p for p in text.split("\n\n") if p.strip()]


def tokenize_docs(tok, docs: list[dict], *, batch: int = 256) -> list[list[list[int]]]:
    """Per document: a list of paragraph token lists (paragraph separators kept
    by re-attaching "\\n\\n" to every paragraph but the last)."""
    flat: list[str] = []
    owner: list[tuple[int, int]] = []
    for di, d in enumerate(docs):
        paras = paragraphs(d["text"])
        for pi, p in enumerate(paras):
            flat.append(p + ("\n\n" if pi < len(paras) - 1 else ""))
            owner.append((di, pi))
    per_doc: list[list[list[int]]] = [[] for _ in docs]
    for i in range(0, len(flat), batch):
        enc = tok(flat[i : i + batch], add_special_tokens=False)["input_ids"]
        for (di, _), ids in zip(owner[i : i + batch], enc):
            per_doc[di].append(ids)
    return per_doc


# ── packing ──────────────────────────────────────────────────────────
class Packer:
    def __init__(self, seq: int = SEQ):
        self.seq = seq
        self.blocks: list[np.ndarray] = []
        self.masks: list[np.ndarray] = []
        self.spans: list[list[tuple[str, int, int]]] = []
        self._ids: list[int] = []
        self._mask: list[int] = []
        self._spans: list[tuple[str, int, int]] = []
        self.pad_tokens = 0
        self.hard_cuts = 0
        self.atomic_overflow = 0

    def _flush(self) -> None:
        if not self._ids:
            return
        n = len(self._ids)
        pad = self.seq - n
        self.pad_tokens += pad
        self.blocks.append(np.array(self._ids + [PAD_ID] * pad, dtype="<u4"))
        self.masks.append(np.array(self._mask + [0] * pad, dtype="u1"))
        self.spans.append(list(self._spans))
        self._ids, self._mask, self._spans = [], [], []

    def _append(self, ids: list[int], mask_val: int, doc_id: str) -> None:
        start = len(self._ids)
        self._ids.extend(ids)
        self._mask.extend([mask_val] * len(ids))
        self._spans.append((doc_id, start, len(self._ids)))

    def add_document(
        self,
        doc_id: str,
        paras: list[list[int]],
        *,
        mask_val: int = 1,
        atomic: bool = False,
    ) -> None:
        """Paragraph-bounded append with EOS after the last paragraph.

        `atomic`: a document that fits in one block is never split across
        two — the block is flushed first when the remainder is too small
        (the `add_example` rule). A fill-in-the-middle document is only
        useful whole: its middle sits at the END, after the sentinels, so
        a middle trained in the next block has no prefix or suffix to
        condition on. Measured on the 2026-09-19 smoke: 26 % of FIM
        documents straddled a block boundary under the paragraph rule.
        A document longer than a block falls back to the paragraph rule
        and is counted in `atomic_overflow`."""
        pieces = list(paras)
        if pieces:
            pieces[-1] = pieces[-1] + [EOS_ID]
        else:
            pieces = [[EOS_ID]]
        if atomic:
            total = sum(len(p) for p in pieces)
            if total <= self.seq:
                if len(self._ids) + total > self.seq:
                    self._flush()
            else:
                self.atomic_overflow += 1
        for ids in pieces:
            if len(ids) > self.seq:
                self.hard_cuts += 1
                for j in range(0, len(ids), self.seq):
                    chunk = ids[j : j + self.seq]
                    if len(self._ids) + len(chunk) > self.seq:
                        self._flush()
                    self._append(chunk, mask_val, doc_id)
                    if len(self._ids) == self.seq:
                        self._flush()
                continue
            if len(self._ids) + len(ids) > self.seq:
                self._flush()
            self._append(ids, mask_val, doc_id)
            if len(self._ids) == self.seq:
                self._flush()

    def add_example(
        self, ex_id: str, prompt_ids: list[int], completion_ids: list[int]
    ) -> bool:
        """Stage-2 example: prompt (masked) + completion + EOS (trained), never split."""
        total = len(prompt_ids) + len(completion_ids) + 1
        if total > self.seq:
            return False
        if len(self._ids) + total > self.seq:
            self._flush()
        self._append(prompt_ids, 0, ex_id)
        self._append(completion_ids + [EOS_ID], 1, ex_id)
        if len(self._ids) == self.seq:
            self._flush()
        return True

    def finish(self) -> None:
        self._flush()


def write_shards(
    out_dir: str,
    prefix: str,
    packer: Packer,
    rng: random.Random,
    *,
    shuffle: bool = True,
) -> dict:
    order = list(range(len(packer.blocks)))
    if shuffle:
        rng.shuffle(order)
    n_written = 0
    for s in range(0, len(order), BLOCKS_PER_SHARD):
        idx = order[s : s + BLOCKS_PER_SHARD]
        base = os.path.join(out_dir, f"{prefix}-{s // BLOCKS_PER_SHARD:03d}")
        np.stack([packer.blocks[i] for i in idx]).tofile(base + ".bin")
        np.stack([packer.masks[i] for i in idx]).tofile(base + ".mask.bin")
        json.dump([packer.spans[i] for i in idx], open(base + ".idx.json", "w"))
        n_written += len(idx)
    return {
        "blocks": n_written,
        "tokens": n_written * packer.seq,
        "pad_tokens": packer.pad_tokens,
        "pad_fraction": round(packer.pad_tokens / max(1, n_written * packer.seq), 4),
        "hard_cuts": packer.hard_cuts,
        "atomic_overflow": packer.atomic_overflow,
    }


# ── mixing ───────────────────────────────────────────────────────────
def expand_repeats(
    docs: list[dict], per_doc_tokens: list[int], mix: dict, rng: random.Random
) -> tuple[list[int], dict]:
    """Indices of documents to pack (with copies), honouring per-source repeats
    and optional token caps. Returns (indices, per-source accounting)."""
    by_source: dict[str, list[int]] = collections.defaultdict(list)
    for i, d in enumerate(docs):
        by_source[d["source"]].append(i)
    acct: dict[str, dict] = {}
    indices: list[int] = []
    for source, idxs in sorted(by_source.items()):
        spec = mix.get(source, {})
        repeats = int(spec.get("repeats", docs[idxs[0]].get("max_repeats", 1)))
        cap = spec.get("cap_tokens")
        rng.shuffle(idxs)
        chosen = idxs
        if cap is not None:
            chosen, used = [], 0
            for i in idxs:
                if used + per_doc_tokens[i] > cap:
                    break
                chosen.append(i)
                used += per_doc_tokens[i]
        uniq = sum(per_doc_tokens[i] for i in chosen)
        for _ in range(max(1, repeats)):
            indices.extend(chosen)
        acct[source] = {
            "docs": len(chosen),
            "docs_available": len(idxs),
            "unique_tokens": uniq,
            "repeats": repeats,
            "weighted_tokens": uniq * max(1, repeats),
            "cap_tokens": cap,
        }
    rng.shuffle(indices)
    return indices, acct


def default_mix_stage1() -> dict:
    # repeats default to each document's max_repeats (binder 4, sheets 3, else 1)
    return {}


ATOMIC_SOURCE_PREFIXES = ("fim/",)


def _atomic(doc: dict) -> bool:
    """Fill-in-the-middle documents are packed whole (see Packer.add_document)."""
    return str(doc.get("source", "")).startswith(ATOMIC_SOURCE_PREFIXES)


def first_fit_bins(lengths: list[int], seq: int, window: int = 256) -> list[list[int]]:
    """Bin the atomic documents (indices into `lengths`) into blocks of `seq`
    tokens: first fit over a window of open bins, the oldest bin closing when
    none fits. Sequential atomic packing padded 15.5 % of the stage-0 pack
    (two ~2k-token documents per block, the third never fitting); first fit
    over 256 open bins brings that to a few percent. A document longer than
    `seq` gets a bin of its own (the Packer hard-cuts it by paragraphs)."""
    bins: list[list[int]] = []
    open_bins: list[tuple[int, int]] = []  # (remaining, bin index), oldest first
    for i, n in enumerate(lengths):
        if n > seq:
            bins.append([i])
            continue
        for k, (rem, b) in enumerate(open_bins):
            if n <= rem:
                bins[b].append(i)
                if rem - n <= 0:
                    open_bins.pop(k)
                else:
                    open_bins[k] = (rem - n, b)
                break
        else:
            bins.append([i])
            open_bins.append((seq - n, len(bins) - 1))
            if len(open_bins) > window:
                open_bins.pop(0)
    return bins


# ── commands ─────────────────────────────────────────────────────────
def stage1(args) -> None:
    t0 = time.time()
    tok = load_tokenizer()
    rng = random.Random(args.seed)
    docs = read_jsonl(sorted(glob.glob(os.path.join(args.docs, "*.jsonl"))))
    if args.replay:
        docs += read_jsonl([args.replay])
    if args.limit:
        rng.shuffle(docs)
        docs = docs[: args.limit]
    print(f"[{time.time()-t0:4.0f}s] {len(docs)} documents; tokenising", flush=True)
    per_doc = tokenize_docs(tok, docs)
    per_doc_tokens = [sum(len(p) for p in paras) + 1 for paras in per_doc]  # +EOS
    mix = json.load(open(args.mix)) if args.mix else {}
    if args.replay_tokens:
        for src in {d["source"] for d in docs if d["source"].startswith("replay/")}:
            mix.setdefault(src, {})[
                "cap_tokens"
            ] = None  # replay is pre-budgeted by replay.py; cap per subset below if asked
    train_idx = [i for i, d in enumerate(docs) if not d.get("val")]
    val_by_source: dict[str, list[int]] = collections.defaultdict(list)
    for i, d in enumerate(docs):
        if d.get("val"):
            val_by_source[d["source"].split("/")[0]].append(i)
    order, acct = expand_repeats(
        [docs[i] for i in train_idx], [per_doc_tokens[i] for i in train_idx], mix, rng
    )
    order = [train_idx[j] for j in order]
    print(
        f"[{time.time()-t0:4.0f}s] packing {len(order)} document instances", flush=True
    )
    pk = Packer(args.seq)
    # atomic (fim/*) documents are bin-packed first fit into whole blocks; the
    # rest follow the paragraph rule. write_shards shuffles the blocks, so the
    # two populations still interleave across the run.
    atomic_idx = [i for i in order if _atomic(docs[i])]
    for b in first_fit_bins([per_doc_tokens[i] for i in atomic_idx], args.seq):
        for j in b:
            pk.add_document(docs[atomic_idx[j]]["doc_id"], per_doc[atomic_idx[j]], atomic=True)
        pk.finish()
    for i in order:
        if not _atomic(docs[i]):
            pk.add_document(docs[i]["doc_id"], per_doc[i])
    pk.finish()
    os.makedirs(args.out, exist_ok=True)
    for old in glob.glob(os.path.join(args.out, "*.bin")) + glob.glob(
        os.path.join(args.out, "*.idx.json")
    ):
        os.remove(old)
    train_stats = write_shards(args.out, "train", pk, rng)
    val_stats = {}
    for src, idxs in sorted(val_by_source.items()):
        vp = Packer(args.seq)
        for i in idxs:
            vp.add_document(docs[i]["doc_id"], per_doc[i], atomic=_atomic(docs[i]))
        vp.finish()
        val_stats[src] = write_shards(args.out, f"val-{src}", vp, rng, shuffle=False)
    lic = collections.Counter()
    for i in order:
        lic[docs[i].get("license") or "unknown"] += per_doc_tokens[i]
    weighted = sum(a["weighted_tokens"] for a in acct.values())
    man = {
        "stage": 1,
        "seq": args.seq,
        "seed": args.seed,
        "git": _git_commit(),
        "tokenizer": TOK,
        "docs": len(docs),
        "train": train_stats,
        "val": val_stats,
        "sources": {
            s: {**a, "share": round(a["weighted_tokens"] / max(1, weighted), 4)}
            for s, a in acct.items()
        },
        "license_tokens": dict(lic),
        "weighted_tokens": weighted,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(man, open(os.path.join(args.out, "manifest.json"), "w"), indent=1)
    print(
        f"[{time.time()-t0:4.0f}s] STAGE 1: {train_stats['blocks']} blocks = {train_stats['tokens']:,} tokens (pad {train_stats['pad_fraction']:.2%}, hard cuts {train_stats['hard_cuts']}); val {sum(v['blocks'] for v in val_stats.values())} blocks"
    )
    for s, a in sorted(
        man["sources"].items(), key=lambda kv: -kv[1]["weighted_tokens"]
    ):
        print(
            f"  {s:18s} docs={a['docs']:6d} unique={a['unique_tokens']:12,d} x{a['repeats']} = {a['weighted_tokens']:12,d} ({100*a['share']:4.1f}%)"
        )


def stage2(args) -> None:
    t0 = time.time()
    tok = load_tokenizer()
    rng = random.Random(args.seed)
    rows = read_jsonl([args.shapes])
    print(
        f"[{time.time()-t0:4.0f}s] {len(rows)} shaped examples; tokenising", flush=True
    )
    P = tok([r["prompt"] for r in rows], add_special_tokens=False)["input_ids"]
    C = tok(
        [
            (
                " " + r["completion"].lstrip()
                if not r["completion"].startswith(" ")
                else r["completion"]
            )
            for r in rows
        ],
        add_special_tokens=False,
    )["input_ids"]
    train = [i for i, r in enumerate(rows) if not r.get("val")]
    val = [i for i, r in enumerate(rows) if r.get("val")]
    rng.shuffle(train)
    pk = Packer(args.seq)
    dropped = 0
    for i in train:
        if not pk.add_example(f"{rows[i]['fact_id']}#f{rows[i]['frame']}", P[i], C[i]):
            dropped += 1
    shape_tokens = sum(len(P[i]) + len(C[i]) + 1 for i in train)
    # carried stage-1 prose + replay, whole documents, budgeted by tokens
    carry_acct = {}
    for label, path_glob, budget in (
        (
            "carry",
            os.path.join(args.carry_docs, "*.jsonl") if args.carry_docs else "",
            args.carry_tokens,
        ),
        ("replay", args.replay or "", args.replay_tokens),
    ):
        if not path_glob or not budget:
            continue
        docs = [d for d in read_jsonl(sorted(glob.glob(path_glob))) if not d.get("val")]
        rng.shuffle(docs)
        used = 0
        chosen = []
        for d in docs:
            est = int(len(d["text"]) / 3.5)
            if used + est > budget:
                continue
            chosen.append(d)
            used += est
            if used >= budget * 0.98:
                break
        per_doc = tokenize_docs(tok, chosen)
        toks = 0
        for d, paras in zip(chosen, per_doc):
            pk.add_document(d["doc_id"], paras)
            toks += sum(len(p) for p in paras) + 1
        carry_acct[label] = {"docs": len(chosen), "tokens": toks, "budget": budget}
    pk.finish()
    os.makedirs(args.out, exist_ok=True)
    for old in glob.glob(os.path.join(args.out, "*.bin")) + glob.glob(
        os.path.join(args.out, "*.idx.json")
    ):
        os.remove(old)
    train_stats = write_shards(args.out, "train", pk, rng)
    vp = Packer(args.seq)
    for i in val:
        vp.add_example(f"{rows[i]['fact_id']}#f{rows[i]['frame']}", P[i], C[i])
    vp.finish()
    val_stats = {"shapes": write_shards(args.out, "val-shapes", vp, rng, shuffle=False)}
    total = shape_tokens + sum(a["tokens"] for a in carry_acct.values())
    man = {
        "stage": 2,
        "seq": args.seq,
        "seed": args.seed,
        "git": _git_commit(),
        "tokenizer": TOK,
        "examples": len(rows),
        "train_examples": len(train) - dropped,
        "dropped_oversize": dropped,
        "shape_tokens": shape_tokens,
        "carry": carry_acct,
        "train": train_stats,
        "val": val_stats,
        "shares": {
            "shapes": round(shape_tokens / max(1, total), 4),
            **{k: round(v["tokens"] / max(1, total), 4) for k, v in carry_acct.items()},
        },
        "by_kind": dict(collections.Counter(rows[i]["kind"] for i in train)),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    json.dump(man, open(os.path.join(args.out, "manifest.json"), "w"), indent=1)
    print(
        f"[{time.time()-t0:4.0f}s] STAGE 2: {train_stats['blocks']} blocks = {train_stats['tokens']:,} tokens; shapes {shape_tokens:,} ({100*man['shares']['shapes']:.0f}%), carry {carry_acct}; dropped {dropped}; pad {train_stats['pad_fraction']:.2%}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("stage1")
    a.add_argument("--docs", required=True)
    a.add_argument("--replay", default="")
    a.add_argument("--replay-tokens", type=int, default=0)
    a.add_argument("--mix", default="")
    a.add_argument("--out", required=True)
    a.add_argument("--seq", type=int, default=SEQ)
    a.add_argument("--seed", type=int, default=20260824)
    a.add_argument("--limit", type=int, default=0)
    a.set_defaults(fn=stage1)
    b = sub.add_parser("stage2")
    b.add_argument("--shapes", required=True)
    b.add_argument("--carry-docs", default="")
    b.add_argument("--carry-tokens", type=int, default=0)
    b.add_argument("--replay", default="")
    b.add_argument("--replay-tokens", type=int, default=0)
    b.add_argument("--out", required=True)
    b.add_argument("--seq", type=int, default=SEQ)
    b.add_argument("--seed", type=int, default=20260824)
    b.set_defaults(fn=stage2)
    args = ap.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
