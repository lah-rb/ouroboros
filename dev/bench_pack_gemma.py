"""Can Gemma 4 12B on the 3060 take repacks off muse? Pack bench, same documents.

WHY (2026-09-30). The two longest queues tonight are both muse's: repacks
(234 accepted papers at ~3.7/h, ~60 h) and figure reading (~36 h). OCR on the
3060 box went idle once its queue drained, so the card is free — for a model
that fits 12 GB alone. Before any repack is trusted to it, it packs papers muse
has ALREADY packed, through PRODUCTION `_pack_windowed` (the pack-only path a
repack takes: raw doc, section windows, the registry block, two attempts per
window, the grounding / registry gates), and nothing is booked.

Two arms, run as two processes in parallel (each on its own server):

    muse   the local server, the curate lanes' domain   (same day, same registry)
    gemma  llmvp_domains["pack_gemma"] -> the 3060 box  (gemma-4-12b-3060)

Plus the STORED muse pack for each paper as a reference (an earlier day's
registry, so its coinage is not comparable — numbers are).

    .venv/bin/python dev/bench_pack_gemma.py select        # once: writes the paper list
    setsid nohup .venv/bin/python -u dev/bench_pack_gemma.py run gemma > ~/tmp/pack_gemma_bench/gemma.log 2>&1 &
    setsid nohup .venv/bin/python -u dev/bench_pack_gemma.py run muse  > ~/tmp/pack_gemma_bench/muse.log 2>&1 &
    .venv/bin/python dev/bench_pack_gemma.py report

Value agreement: every number in a pack's leaves, rounded to 4 significant
figures, as a multiset; `recall_vs_stored` = share of the stored muse pack's
numbers this arm also packed, `extra_vs_stored` = share of this arm's numbers
the stored pack lacks (all of them passed the grounding gate, so "extra" is
not "invented" — it is a selectivity difference to read by hand).
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.pack_windows import window_sections  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
OUT = os.path.expanduser(os.environ.get("BENCH_OUT", "~/tmp/pack_gemma_bench"))
PAPERS = os.path.join(OUT, "papers.json")
SEED = 20260930
PER_STRATUM = int(os.environ.get("BENCH_PER_STRATUM", "3"))
# window count strata: one window, two, three or four
STRATA = {"1": (1, 1), "2": (2, 2), "3-4": (3, 4)}
# the 3060 seat is 49,152: static head ~2k + pack prompt ~2.6k + 8,192 out
MAX_WINDOW_TOKENS = 25_000
ARMS = {
    "muse": "",  # the mission's default domain: the local server
    "gemma": "pack_gemma",
}
GEMMA_ROUTE = {
    "endpoint": os.environ.get("GEMMA_ENDPOINT", "http://192.168.1.76:8008/graphql"),
    "model": os.environ.get("GEMMA_MODEL", "gemma-4-12b-3060"),
}


def _latest_papers() -> dict:
    rows: dict = {}
    with open(f"{ROOT}/databank/papers.jsonl") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("paper_key"):
                rows[d["paper_key"]] = d
    return rows


def _effects(domain: str):
    cfg = json.load(open(f"{ROOT}/.agent/mission.json"))["config"]
    domains = dict(cfg.get("llmvp_domains") or {})
    domains["pack_gemma"] = GEMMA_ROUTE
    parent = LocalEffects(ROOT, llmvp_domains=domains)
    return ChildEffects(parent, branch="bench:pack_gemma", inference_domain=domain)


def _numbers(obj) -> collections.Counter:
    out: collections.Counter = collections.Counter()

    def walk(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            if math.isfinite(x):
                out[float(f"{x:.4g}")] += 1
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return out


def _overlap(arm: collections.Counter, ref: collections.Counter) -> tuple:
    shared = sum((arm & ref).values())
    n_arm, n_ref = sum(arm.values()), sum(ref.values())
    return (
        round(shared / n_ref, 3) if n_ref else None,
        round((n_arm - shared) / n_arm, 3) if n_arm else None,
    )


async def select() -> None:
    if os.path.exists(PAPERS):
        print(f"{PAPERS} exists — keeping it (delete it to reselect)")
        return
    os.makedirs(OUT, exist_ok=True)
    fx = _effects("")
    rows = _latest_papers()
    keys = sorted(
        k
        for k, r in rows.items()
        if r.get("pack_status") == "packed"
        and r.get("pack_doc_form") == "raw"
        and r.get("review_status") == "accepted"
        and os.path.exists(f"{ROOT}/databank/dataset/{k}.json")
    )
    random.Random(SEED).shuffle(keys)
    target, cap = ca._pack_window_sizes()
    picked: dict = {s: [] for s in STRATA}
    for k in keys:
        if all(len(v) >= PER_STRATUM for v in picked.values()):
            break
        doc = await ca._raw_curator_doc(fx, k)
        wins = window_sections(doc, target, cap)
        if not wins or max(w.tokens for w in wins) > MAX_WINDOW_TOKENS:
            continue
        for s, (lo, hi) in STRATA.items():
            if lo <= len(wins) <= hi and len(picked[s]) < PER_STRATUM:
                picked[s].append(
                    {
                        "key": k,
                        "stratum": s,
                        "windows": len(wins),
                        "doc_tokens": sum(w.tokens for w in wins),
                        "doc_sha": hashlib.sha256(doc.encode()).hexdigest()[:16],
                        "title": str(rows[k].get("title") or "")[:120],
                    }
                )
    papers = [p for s in STRATA for p in picked[s]]
    json.dump(papers, open(PAPERS, "w"), indent=1)
    for p in papers:
        print(f"{p['stratum']:<4} {p['windows']}w {p['doc_tokens']:>6} tok  {p['key']}")
    print(f"wrote {PAPERS} ({len(papers)} papers)")


async def run(arm: str) -> None:
    papers = json.load(open(PAPERS))
    fx = _effects(ARMS[arm])
    registry = await ca._load_registry(fx)
    out_rows = os.path.join(OUT, f"{arm}.json")
    rows = json.load(open(out_rows)) if os.path.exists(out_rows) else []
    done = {r["key"] for r in rows}
    os.makedirs(os.path.join(OUT, arm), exist_ok=True)
    for p in papers:
        key = p["key"]
        if key in done:
            continue
        doc = await ca._raw_curator_doc(fx, key)
        sha = hashlib.sha256(doc.encode()).hexdigest()[:16]
        t0 = time.time()
        pack, faults = None, []
        # Production's drain retries a transport fault next round; give the
        # bench the same allowance so one degenerate abort does not end a paper.
        for _ in range(3):
            try:
                pack = await ca._pack_windowed(fx, doc, registry)
                break
            except ca._CurateTransportFault as e:
                faults.append(str(e)[:160])
                print(f"{arm} {key} transport fault: {str(e)[:120]}", flush=True)
        if pack is None:
            pack = {"status": "transport_fault", "quality": {}, "data": {}}
        q = pack.get("quality") or {}
        stored = json.load(open(f"{ROOT}/databank/dataset/{key}.json"))
        recall, extra = _overlap(
            _numbers(pack.get("data") or {}), _numbers(stored.get("data") or {})
        )
        row = dict(
            arm=arm,
            key=key,
            stratum=p["stratum"],
            doc_sha_match=sha == p["doc_sha"],
            status=pack["status"],
            seconds=round(time.time() - t0),
            attempts=pack.get("attempts"),
            windows=q.get("windows"),
            windows_passed=q.get("windows_passed"),
            leaves=q.get("numeric_leaves"),
            grounding=q.get("grounding_rate"),
            ungrounded=len(q.get("ungrounded") or []),
            new_keys=q.get("new_keys"),
            reused_keys=q.get("reused_keys"),
            stored_leaves=sum(_numbers(stored.get("data") or {}).values()),
            recall_vs_stored=recall,
            extra_vs_stored=extra,
            transport_faults=faults,
            reason=str(pack.get("reason") or "")[:300],
            window_outcomes=q.get("window_outcomes"),
        )
        rows.append(row)
        json.dump(rows, open(out_rows, "w"), indent=1)
        json.dump(pack, open(os.path.join(OUT, arm, f"{key}.json"), "w"), indent=1)
        print(
            f"{arm:<5} {p['stratum']:<4} {key:<40} {pack['status']:<12} "
            f"win {q.get('windows_passed')}/{q.get('windows')} leaves {q.get('numeric_leaves')} "
            f"(stored {row['stored_leaves']}) recall {recall} extra {extra} "
            f"new {q.get('new_keys')} {row['seconds']}s",
            flush=True,
        )


def report() -> None:
    papers = json.load(open(PAPERS))
    arms = {}
    for arm in ARMS:
        path = os.path.join(OUT, f"{arm}.json")
        arms[arm] = (
            {r["key"]: r for r in json.load(open(path))} if os.path.exists(path) else {}
        )
    print(f"{'paper':<42}{'str':<5}" + "".join(f"{a:>34}" for a in ARMS))
    for p in papers:
        cells = []
        for arm in ARMS:
            r = arms[arm].get(p["key"])
            cells.append(
                "—"
                if r is None
                else f"{r['status'][:6]} {r['windows_passed']}/{r['windows']} "
                f"lv {r['leaves']}/{r['stored_leaves']} rc {r['recall_vs_stored']} {r['seconds']}s"
            )
        print(
            f"{p['key'][:41]:<42}{p['stratum']:<5}" + "".join(f"{c:>34}" for c in cells)
        )
    print()
    for arm, rs in arms.items():
        if not rs:
            continue
        vals = list(rs.values())
        packed = [r for r in vals if r["status"] == "packed"]
        wp = sum(r["windows_passed"] or 0 for r in vals)
        wt = sum(r["windows"] or 0 for r in vals)
        lv = sum(r["leaves"] or 0 for r in packed)
        st = sum(r["stored_leaves"] for r in packed)
        rc = [
            r["recall_vs_stored"] for r in packed if r["recall_vs_stored"] is not None
        ]
        secs = sum(r["seconds"] for r in vals)
        print(
            f"{arm:<6} packed {len(packed)}/{len(vals)}  windows {wp}/{wt}  "
            f"leaves {lv} vs stored {st}  median recall {sorted(rc)[len(rc)//2] if rc else None}  "
            f"new keys {sum(r['new_keys'] or 0 for r in packed)}  "
            f"{secs/len(vals)/60:.1f} min/paper  ({3600*len(vals)/secs if secs else 0:.1f} papers/h serial)"
        )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "select":
        asyncio.run(select())
    elif cmd == "run":
        asyncio.run(run(sys.argv[2]))
    else:
        report()
