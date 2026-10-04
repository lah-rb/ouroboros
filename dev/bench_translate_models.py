#!/usr/bin/env python3
"""Translate bench (2026-10-04): which engines should carry the translation queue?

The queue is about to grow: chunk-level patches of ~200 translated papers
(~11 M source chars) and the non-English papers of the re-OCR'd failed pile,
all on ONE seat of qwen3-next 80B-A3 on the Mac (~60 tok/s). Two questions
(operator):

  1. Qwen 3.5 9B on the 3060 box: always-on thinking, so its cost per chunk
     is unpredictable. Gate pass, numbers, English, speed, how much of its
     output is thinking, and — on a blind pairwise read — quality against
     qwen3-next on the same chunks.
  2. qwen3-next is the hybrid LLMVP can BATCH (several sequences in one
     context, like muse). Aggregate chunks/h at 1, 2 and 4 requests at once,
     on a batched variant config (qwen3-next-80b-a3-batched).

PRODUCTION PATH, nothing booked: the drain's prompt (_render_translate_prompt),
its two-temperature per-span loop, image reinsertion and _span_problem; the
output budget is the server's token count x _TRANSLATE_OUT_MARGIN when the
server answers, else the drain's own character fallback.

Sample: chunks of the SOURCES of accepted lingual papers, stratified by
language, seeded; reference-section chunks skipped (they translate by rote).

    .venv/bin/python dev/bench_translate_models.py sample
    .venv/bin/python dev/bench_translate_models.py run --arm mac --concurrency 1
    .venv/bin/python dev/bench_translate_models.py run --arm mac_batched --concurrency 4
    .venv/bin/python dev/bench_translate_models.py run --arm qwen35 --concurrency 2
    .venv/bin/python dev/bench_translate_models.py report
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import translation_actions as ta  # noqa: E402
from agent.bibliography import bibliography_spans  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
OUT = os.path.expanduser(os.environ.get("BENCH_OUT", "~/tmp/translate_bench"))
SEED = 20261004
LANGS = ("fr", "es", "pt", "de", "it", "ru", "zh", "ja")
PER_LANG = int(os.environ.get("BENCH_PER_LANG", "5"))
ARMS = {
    # the mission's own route for the remote translate lane
    "mac": ("curate_remote", None),
    "mac_batched": (
        "bench_mac_batched",
        {
            "endpoint": "http://192.168.1.209:8008/graphql",
            "model": "qwen3-next-80b-a3-batched",
        },
    ),
    "qwen35": (
        "bench_qwen35",
        {"endpoint": "http://192.168.1.76:8008/graphql", "model": "qwen3.5-9b-3060"},
    ),
}


def _records() -> dict:
    recs: dict = {}
    for name in ("papers.jsonl", "extraction.jsonl"):
        with open(f"{ROOT}/databank/{name}", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("paper_key"):
                    recs[r["paper_key"]] = {**recs.get(r["paper_key"], {}), **r}
    return recs


def cmd_sample(_a) -> None:
    rng = random.Random(SEED)
    by_lang = collections.defaultdict(list)
    for key, r in sorted(_records().items()):
        lang = str(r.get("language") or "").lower()
        if lang not in LANGS or r.get("review_status") != "accepted":
            continue
        if not (r.get("translated") or r.get("extraction_status") == "extract_lingual"):
            continue
        path = os.path.join(ROOT, str(r.get("md_path") or ""))
        if not os.path.isfile(path):
            continue
        src = open(path, encoding="utf-8", errors="replace").read()
        bib = bibliography_spans(src)
        o = 0
        for i, chunk in enumerate(ta.chunk_markdown(src)):
            s, e = o, o + len(chunk)
            o = e + 2
            if len(chunk) < 3000 or any(bs <= s and e <= be for bs, be in bib):
                continue
            by_lang[lang].append({"key": key, "lang": lang, "idx": i, "chunk": chunk})
    sample = []
    for lang in LANGS:
        pool = by_lang.get(lang, [])
        rng.shuffle(pool)
        seen: set = set()
        for c in pool:  # one chunk per paper first, so a thesis cannot dominate
            if c["key"] in seen:
                continue
            seen.add(c["key"])
            sample.append(c)
            if sum(1 for x in sample if x["lang"] == lang) >= PER_LANG:
                break
    for n, c in enumerate(sample):
        c["id"] = f"t{n:03d}"
    os.makedirs(OUT, exist_ok=True)
    json.dump(sample, open(os.path.join(OUT, "sample.json"), "w"), ensure_ascii=False)
    print(collections.Counter(c["lang"] for c in sample), f"-> {OUT}/sample.json")


def _effects(arm: str):
    domain, route = ARMS[arm]
    cfg = json.load(open(f"{ROOT}/.agent/mission.json"))["config"]
    domains = dict(cfg.get("llmvp_domains") or {})
    if route:
        domains[domain] = route
    parent = LocalEffects(ROOT, llmvp_domains=domains)
    return ChildEffects(
        parent, branch=f"bench:translate:{arm}", inference_domain=domain
    )


async def _translate(eff, chunk: dict) -> dict:
    """The drain's translate_text, verbatim policy, instrumented."""
    src = ta.strip_table_decoration(ta.collapse_source_loops(chunk["chunk"])[0])
    hint = ta._LANGUAGE_NAMES.get(chunk["lang"], chunk["lang"])
    counter = getattr(eff, "token_count", None)
    budget = max(1024, int(len(src) / 2))
    if counter is not None:
        try:
            counts = await counter([src])
            if counts:
                budget = max(1024, int(counts[0] * ta._TRANSLATE_OUT_MARGIN))
        except Exception:  # noqa: BLE001 — sizing never fails the chunk
            pass
    t0 = time.monotonic()
    tries, text, err, generated, truncated = [], "", "", 0, False
    for temp in (0.3, 0.7):
        res = await eff.run_inference(
            ta._render_translate_prompt(src, hint),
            {"temperature": temp, "max_tokens": budget},
        )
        generated += int(getattr(res, "tokens_generated", 0) or 0)
        truncated = truncated or bool(getattr(res, "truncated", False))
        if getattr(res, "error", None):
            err = str(res.error)[:200]
            tries.append({"temp": temp, "error": err})
            break
        text = str(getattr(res, "text", "") or "")
        if not text.strip():
            err = "empty translation text"
            tries.append({"temp": temp, "error": err})
            break
        text, added = ta.reinsert_missing_imgs(src, text)
        problem = ta._span_problem(src, text)
        tries.append({"temp": temp, "problem": problem, "imgs_reinserted": added})
        if not problem:
            err = ""
            break
        err = problem
    return {
        "id": chunk["id"],
        "lang": chunk["lang"],
        "src_chars": len(src),
        "budget": budget,
        "seconds": round(time.monotonic() - t0, 1),
        "generated_tokens": generated,
        "truncated": truncated,
        "tries": tries,
        "passed": bool(text.strip()) and not err,
        "problem": err,
        "numeric": round(ta._numeric_preservation(src, text), 4) if text else 0.0,
        "out_chars": len(text),
        "text": text,
    }


async def cmd_run(a) -> None:
    sample = json.load(open(os.path.join(OUT, "sample.json")))
    path = os.path.join(OUT, f"{a.arm}_c{a.concurrency}.jsonl")
    done = set()
    if os.path.exists(path):
        done = {json.loads(x)["id"] for x in open(path) if x.strip()}
    todo = [c for c in sample if c["id"] not in done][: a.limit or None]
    eff = _effects(a.arm)
    sem = asyncio.Semaphore(a.concurrency)
    t0 = time.monotonic()

    async def one(c):
        async with sem:
            row = await _translate(eff, c)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"  {row['id']} {row['lang']} {'PASS' if row['passed'] else 'fail'} "
              f"{row['seconds']:>6.1f}s gen {row['generated_tokens']:>5} {row['problem'][:60]}", flush=True)  # fmt: skip

    await asyncio.gather(*(one(c) for c in todo))
    wall = time.monotonic() - t0
    with open(os.path.join(OUT, "walls.jsonl"), "a") as fh:
        fh.write(
            json.dumps(
                {
                    "arm": a.arm,
                    "concurrency": a.concurrency,
                    "chunks": len(todo),
                    "wall_s": round(wall, 1),
                }
            )
            + "\n"
        )
    print(
        f"{a.arm} x{a.concurrency}: {len(todo)} chunks in {wall / 60:.1f} min -> {len(todo) / wall * 3600:.1f} chunks/h"
    )


def cmd_report(_a) -> None:
    walls = collections.defaultdict(list)
    if os.path.exists(os.path.join(OUT, "walls.jsonl")):
        for x in open(os.path.join(OUT, "walls.jsonl")):
            w = json.loads(x)
            walls[(w["arm"], w["concurrency"])].append(w)
    print(
        f"{'arm':<18}{'n':>4}{'pass':>7}{'numeric':>9}{'trunc':>7}{'s/chunk':>9}{'gen tok':>9}{'out/src':>9}{'chunks/h':>10}"
    )
    for path in sorted(p for p in os.listdir(OUT) if re.match(r".+_c\d+\.jsonl$", p)):
        rows = [json.loads(x) for x in open(os.path.join(OUT, path)) if x.strip()]
        if not rows:
            continue
        arm, conc = re.match(r"(.+)_c(\d+)\.jsonl$", path).groups()
        ws = walls.get((arm, int(conc)), [])
        rate = (
            sum(w["chunks"] for w in ws) / max(1, sum(w["wall_s"] for w in ws)) * 3600
            if ws
            else 0
        )
        n = len(rows)
        print(
            f"{arm + ' x' + conc:<18}{n:>4}{sum(r['passed'] for r in rows) / n:>7.0%}"
            f"{sum(r['numeric'] for r in rows) / n:>9.3f}{sum(r['truncated'] for r in rows):>7}"
            f"{sum(r['seconds'] for r in rows) / n:>9.1f}{sum(r['generated_tokens'] for r in rows) / n:>9.0f}"
            f"{sum(r['out_chars'] for r in rows) / max(1, sum(r['src_chars'] for r in rows)):>9.2f}{rate:>10.1f}"
        )
        fails = collections.Counter(
            r["problem"].split(":")[0] for r in rows if not r["passed"]
        )
        if fails:
            print(f"{'':<18}failures: {dict(fails)}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sample")
    r = sub.add_parser("run")
    r.add_argument("--arm", choices=tuple(ARMS), required=True)
    r.add_argument("--concurrency", type=int, default=1)
    r.add_argument("--limit", type=int, default=0)
    sub.add_parser("report")
    a = ap.parse_args()
    if a.cmd == "sample":
        cmd_sample(a)
    elif a.cmd == "run":
        asyncio.run(cmd_run(a))
    else:
        cmd_report(a)


if __name__ == "__main__":
    main()
