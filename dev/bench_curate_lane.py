"""Bench a candidate curate model on a remote LLMVP, on REAL curator docs.

The question: gpt-oss-120b is faster and parallel-capable -- is it fast
enough at the curate position to be the throughput lever, and does the Mac
serve curate or figtext better?

Two methodology points that decide whether the numbers mean anything:

1. The curate turn is PREFILL-DOMINATED -- ~23k tokens in, a few hundred
   out -- so a bench on short prompts measures the wrong half of the
   workload. These are actual curator docs (markdown + inlined figtext),
   built by the same `build_curator_doc` the flow calls.

2. Every call gets a DISTINCT document. LLMVP reports `cacheHit` and
   `cachedPrefixTokens`, and reusing one prompt would let the prefix cache
   serve later reps at a fraction of the true cost -- turning a prefill
   benchmark into a cache benchmark. Cache hits are counted and printed as
   a warning so a contaminated arm cannot pass silently.

Timing comes from the server's own `prefillMs`/`decodeMs` registers plus
wall clock, not from wall clock alone.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import statistics
import time

import httpx

LOAD = """mutation($n:String!){
  swapModel(name:$n){ ok name previous noop rolledBack loadMs totalMs error }
}"""
HEALTH = "{health{status poolSize availableInstances inFlight}}"
COMPLETE = """mutation($r:CompletionRequest!){
  createCompletion(request:$r){
    text promptTokens generatedTokens cachedPrefixTokens freshPrefillTokens
    cacheHit prefillMs decodeMs finished truncated
  }
}"""


async def gql(client, url, query, variables=None, timeout=3600.0):
    r = await client.post(
        url, json={"query": query, "variables": variables or {}}, timeout=timeout
    )
    r.raise_for_status()
    d = r.json()
    if d.get("errors"):
        raise RuntimeError(json.dumps(d["errors"])[:400])
    return d["data"]


async def one(client, url, prompt, max_tokens):
    t0 = time.time()
    d = await gql(
        client,
        url,
        COMPLETE,
        {"r": {"prompt": prompt, "maxTokens": max_tokens, "temperature": 0.4}},
    )
    dt = time.time() - t0
    c = d["createCompletion"]
    return {
        "seconds": dt,
        "prompt_tokens": c.get("promptTokens") or 0,
        "generated": c.get("generatedTokens") or 0,
        "fresh_prefill": c.get("freshPrefillTokens") or 0,
        "cached_prefix": c.get("cachedPrefixTokens") or 0,
        "cache_hit": bool(c.get("cacheHit")),
        "prefill_ms": c.get("prefillMs") or 0,
        "decode_ms": c.get("decodeMs") or 0,
        "truncated": bool(c.get("truncated")),
        "text": (c.get("text") or "")[:500],
    }


async def run(url, model, prompts, max_tokens, concurrencies, reps):
    out = {"model": model, "url": url, "arms": [], "samples": []}
    async with httpx.AsyncClient() as client:
        h = await gql(client, url, HEALTH)
        print(f"  health before: {h['health']}", flush=True)
        if model:
            print(f"  loading {model} ...", flush=True)
            t0 = time.time()
            d = await gql(client, url, LOAD, {"n": model})
            out["load_seconds"] = time.time() - t0
            sw = d["swapModel"]
            if not sw.get("ok"):
                raise SystemExit(f"swap FAILED: {sw.get('error')}")
            print(
                f"  swapped in {out['load_seconds']:.0f}s "
                f"(prev={sw.get('previous')} noop={sw.get('noop')})",
                flush=True,
            )

        print("  warm-up ...", flush=True)
        w = await one(client, url, prompts[0], 64)
        print(
            f"    warm-up {w['seconds']:.1f}s prefill={w['fresh_prefill']} "
            f"decode={w['generated']}",
            flush=True,
        )

        cursor = 1  # prompts[0] spent on warm-up
        for n in concurrencies:
            runs = []
            for rep in range(reps):
                batch = []
                for _ in range(n):
                    batch.append(prompts[cursor % len(prompts)])
                    cursor += 1
                t0 = time.time()
                res = await asyncio.gather(
                    *[one(client, url, p, max_tokens) for p in batch]
                )
                wall = time.time() - t0
                fresh = sum(r["fresh_prefill"] for r in res)
                gen = sum(r["generated"] for r in res)
                hits = sum(1 for r in res if r["cache_hit"])
                runs.append(
                    {
                        "wall": wall,
                        "fresh_prefill": fresh,
                        "generated": gen,
                        "cache_hits": hits,
                        "per_call_s": [r["seconds"] for r in res],
                    }
                )
                out["samples"].extend(res)
                warn = f"   [!! {hits}/{n} CACHE HITS]" if hits else ""
                print(
                    f"    conc={n} rep={rep+1}: wall {wall:>6.1f}s  "
                    f"prefill {fresh:>7,} ({fresh/wall:>7.1f} tok/s)  "
                    f"decode {gen:>5,} ({gen/wall:>6.1f} tok/s){warn}",
                    flush=True,
                )
            med = statistics.median(r["wall"] for r in runs)
            fp = statistics.median(r["fresh_prefill"] for r in runs)
            gn = statistics.median(r["generated"] for r in runs)
            out["arms"].append(
                {
                    "concurrency": n,
                    "median_wall_s": med,
                    "prefill_tok_s": fp / med,
                    "decode_tok_s": gn / med,
                    "cache_hits": sum(r["cache_hits"] for r in runs),
                    "docs_per_hour": 3600.0 * n / med,
                    "runs": runs,
                }
            )
            print(
                f"  conc={n}: median wall {med:.1f}s -> {3600.0*n/med:.1f} docs/hour",
                flush=True,
            )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://192.168.1.209:8008/graphql")
    ap.add_argument("--model", default=None, help="registry name to loadModel first")
    ap.add_argument("--docs-glob", default="/tmp/figdig_bench/docs/*.txt")
    ap.add_argument("--max-tokens", type=int, default=384)
    ap.add_argument("--concurrency", default="1,2,4")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    paths = sorted(glob.glob(a.docs_glob))
    if not paths:
        raise SystemExit(f"no curator docs matched {a.docs_glob}")
    head = (
        "You are curating a scientific paper for a spectroscopy data corpus.\n"
        "Read the document and reply with a JSON object: "
        '{"status":"accepted"|"denied","summary":"<2-3 sentences>"}.\n\n'
    )
    prompts = [
        head
        + "---\n"
        + open(p, encoding="utf-8", errors="ignore").read()
        + "\n---\n\nJSON:"
        for p in paths
    ]
    mean = sum(len(p) for p in prompts) / len(prompts)
    print(f"=== bench {a.model or '(active model)'} @ {a.url} ===")
    print(
        f"  {len(prompts)} DISTINCT curator docs, mean {mean:,.0f} chars "
        f"(~{mean/3.08:,.0f} tokens); distinct so no prefix cache can flatter this"
    )
    res = asyncio.run(
        run(
            a.url,
            a.model,
            prompts,
            a.max_tokens,
            [int(x) for x in a.concurrency.split(",")],
            a.reps,
        )
    )
    if a.out:
        json.dump(res, open(a.out, "w"), indent=1)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
