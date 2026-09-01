"""Does gpt-oss reproduce muse's curation verdicts, on production prompts?

Speed is settled (dev/CURATE_LANE_BENCH_2026-08-31.md): gpt-oss is ~3.5x
muse on the curate workload. Quality is the gating question for any swap.

METHOD -- the point that decides whether this measures anything:

An accept-only test is UNFALSIFIABLE. Every pack on disk is `accepted`, so a
model that accepts unconditionally scores 100%. This project has hit that
vacuous-gate shape five times already. So the sample is BALANCED: papers muse
accepted (from databank/dataset/) AND papers muse denied (review_status in
papers.jsonl, 1466 available). A false ACCEPT is the more expensive error --
it pollutes the corpus -- so the deny arm is the one that matters most.

Prompts are the production ones, rendered through the same PromptRenderer
over prompts/curator/review_paper with the live mission objective as
corpus_subject, and the same turn shape the flow fires:

    doc + "\\n\\n---\\n\\n" + review_prompt      max_tokens 4096, temp t*0.4

Verdicts are parsed with the production `parse_llm_json`, so a model whose
output the real pipeline could not parse scores as a review failure here too.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import random
import sys
import time

import httpx
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.curation_actions import (  # noqa: E402
    _prompts_dir,
    build_curator_doc,
)
from agent.llm_json import parse_llm_json  # noqa: E402
from agent.loader import PromptRenderer  # noqa: E402

WD = os.path.expanduser("~/corpora/ouroboros-spectra")
D = f"{WD}/databank"
COMPLETE = """mutation($r:CompletionRequest!){
  createCompletion(request:$r){ text promptTokens generatedTokens
    freshPrefillTokens truncated finished }
}"""
SWAP = """mutation($n:String!){
  swapModel(name:$n){ ok name previous noop error }
}"""


def corpus_subject() -> str:
    try:
        m = json.load(open(f"{WD}/.agent/mission.json"))
        return str(m.get("objective") or "").strip()
    except Exception:
        return ""


def curator_doc(key: str) -> str | None:
    md_p = f"{D}/markdown/{key}.md"
    if not os.path.exists(md_p):
        return None
    md = open(md_p, encoding="utf-8", errors="ignore").read()
    if not md.strip():
        return None
    fx = None
    ft_p = f"{D}/figtext/{key}.json"
    if os.path.exists(ft_p):
        try:
            fx = json.load(open(ft_p))
        except Exception:
            fx = None
    return build_curator_doc(md, fx)


def sample(n_acc: int, n_den: int, seed: int):
    """Accepted spread ACROSS SIZES (the operator's ask), denied as control."""
    rng = random.Random(seed)
    packed = {}
    for f in glob.glob(f"{D}/dataset/*.json"):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        prov = d.get("provenance") or {}
        # only papers muse itself judged -- haiku-repacked ones are not
        # muse's verdict and would contaminate the comparison
        if prov.get("model") != "muse-glimmer-30b-cuda":
            continue
        if "haiku" in str(prov.get("repack_method") or ""):
            continue
        packed[os.path.basename(f)[:-5]] = d

    acc = []
    for k, d in packed.items():
        doc = curator_doc(k)
        if doc:
            acc.append((len(doc), k, d))
    acc.sort()
    # spread over the size range: even quantiles, so this is not all-small
    picks_acc = []
    if acc:
        idx = np.linspace(0, len(acc) - 1, num=min(n_acc, len(acc))).round().astype(int)
        picks_acc = [acc[i] for i in dict.fromkeys(idx.tolist())]

    den_keys = []
    with open(f"{D}/papers.jsonl") as fh:
        byk = {}
        for line in fh:
            try:
                d = json.loads(line)
            except Exception:
                continue
            k = d.get("paper_key")
            if k:
                byk[k] = d
    for k, d in byk.items():
        if d.get("review_status") == "denied" and k not in packed:
            den_keys.append((k, d))
    rng.shuffle(den_keys)
    picks_den = []
    for k, d in den_keys:
        doc = curator_doc(k)
        if doc:
            picks_den.append((len(doc), k, d))
        if len(picks_den) >= n_den:
            break
    return picks_acc, picks_den


async def ask(client, url, prompt, temp, max_tokens=4096):
    t0 = time.time()
    r = await client.post(
        url,
        json={
            "query": COMPLETE,
            "variables": {
                "r": {"prompt": prompt, "maxTokens": max_tokens, "temperature": temp}
            },
        },
        timeout=3600.0,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("errors"):
        raise RuntimeError(json.dumps(d["errors"])[:300])
    c = d["data"]["createCompletion"]
    return c, time.time() - t0


async def main_async(a):
    subject = corpus_subject()
    renderer = PromptRenderer(_prompts_dir())
    # Exactly production's namespace shape (curation_actions._render_prompt).
    # The template gates its corpus section on `context.corpus_subject`, so a
    # bare dict renders that section away SILENTLY and would test a different
    # prompt than the one the mission runs.
    review_prompt = renderer.render(
        "curator/review_paper",
        {"input": {}, "context": {"corpus_subject": subject}, "meta": {}},
    )
    assert (
        "THE CORPUS YOU ARE CURATING FOR" in review_prompt
    ), "corpus section missing -- namespace shape is wrong"
    print(f"corpus_subject: {subject[:110]!r}")
    print(f"review prompt: {len(review_prompt):,} chars\n")

    picks_acc, picks_den = sample(a.accepted, a.denied, a.seed)
    print(f"sample: {len(picks_acc)} accepted (size-spread) + {len(picks_den)} denied")
    for sz, k, _ in picks_acc:
        print(f"   ACC  {sz/3.08:>8,.0f} tok  {k[:56]}")
    for sz, k, _ in picks_den:
        print(f"   DEN  {sz/3.08:>8,.0f} tok  {k[:56]}")

    rows = []
    async with httpx.AsyncClient() as client:
        if a.model:
            print(f"\nswapping to {a.model} ...", flush=True)
            r = await client.post(
                a.url, json={"query": SWAP, "variables": {"n": a.model}}, timeout=1800.0
            )
            body = r.json()
            if body.get("errors") or not body.get("data"):
                raise SystemExit(
                    f"swap rejected (is {a.model!r} a registry name on THIS "
                    f"host? the mac uses -mac/-swarm, not -cuda): "
                    f"{json.dumps(body.get('errors'))[:300]}"
                )
            sw = body["data"]["swapModel"]
            if not sw.get("ok"):
                raise SystemExit(f"swap failed: {sw.get('error')}")
            print(f"  ok (prev={sw.get('previous')})", flush=True)

        for truth, picks in (("accepted", picks_acc), ("denied", picks_den)):
            for sz, k, rec in picks:
                doc = curator_doc(k)
                prompt = doc + "\n\n---\n\n" + review_prompt
                try:
                    c, secs = await ask(client, a.url, prompt, a.temperature)
                except Exception as e:  # noqa: BLE001
                    print(f"  [{truth}] {k[:44]}: TRANSPORT {e}", flush=True)
                    rows.append(
                        dict(key=k, truth=truth, verdict="error", detail=str(e)[:200])
                    )
                    continue
                parsed = parse_llm_json(c.get("text") or "")
                v = (parsed or {}).get("verdict") if isinstance(parsed, dict) else None
                got = (
                    "accepted"
                    if v == "accept"
                    else "denied" if v == "deny" else "unparseable"
                )
                rows.append(
                    dict(
                        key=k,
                        truth=truth,
                        verdict=got,
                        agree=(got == truth),
                        seconds=secs,
                        prompt_tokens=c.get("promptTokens"),
                        generated=c.get("generatedTokens"),
                        truncated=bool(c.get("truncated")),
                        summary=str((parsed or {}).get("summary") or "")[:600],
                        issues=[str(i) for i in ((parsed or {}).get("issues") or [])][
                            :8
                        ],
                        muse_summary=str(
                            (rec.get("review") or {}).get("summary")
                            if truth == "accepted"
                            else rec.get("review_summary") or ""
                        )[:600],
                        raw=(c.get("text") or "")[:300],
                    )
                )
                mark = "OK " if got == truth else "MISS"
                print(
                    f"  [{truth[:3]}] {mark} {k[:42]:<44} -> {got:<12} {secs:>6.1f}s",
                    flush=True,
                )

    json.dump(rows, open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}")
    scored = [r for r in rows if r.get("verdict") != "error"]
    for truth in ("accepted", "denied"):
        sub = [r for r in scored if r["truth"] == truth]
        if not sub:
            continue
        ok = sum(1 for r in sub if r["agree"])
        print(f"  {truth:<9} agreement {ok}/{len(sub)} = {100*ok/len(sub):.0f}%")
    if scored:
        ok = sum(1 for r in scored if r["agree"])
        print(f"  OVERALL   {ok}/{len(scored)} = {100*ok/len(scored):.0f}%")
    bad = [r for r in scored if r["verdict"] == "unparseable"]
    if bad:
        print(f"  UNPARSEABLE (would be review_failed in production): {len(bad)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://192.168.1.209:8008/graphql")
    ap.add_argument("--model", default=None)
    ap.add_argument("--accepted", type=int, default=10)
    ap.add_argument("--denied", type=int, default=10)
    ap.add_argument("--temperature", type=float, default=0.28)  # t*0.4 of 0.7
    ap.add_argument("--seed", type=int, default=20260831)
    ap.add_argument("--out", default="/tmp/figdig_bench/quality.json")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
