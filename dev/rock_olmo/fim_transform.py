#!/usr/bin/env python3
"""Fill-in-the-middle corpora for the v3 pass (rock venv: zstandard).

Stage 0 of the v3 plan (operator, 2026-09-19; PROCEDURE.md §22): teach the
1B the three FIM sentinels its tokenizer already reserves (<|fim_prefix|>
100258, <|fim_middle|> 100259, <|fim_suffix|> 100260 — embeddings at
initialisation, norm 0.88 against 10.8 for trained tokens) on text it
already speaks, before any domain data arrives with blanks in it. Bavarian
et al. (2022): FIM is a data transformation; a next-token model learns
infilling from rearranged documents.

Transformation, per document:
  * with probability --fim-rate the document is rearranged, else kept plain;
  * middle drawn from TWO distributions, half each: a SHORT SPAN of 20–200
    characters snapped to whitespace (our downstream blank is an attribute
    value or a band slot), or a UNIFORM split (two random cut points);
  * order PSM or SPM, half each (`fim_wrap` is the one definition):
      PSM  <|fim_prefix|>P<|fim_suffix|>S<|fim_middle|>M
      SPM  <|fim_suffix|>S<|fim_prefix|>P<|fim_middle|>M
    (the packer appends <|endoftext|>; package.py packs fim/* documents
    ATOMICALLY so the middle is never cut from its prefix and suffix).

Two input modes:
  * SHARD mode (default): dolmino-mix-1124 raw shards (pes2o, wiki, dclm;
    ODC-BY), budgeted per subset in tokens (chars/4), clipped to --clip chars
    so two documents fit a 4,096-token block. Every VAL document is written
    TWICE — plain and rearranged — so val-plain and val-fim measure the same
    documents (the smoke's val-plain was 5 blocks). --exclude-jsonl drops any
    document whose dolmino id or first-2000-char sha1 appears in that file:
    replay.py streams the same shards from the head, and the smoke trained on
    3,196 replay documents, 40 of them replay-val.
  * RECORD mode (--record-mode): record-shaped jsonl rows (corpus_stage1
    shape, e.g. v4/replay/replay.jsonl). Every row passes; train rows are
    rearranged with probability --fim-rate and relabelled fim/<subset>; val
    rows are NEVER rearranged, so val-replay stays the plain forgetting
    instrument. This is the stage-1 "keep the sentinel skill alive" share.

Recovery items (probe_fim_recovery.py): drawn from ALL val documents,
class-stratified by where else the blanked token occurs within the window
the model will see —
    copy_suffix  the answer also appears verbatim in the SUFFIX only
                 (the conditioning test: a left-to-right model cannot see it)
    copy_prefix  in the prefix only (a plain LM can copy it — control)
    free         in neither (world knowledge; the old item style)
— half numeric, half capitalised words, at most two per document, no
sentence-initial words. Written as fim_recovery_items.json.

  ./.venv/bin/python fim_transform.py --out ~/corpora/rock-olmo-training/v6/stage0/docs \\
      --tokens pes2o=176000000,wiki=80000000,dclm=64000000 --fim-rate 0.9 --clip 8000 \\
      --exclude-jsonl ~/corpora/rock-olmo-training/v4/replay/replay.jsonl --recovery-n 400
  ./.venv/bin/python fim_transform.py --record-mode --inputs replay=~/corpora/rock-olmo-training/v4/replay/replay.jsonl \\
      --fim-rate 0.33 --out ~/corpora/rock-olmo-training/v6/replay_fim
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import random
import re

RAW = os.path.expanduser("~/corpora/rock-olmo-training/v4/replay/raw/data")
SHARDS = {
    "pes2o": f"{RAW}/pes2o/pes2o-0025.json.gz",
    "wiki": f"{RAW}/wiki/wiki-0001.json.gz",
    "dclm": f"{RAW}/dclm/0246/dclm-0001.json.zst",
}
PRE, SUF, MID = "<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>"
PRE_ID, MID_ID, SUF_ID = 100258, 100259, 100260
ORDERS = ("psm", "spm")
CHARS_PER_TOKEN = 4.0
CLASSES = ("copy_suffix", "copy_prefix", "free")
# capitalised function words are not knowledge; they are excluded as blanks
_STOP = {
    "The", "This", "That", "These", "Those", "There", "They", "Then", "Thus",
    "When", "Where", "Which", "While", "With", "Without", "After", "Before",
    "However", "Although", "Because", "Also", "Here", "Some", "Such", "Many",
    "Most", "More", "Other", "From", "Into", "About", "Over", "Under", "Each",
    "Both", "Since", "Until", "Note", "Table", "Figure", "Section", "Chapter",
    "Their", "What", "Whether", "Therefore", "Moreover", "Furthermore",
    "Finally", "First", "Second", "Third", "Next", "Last", "Only", "Even",
    "Once", "Upon", "Among", "Between", "During", "Through", "Within",
}  # fmt: skip
# two or more digits: "Fig. 1" and "Between 1 February" are not knowledge blanks
_NUM_RE = re.compile(r"(?<![\w.])(\d[\d.,]*\d)(?![\w.])")
_WORD_RE = re.compile(r"\b[A-Z][a-z]{3,}\b")


# ── the grammar ──────────────────────────────────────────────────────
#: sentinel strings per model family (prefix, suffix, middle) — the grammar is the same
SENTINEL_SETS = {
    "olmo": (PRE, SUF, MID),  # OLMo-2 / OLMo-3 (identical tokenizer)
    "starcoder": ("<fim_prefix>", "<fim_suffix>", "<fim_middle>"),  # SantaCoder / StarCoder / StarCoder2
    "qwen": ("<|fim_prefix|>", "<|fim_suffix|>", "<|fim_middle|>"),  # Qwen2.5-Coder (same spelling as OLMo)
    "deepseek": ("<｜fim▁begin｜>", "<｜fim▁end｜>", "<｜fim▁hole｜>"),  # DeepSeek-Coder: begin P end S hole M
}


def fim_wrap(
    prefix: str,
    middle: str,
    suffix: str,
    order: str = "psm",
    *,
    tokens: tuple[str, str, str] = (PRE, SUF, MID),
) -> str:
    """The one place the sentinel grammar is spelled out. `tokens` =
    (prefix, suffix, middle) sentinel strings; default OLMo's."""
    pre, suf, mid = tokens
    if order == "psm":
        return f"{pre}{prefix}{suf}{suffix}{mid}{middle}"
    if order == "spm":
        return f"{suf}{suffix}{pre}{prefix}{mid}{middle}"
    raise ValueError(f"order must be psm or spm, not {order!r}")


def fim_unwrap(body: str) -> tuple[str, str, str, str]:
    """Inverse of fim_wrap: (prefix, middle, suffix, order)."""
    head, middle = body.rsplit(MID, 1)
    if head.startswith(PRE):
        prefix, suffix = head[len(PRE) :].split(SUF, 1)
        return prefix, middle, suffix, "psm"
    if head.startswith(SUF):
        suffix, prefix = head[len(SUF) :].split(PRE, 1)
        return prefix, middle, suffix, "spm"
    raise ValueError("not a FIM body")


# ── transformation ───────────────────────────────────────────────────
def iter_docs(path: str):
    if path.endswith(".zst"):
        import zstandard as zstd

        with open(path, "rb") as fh:
            reader = zstd.ZstdDecompressor().stream_reader(fh)
            for line in io.TextIOWrapper(reader, encoding="utf-8", errors="replace"):
                if line.strip():
                    yield json.loads(line)
    elif path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)
    else:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def _snap(text: str, i: int, forward: bool) -> int:
    """Move a cut to the nearest whitespace so no word is split."""
    n = len(text)
    j = i
    while 0 < j < n and not text[j].isspace():
        j = j + 1 if forward else j - 1
    return max(0, min(n, j))


def split_points(text: str, rng: random.Random) -> tuple[int, int, str]:
    """(a, b, mode): middle = text[a:b]."""
    n = len(text)
    if rng.random() < 0.5:
        # short span, 20–200 chars, anywhere but the very edges
        length = rng.randint(20, 200)
        lo = min(50, n // 4)
        start = rng.randint(lo, max(lo, n - length - 20))
        a = _snap(text, start, False)
        b = _snap(text, min(n, a + length), True)
        mode = "span"
    else:
        hi = max(21, n - 20)
        a, b = sorted(rng.sample(range(20, hi), 2)) if hi - 20 >= 2 else (20, hi)
        a, b = _snap(text, a, False), _snap(text, b, True)
        mode = "uniform"
    if b <= a:
        b = min(n, a + 20)
    return a, b, mode


def transform(text: str, rng: random.Random) -> tuple[str, str]:
    """(rearranged text, "span|uniform/psm|spm")."""
    a, b, mode = split_points(text, rng)
    order = ORDERS[rng.random() >= 0.5]
    return fim_wrap(text[:a], text[a:b], text[b:], order), f"{mode}/{order}"


def subset_of(rec: dict) -> str:
    prov = rec.get("provenance") or {}
    return str(prov.get("subset") or str(rec.get("source", "")).split("/")[-1] or "x")


def transform_record(
    rec: dict,
    rng: random.Random,
    fim_rate: float,
    *,
    val_twins: bool = True,
    fim_clip: int = 0,
    plain_source: str | None = None,
) -> list[dict]:
    """Rows for one record-shaped document.

    val_twins=True (shard mode): a val row yields a plain row AND a
    rearranged twin; val_twins=False (record mode): val rows stay plain.
    `plain_source` keeps the row's own source label for plain rows (record
    mode: replay/pes2o stays replay/pes2o); default "plain/<subset>".
    `fim_clip` clips only the rearranged copy (a middle after the block
    boundary trains against nothing)."""
    text = (rec.get("text") or "").strip()
    subset = subset_of(rec)
    val = bool(rec.get("val"))
    base = {
        k: v
        for k, v in rec.items()
        if k not in ("text", "source", "doc_id", "tokens", "tokens_est", "provenance")
    }
    prov = dict(rec.get("provenance") or {})
    prov["subset"] = subset

    def plain_row() -> dict:
        return {
            "doc_id": rec["doc_id"],
            "source": plain_source or f"plain/{subset}",
            "text": text,
            **base,
            "provenance": {**prov, "mode": "plain"},
        }

    def fim_row(suffix_id: str = "") -> dict:
        t = text[:fim_clip] if fim_clip else text
        body, mode = transform(t, rng)
        return {
            "doc_id": rec["doc_id"] + suffix_id,
            "source": f"fim/{subset}",
            "text": body,
            **base,
            "provenance": {**prov, "mode": mode},
        }

    if val:
        rows = [plain_row()]
        if val_twins:
            rows.append(fim_row("#fim"))
        return rows
    if rng.random() < fim_rate:
        return [fim_row()]
    return [plain_row()]


# ── replay exclusion ─────────────────────────────────────────────────
def _text_hash(text: str) -> str:
    return hashlib.sha1(text.strip()[:2000].encode("utf-8", "replace")).hexdigest()


def load_exclusions(paths: list[str]) -> tuple[set[tuple[str, str]], set[str]]:
    """(subset, dolmino id) pairs and first-2000-char hashes of every row."""
    ids: set[tuple[str, str]] = set()
    hashes: set[str] = set()
    for p in paths:
        for rec in iter_docs(os.path.expanduser(p)):
            did = str((rec.get("provenance") or {}).get("id") or "")
            if did:
                ids.add((subset_of(rec), did))
            hashes.add(_text_hash(rec.get("text") or ""))
    return ids, hashes


# ── recovery items ───────────────────────────────────────────────────
def classify_blank(answer: str, prefix: str, suffix: str) -> str:
    pat = re.compile(rf"(?<![\w.]){re.escape(answer)}(?![\w.])")
    in_p, in_s = bool(pat.search(prefix)), bool(pat.search(suffix))
    if in_s and not in_p:
        return "copy_suffix"
    if in_p and not in_s:
        return "copy_prefix"
    if not in_p and not in_s:
        return "free"
    return "both"


def _sentence_initial(text: str, start: int) -> bool:
    head = text[max(0, start - 3) : start]
    return start == 0 or head.endswith("\n") or bool(re.search(r"[.!?]\s+$", head))


def recovery_items(
    docs: list[dict],
    rng: random.Random,
    n: int,
    classes: tuple[str, ...] = CLASSES,
    *,
    window: int = 1500,
    per_doc: int = 2,
) -> list[dict]:
    """Class-stratified blanks from val documents ({doc_id, subset, text}).
    Buckets: class × numeric, each ceil(n / (2·classes)) items."""
    per_bucket = math.ceil(n / (2 * len(classes)))
    buckets: dict[tuple[str, bool], list[dict]] = {
        (c, num): [] for c in classes for num in (True, False)
    }
    docs = list(docs)
    rng.shuffle(docs)
    for d in docs:
        text = d["text"]
        cands = [(m, True) for m in _NUM_RE.finditer(text)] + [
            (m, False) for m in _WORD_RE.finditer(text) if m.group(0) not in _STOP
        ]
        rng.shuffle(cands)
        taken = 0
        for m, numeric in cands:
            if taken >= per_doc:
                break
            if m.start() < 40 or m.end() > len(text) - 40:
                continue
            if not numeric and _sentence_initial(text, m.start()):
                continue
            ans = m.group(0)
            p0 = _snap(text, max(0, m.start() - window), True) if m.start() > window else 0
            s1 = _snap(text, min(len(text), m.end() + window), False)
            prefix, suffix = text[p0 : m.start()], text[m.end() : s1]
            key = (classify_blank(ans, prefix, suffix), numeric)
            if key not in buckets or len(buckets[key]) >= per_bucket:
                continue
            buckets[key].append(
                {
                    "prefix": prefix,
                    "answer": ans,
                    "suffix": suffix,
                    "class": key[0],
                    "numeric": numeric,
                    "subset": d.get("subset", ""),
                    "doc_id": d.get("doc_id", ""),
                }
            )
            taken += 1
        if all(len(v) >= per_bucket for v in buckets.values()):
            break
    items = [it for v in buckets.values() for it in v]
    rng.shuffle(items)
    return items


def _by_class(items: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for it in items:
        c = out.setdefault(it["class"], {"n": 0, "numeric": 0})
        c["n"] += 1
        c["numeric"] += bool(it["numeric"])
    return out


# ── main ─────────────────────────────────────────────────────────────
def _parse_kv(spec: str) -> dict[str, str]:
    return {
        k: "+".join(os.path.expanduser(p) for p in v.split("+"))
        for k, v in (kv.split("=", 1) for kv in spec.split(","))
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--inputs", default="", help="name=path[+path2][,name=path] (default: the three dolmino shards)")
    ap.add_argument("--record-mode", action="store_true", help="inputs are record-shaped jsonl rows (replay.jsonl)")
    ap.add_argument("--tokens", default="pes2o=15000000,wiki=9000000,dclm=6000000", help="shard mode: per-subset token budget")
    ap.add_argument("--fim-rate", type=float, default=0.9)
    ap.add_argument("--clip", type=int, default=8000, help="max chars per doc (~2k tokens; two per block)")
    ap.add_argument("--min-chars", type=int, default=400)
    ap.add_argument("--val-frac", type=float, default=0.01)
    ap.add_argument(
        "--val-max-docs",
        type=int,
        default=120,
        help="shard mode: val documents written per subset (plain + twin); the rest are held out of training and feed the recovery items only",
    )
    ap.add_argument("--exclude-jsonl", action="append", default=[], help="drop documents present in this record jsonl (replay)")
    ap.add_argument("--recovery-n", type=int, default=400)
    ap.add_argument("--recovery-classes", default=",".join(CLASSES))
    ap.add_argument("--recovery-window", type=int, default=1500)
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    inputs = _parse_kv(args.inputs) if args.inputs else dict(SHARDS)
    ex_ids, ex_hashes = load_exclusions(args.exclude_jsonl) if args.exclude_jsonl else (set(), set())
    if args.exclude_jsonl:
        print(f"[fim] exclusions: {len(ex_ids)} ids, {len(ex_hashes)} hashes", flush=True)
    os.makedirs(args.out, exist_ok=True)
    stats: dict = {}
    val_docs: list[dict] = []
    for name, path in inputs.items():
        st = {"docs_fim": 0, "docs_plain": 0, "val_docs": 0, "val_twins": 0, "excluded": 0, "chars": 0, "modes": {}}
        fh = None if args.dry_run else open(os.path.join(args.out, f"{name}.jsonl"), "w", encoding="utf-8")

        def emit(rows: list[dict]) -> None:
            for r in rows:
                mode = r["provenance"]["mode"]
                if r["source"].startswith("fim/"):
                    st["docs_fim"] += 1
                    st["modes"][mode] = st["modes"].get(mode, 0) + 1
                    if r.get("val"):
                        st["val_twins"] += 1
                else:
                    st["docs_plain"] += 1
                if fh:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")

        if args.record_mode:
            for rec in iter_docs(path):
                text = (rec.get("text") or "").strip()
                if len(text) < args.min_chars:
                    continue
                rows = transform_record(
                    rec, rng, args.fim_rate, val_twins=False, fim_clip=args.clip, plain_source=rec.get("source")
                )
                st["val_docs"] += bool(rec.get("val"))
                st["chars"] += len(text)
                emit(rows)
        else:
            budgets = {k: int(v) for k, v in (kv.split("=") for kv in args.tokens.split(","))}
            want_chars = budgets.get(name, 0) * CHARS_PER_TOKEN
            st["val_held_out"] = 0
            # "a+b": several shards of one subset, read in order until the budget is met
            for shard in path.split("+"):
                if st["chars"] >= want_chars:
                    break
                for i, d in enumerate(iter_docs(shard)):
                    text = (d.get("text") or "").strip()
                    if len(text) < args.min_chars:
                        continue
                    did = str(d.get("id") or f"{os.path.basename(shard)}:{i}")
                    if (name, did) in ex_ids or _text_hash(text) in ex_hashes:
                        st["excluded"] += 1
                        continue
                    text = text[: args.clip]
                    doc_id = f"{name}-{did}"
                    is_val = int(hashlib.sha256(doc_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < args.val_frac
                    if is_val:
                        # every val document feeds the recovery items; only the
                        # first --val-max-docs are written (an eval over 2,500
                        # documents would cost minutes per evaluation)
                        val_docs.append({"doc_id": doc_id, "subset": name, "text": text})
                        if st["val_docs"] >= args.val_max_docs:
                            st["val_held_out"] += 1
                            continue
                        st["val_docs"] += 1
                    rec = {
                        "doc_id": doc_id,
                        "source": f"plain/{name}",
                        "text": text,
                        "license": "ODC-BY",
                        "provenance": {"subset": name, "shard": os.path.basename(shard), "id": did},
                        "val": is_val,  # package.py stage1 splits on this top-level flag
                        "val_key": doc_id,
                    }
                    emit(transform_record(rec, rng, args.fim_rate, val_twins=True))
                    st["chars"] += len(text)
                    if st["chars"] >= want_chars:
                        break
        if fh:
            fh.close()
        st["est_tokens"] = int(st["chars"] / CHARS_PER_TOKEN)
        stats[name] = st
        print(
            f"[fim] {name}: {st['docs_fim']} fim + {st['docs_plain']} plain docs (val {st['val_docs']}, twins {st['val_twins']}, excluded {st['excluded']}), ~{st['est_tokens'] / 1e6:.1f} M tokens, modes {st['modes']}",
            flush=True,
        )
    items: list[dict] = []
    if val_docs:
        classes = tuple(c for c in args.recovery_classes.split(",") if c)
        items = recovery_items(val_docs, rng, args.recovery_n, classes, window=args.recovery_window)
    manifest = {
        "stats": stats,
        "args": vars(args),
        "excluded_replay_docs": sum(s["excluded"] for s in stats.values()),
        "recovery_items": len(items),
        "recovery_by_class": _by_class(items),
        "sentinels": {"prefix": PRE_ID, "middle": MID_ID, "suffix": SUF_ID},
    }
    if not args.dry_run:
        if items:
            json.dump(items, open(os.path.join(args.out, "fim_recovery_items.json"), "w"), ensure_ascii=False, indent=0)
        json.dump(manifest, open(os.path.join(args.out, "fim_manifest.json"), "w"), indent=1)
    print(
        f"[fim] recovery items: {len(items)} {manifest['recovery_by_class']} | excluded {manifest['excluded_replay_docs']} | total ~{sum(s['est_tokens'] for s in stats.values()) / 1e6:.1f} M tokens",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
