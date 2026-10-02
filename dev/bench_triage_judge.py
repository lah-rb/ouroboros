"""Page-one fit judge bench: could a cheap first read predict the curator?

WHY (2026-10-01). Curation denies 43% of what it reviews, and every denied
paper first costs full OCR, ~24 figure readings and a curation review. The
existing pre-OCR triage asks "is it geological?", which "mineral dust" and
"ceramics" pass: curation then denied 55% of the papers triage called research
+ on topic. A judge that reads page one against the corpus charter (the
mission objective, in the operator's words) could order or screen work before
the expensive stages. Operator ruling: bench it during the OCR -> packing
transition of the 3060 (gemma-4-12b loaded there), against muse.

Labels: the curator's own verdicts (accepted / denied) on reviewed papers.
Input: the opening of the paper's markdown (English version when present) --
title, abstract and the start of the introduction, standing in for page one.
Verdict: FIT yes | no | unclear. A judge is useful only if it keeps (yes or
unclear) nearly every ACCEPTED paper; what it then catches among the DENIED
ones is the saving.

    .venv/bin/python dev/bench_triage_judge.py select [--n 200]
    .venv/bin/python dev/bench_triage_judge.py run gemma|muse [--concurrency 2]
    .venv/bin/python dev/bench_triage_judge.py report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
OUT = os.path.expanduser(os.environ.get("BENCH_OUT", "~/tmp/triage_bench"))
ITEMS = os.path.join(OUT, "items.json")
SEED = 20261002
HEAD_CHARS = 6000
DOMAINS = {
    "muse": "",
    "gemma": "triage_gemma",
}
GEMMA_ROUTE = {
    "endpoint": "http://192.168.1.76:8008/graphql",
    "model": "gemma-4-12b-3060",
}

PROMPT = """You are screening scientific papers for a research corpus. This is what the corpus collects, in its curator's words:

<charter>
{charter}
</charter>

Below is the OPENING of one paper (title, abstract, start of the text). Judge from it alone whether a careful curator applying the charter above would ACCEPT this paper into the corpus.

<paper>
{head}
</paper>

Answer in exactly two lines:
FIT: yes | no | unclear
REASON: one sentence"""


def _rows(path: str) -> dict:
    out = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("paper_key"):
                out[d["paper_key"]] = d
    return out


def _head(key: str, rec: dict) -> str:
    for rel in (f"databank/markdown/{key}.en.md", f"databank/markdown/{key}.md"):
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            text = open(p, encoding="utf-8", errors="replace").read()
            text = re.sub(r"<img[^>]*>|<div[^>]*>|</div>", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:HEAD_CHARS]
    return ""


def select(n: int) -> None:
    os.makedirs(OUT, exist_ok=True)
    papers = _rows(os.path.join(ROOT, "databank", "papers.jsonl"))
    pool = {"accepted": [], "denied": []}
    for k, r in papers.items():
        v = r.get("review_status")
        if v not in pool or r.get("record_kind") == "supplement" or r.get("binder"):
            continue
        if str(r.get("curation_method") or "").startswith(
            ("binder_preapproved", "closed_preapproved")
        ):
            continue
        pool[v].append(k)
    rng = random.Random(SEED)
    items = []
    for label in ("accepted", "denied"):
        keys = sorted(pool[label])
        rng.shuffle(keys)
        for k in keys:
            if len([i for i in items if i["label"] == label]) >= n // 2:
                break
            head = _head(k, papers[k])
            if len(head) < 800:
                continue
            items.append({"key": k, "label": label, "head": head})
    json.dump(items, open(ITEMS, "w"), ensure_ascii=False)
    print(f"wrote {ITEMS}: {len(items)} items "
          f"({sum(i['label'] == 'accepted' for i in items)} accepted, "
          f"{sum(i['label'] == 'denied' for i in items)} denied)")  # fmt: skip


def _verdict(text: str) -> str:
    m = re.search(r"FIT:\s*(yes|no|unclear)", text or "", re.I)
    return m.group(1).lower() if m else "unparsed"


async def run(arm: str, concurrency: int) -> None:
    items = json.load(open(ITEMS))
    cfg = json.load(open(f"{ROOT}/.agent/mission.json"))["config"]
    domains = dict(cfg.get("llmvp_domains") or {}, triage_gemma=GEMMA_ROUTE)
    parent = LocalEffects(ROOT, llmvp_domains=domains)
    fx = ChildEffects(parent, branch="bench:triage", inference_domain=DOMAINS[arm])
    charter = str(cfg.get("objective") or "")
    if not charter:
        mission = await parent.load_mission()
        charter = str(getattr(mission, "objective", "") or "")
    out_path = os.path.join(OUT, f"{arm}.jsonl")
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(line)["key"] for line in open(out_path) if line.strip()}
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one(item):
        async with sem:
            t0 = time.time()
            r = await fx.run_inference(
                PROMPT.format(charter=charter, head=item["head"]),
                {"max_tokens": int(os.environ.get("BENCH_MAX_TOKENS", "400")), "temperature": 0.0},
            )
            row = {"key": item["key"], "label": item["label"], "verdict": _verdict(r.text),
                   "text": (r.text or "")[:400], "error": r.error,
                   "seconds": round(time.time() - t0, 1)}  # fmt: skip
            async with lock:
                with open(out_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    todo = [i for i in items if i["key"] not in done]
    print(f"{arm}: {len(todo)} to judge ({len(done)} done)", flush=True)
    await asyncio.gather(*(one(i) for i in todo))


def report() -> None:
    for arm in DOMAINS:
        path = os.path.join(OUT, f"{arm}.jsonl")
        if not os.path.exists(path):
            continue
        rows = {}
        for line in open(path):
            if line.strip():
                r = json.loads(line)
                rows[r["key"]] = r
        acc = [r for r in rows.values() if r["label"] == "accepted"]
        den = [r for r in rows.values() if r["label"] == "denied"]

        def share(rs, *vs):
            return sum(r["verdict"] in vs for r in rs) / max(1, len(rs))

        secs = sorted(r["seconds"] for r in rows.values())
        print(
            f"{arm:<6} n={len(rows)} | ACCEPTED kept {share(acc, 'yes', 'unclear'):.0%} "
            f"(yes {share(acc, 'yes'):.0%}, unclear {share(acc, 'unclear'):.0%}, no {share(acc, 'no'):.0%}) "
            f"| DENIED caught {share(den, 'no'):.0%} (unclear {share(den, 'unclear'):.0%}) "
            f"| unparsed {share(list(rows.values()), 'unparsed'):.0%} | median {secs[len(secs) // 2] if secs else 0}s"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("select", "run", "report"))
    ap.add_argument("arm", nargs="?", default="")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=1)
    a = ap.parse_args()
    if a.cmd == "select":
        select(a.n)
    elif a.cmd == "run":
        asyncio.run(run(a.arm, a.concurrency))
    else:
        report()


if __name__ == "__main__":
    main()
