#!/usr/bin/env python3
"""Counterfactual corpus regeneration AS a max-throughput swarm benchmark.

Two jobs, one pass:

1. REGENERATE the low/medium/high candidate actions for the adaptive_thinking
   label corpus, using the fixed extractor (dev/cf_extract.py). The originals
   were mutilated by a fallback bug — see that module's docstring.
2. MEASURE the batched engine under real, heterogeneous, high-concurrency load.
   Every request is an independent stateless completion, which is exactly the
   shape the batched single-context engine is meant to eat.

Per-request `reasoning` (shipped f1f30f6/a54bd03) means all three levels run in
ONE pass with no server reconfiguration — the original harness needed three
separate runs with the level baked into the server config.

Modes
-----
  --sweep      concurrency sweep on a fixed sample: find max aggregate decode
  --run        full regeneration at --concurrency
  --limit N    cap turns (smoke tests)

Aggregate decode is measured as total generated tokens / wall-clock of the
whole wave, which is the number that actually matters for swarm work; per-
request rate is reported alongside so the batching gain is visible.

Usage:
  cf_regen_swarm.py --sweep --limit 24
  cf_regen_swarm.py --run --concurrency 16 --out dev/cf_regen_v2.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics as st
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cf_extract import audit, extract  # noqa: E402

ENDPOINT = "http://localhost:8008/graphql"
LEVELS = ("low", "medium", "high")
QUERY = (
    "query($r: CompletionRequest!){ rawCompletion(request:$r)"
    "{ rawText tokensGenerated finished } }"
)
# Generous: the whole point is to let high-reasoning turns REACH the final
# channel. The old corpus was not budget-starved (2500) but extraction-broken;
# still, headroom costs nothing when truncation is the failure being fixed.
MAX_TOKENS = 4096
TEMPERATURE = 0.3


def load_turns(limit: int | None) -> list[dict]:
    """Turns come from the *_labeling.json files, which carry the prompts."""
    out, seen = [], set()
    for src in ("phaseB", "phaseC", "tb1_cf"):
        p = Path(__file__).resolve().parent / f"{src}_labeling.json"
        if not p.exists():
            continue
        d = json.loads(p.read_text())
        recs = d if isinstance(d, list) else d.get("records", [])
        for r in recs:
            uid = f"{src}:{r['id']}"
            if uid in seen:
                continue
            seen.add(uid)
            out.append(
                {"uid": uid, "source": src, "id": r["id"], "prompt": r["context"]}
            )
    out.sort(key=lambda r: r["uid"])
    return out[:limit] if limit else out


async def one(client: httpx.AsyncClient, turn: dict, level: str) -> dict:
    t0 = time.perf_counter()
    body = {
        "query": QUERY,
        "variables": {
            "r": {
                "prompt": turn["prompt"],
                "maxTokens": MAX_TOKENS,
                "temperature": TEMPERATURE,
                "reasoning": level,
            }
        },
    }
    rec = {"uid": turn["uid"], "level": level}
    try:
        resp = await client.post(ENDPOINT, json=body)
        d = resp.json()
        if d.get("errors"):
            rec |= {"error": str(d["errors"][:1])[:160], "usable": False, "tokens": 0}
        else:
            rc = d["data"]["rawCompletion"]
            ex = extract(rc["rawText"], rc.get("finished", True))
            rec |= ex
            rec["tokens"] = rc["tokensGenerated"]
            rec["raw_chars"] = len(rc["rawText"])
    except Exception as exc:  # noqa: BLE001 — a dead request must not kill the wave
        rec |= {
            "error": f"{type(exc).__name__}: {exc}"[:160],
            "usable": False,
            "tokens": 0,
        }
    rec["wall_s"] = round(time.perf_counter() - t0, 2)
    return rec


async def wave(jobs: list[tuple], concurrency: int, out_fh=None) -> dict:
    """Run `jobs` at fixed concurrency; return throughput stats."""
    sem = asyncio.Semaphore(concurrency)
    results: list[dict] = []
    done = 0
    t0 = time.perf_counter()

    async with httpx.AsyncClient(timeout=None) as client:

        async def guarded(turn, level):
            nonlocal done
            async with sem:
                r = await one(client, turn, level)
            results.append(r)
            if out_fh:
                out_fh.write(json.dumps(r) + "\n")
                out_fh.flush()
            done += 1
            if done % 25 == 0:
                el = time.perf_counter() - t0
                tk = sum(x.get("tokens", 0) for x in results)
                print(
                    f"    {done}/{len(jobs)}  {el:6.0f}s  agg {tk/el:6.1f} tok/s",
                    flush=True,
                )

        await asyncio.gather(*(guarded(t, lv) for t, lv in jobs))

    elapsed = time.perf_counter() - t0
    toks = sum(r.get("tokens", 0) for r in results)
    walls = [r["wall_s"] for r in results if r.get("wall_s")]
    errs = sum(1 for r in results if r.get("error"))
    per_req = [
        r["tokens"] / r["wall_s"]
        for r in results
        if r.get("tokens") and r.get("wall_s")
    ]
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "elapsed_s": round(elapsed, 1),
        "tokens": toks,
        "aggregate_tok_s": round(toks / elapsed, 1) if elapsed else 0,
        "per_request_tok_s_mean": round(st.mean(per_req), 1) if per_req else 0,
        "latency_p50_s": round(st.median(walls), 1) if walls else 0,
        "latency_p95_s": (
            round(sorted(walls)[int(len(walls) * 0.95)], 1) if walls else 0
        ),
        "errors": errs,
        "results": results,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--out", default="dev/cf_regen_v2.jsonl")
    ap.add_argument(
        "--ladder", default="1,2,4,8,16,24,32", help="sweep concurrency ladder"
    )
    args = ap.parse_args()

    turns = load_turns(args.limit)
    print(f"turns loaded: {len(turns)}  (x{len(LEVELS)} levels = {len(turns)*3} jobs)")

    if args.sweep:
        ladder = [int(x) for x in args.ladder.split(",")]
        sample = turns[: max(ladder)] if len(turns) >= max(ladder) else turns
        print(f"\nSWEEP — {len(sample)} turns/rung at level=high (worst case)\n")
        rows = []
        for c in ladder:
            jobs = [(t, "high") for t in sample]
            s = await wave(jobs, c)
            rows.append(s)
            print(
                f"  c={c:<3} agg {s['aggregate_tok_s']:>7.1f} tok/s   "
                f"per-req {s['per_request_tok_s_mean']:>6.1f}   "
                f"p50 {s['latency_p50_s']:>6.1f}s  p95 {s['latency_p95_s']:>6.1f}s  "
                f"err {s['errors']}"
            )
        best = max(rows, key=lambda r: r["aggregate_tok_s"])
        print(
            f"\n  PEAK aggregate: {best['aggregate_tok_s']} tok/s at "
            f"concurrency {best['concurrency']}"
        )
        base = rows[0]["aggregate_tok_s"] or 1
        print(f"  batching gain vs c=1: {best['aggregate_tok_s']/base:.2f}x")
        Path("dev/cf_sweep_results.json").write_text(
            json.dumps(
                [{k: v for k, v in r.items() if k != "results"} for r in rows], indent=2
            )
        )
        print("  -> dev/cf_sweep_results.json")

    if args.run:
        jobs = [(t, lv) for t in turns for lv in LEVELS]
        print(f"\nREGEN — {len(jobs)} jobs at concurrency {args.concurrency}\n")
        with open(args.out, "w") as fh:
            s = await wave(jobs, args.concurrency, out_fh=fh)
        print(
            f"\n  done: {s['requests']} requests, {s['tokens']:,} tokens, "
            f"{s['elapsed_s']}s, aggregate {s['aggregate_tok_s']} tok/s, "
            f"errors {s['errors']}"
        )
        by_level = {}
        for lv in LEVELS:
            sub = [r for r in s["results"] if r["level"] == lv]
            by_level[lv] = audit(sub)
        print("\n  EXTRACTION HEALTH (the thing the old harness got wrong):")
        for lv, a in by_level.items():
            print(
                f"    {lv:<7} n={a['n']:<5} usable={a['usable']:<5} "
                f"no_final={a['no_final']:<4} truncated={a['truncated']:<4} "
                f"unusable={a['unusable_pct']}%"
            )
        print(f"\n  -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
