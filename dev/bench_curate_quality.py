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
import re
import sys
import time

import httpx
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.curation_actions import (  # noqa: E402
    _CURATE_OVERSIZE_PARK_MARGIN,
    _CURATE_TURN_OVERHEAD_TOKENS,
    _estimate_doc_tokens,
    _prompts_dir,
    build_curator_doc,
)
from agent.actions.doc_compression import LADDER, compress_rung  # noqa: E402
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
HEALTH = """{ health { inFlight generationActive modelMaxContext nCtxSeq kvPoolTokens
  decodeMode memProcessRssMb memSystemAvailableMb
  capacity { seatsTotal nCtxSeq kvPoolTokens freeCells } } }"""


def usable_tokens(seat_tokens: int) -> int:
    """The doc budget production would grant a lane with this seat: the seat
    less one turn's overhead, under the same margin the oversize park uses."""
    return int(
        (seat_tokens - _CURATE_TURN_OVERHEAD_TOKENS) / _CURATE_OVERSIZE_PARK_MARGIN
    )


def sample_parked(n: int, seat_tokens: int) -> list[tuple[int, str]]:
    """Oversize-parked papers whose recorded floor fits `seat_tokens`, spread
    evenly across the floor range so the arm is not all-small."""
    db: dict = {}
    for path in (f"{D}/papers.jsonl", f"{D}/extraction.jsonl"):
        with open(path) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("paper_key"):
                    db.setdefault(r["paper_key"], {}).update(r)
    usable = usable_tokens(seat_tokens)
    fit = []
    for k, r in db.items():
        if r.get("extraction_status") != "curate_oversize":
            continue
        m = re.search(r"doc floor ~([\d,]+) tokens", r.get("failure_reason") or "")
        if not m:
            continue
        floor = int(m.group(1).replace(",", ""))
        if floor <= usable and os.path.exists(f"{D}/markdown/{k}.md"):
            fit.append((floor, k))
    fit.sort()
    if not fit or n <= 0:
        return []
    idx = np.linspace(0, len(fit) - 1, num=min(n, len(fit))).round().astype(int)
    return [fit[i] for i in dict.fromkeys(idx.tolist())]


def fitted_doc(key: str, usable: int) -> tuple[str, str, int] | None:
    """(doc, rung, tokens): the curator doc at the FIRST ladder rung that fits,
    exactly as production's _build_doc_for walks it -- the English translation
    preferred, the markdown compressed BEFORE figtext is inlined, figtext never
    compressed. None when even the deepest rung is over."""
    md_p = f"{D}/markdown/{key}.en.md"
    if not os.path.exists(md_p):
        md_p = f"{D}/markdown/{key}.md"
    if not os.path.exists(md_p):
        return None
    md = open(md_p, encoding="utf-8", errors="ignore").read()
    fx = None
    ft_p = f"{D}/figtext/{key}.json"
    if os.path.exists(ft_p):
        try:
            fx = json.load(open(ft_p))
        except Exception:
            fx = None
    for rung in LADDER:
        src = md if rung == "raw" else compress_rung(md, rung)
        doc = build_curator_doc(src, fx)
        t = _estimate_doc_tokens(doc)
        if t <= usable:
            return doc, rung, t
    return None


async def health(client, url) -> dict:
    r = await client.post(url, json={"query": HEALTH}, timeout=60.0)
    r.raise_for_status()
    return (r.json().get("data") or {}).get("health") or {}


async def wait_idle(client, url, max_s: float) -> None:
    """Do not swap out from under a turn another lane is still decoding."""
    t0 = time.time()
    while time.time() - t0 < max_s:
        h = await health(client, url)
        if not h.get("inFlight") and not h.get("generationActive"):
            return
        print(
            f"  waiting for the server to go idle (inFlight={h.get('inFlight')}) "
            f"{time.time()-t0:>5.0f}s",
            flush=True,
        )
        await asyncio.sleep(20)
    print("  server still busy after the wait cap -- swapping anyway", flush=True)


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

    picks_parked = sample_parked(a.parked, a.seat_tokens)
    usable = usable_tokens(a.seat_tokens)
    if picks_parked:
        print(
            f"parked arm: {len(picks_parked)} oversize-parked papers, seat "
            f"{a.seat_tokens:,} -> usable doc budget {usable:,} tokens"
        )
        for fl, k in picks_parked:
            print(f"   PRK  {fl:>8,} tok floor  {k[:56]}")

    rows = []
    previous = None
    async with httpx.AsyncClient() as client:
        try:
            h0 = await health(client, a.url)
            print(
                "resident before run: modelMaxContext=%s nCtxSeq=%s kvPoolTokens=%s "
                "decodeMode=%s rss=%.0f MB avail=%.0f MB"
                % (
                    h0.get("modelMaxContext"),
                    h0.get("nCtxSeq"),
                    h0.get("kvPoolTokens"),
                    h0.get("decodeMode"),
                    h0.get("memProcessRssMb") or 0,
                    h0.get("memSystemAvailableMb") or 0,
                ),
                flush=True,
            )
            if a.model:
                await wait_idle(client, a.url, a.wait_idle_s)
                print(f"\nswapping to {a.model} ...", flush=True)
                r = await client.post(
                    a.url,
                    json={"query": SWAP, "variables": {"n": a.model}},
                    timeout=1800.0,
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
                previous = sw.get("previous")
                print(f"  ok (prev={previous})", flush=True)
                h = await health(client, a.url)
                print(
                    "  health: modelMaxContext=%s nCtxSeq=%s kvPoolTokens=%s decodeMode=%s "
                    "rss=%.0f MB avail=%.0f MB seats=%s"
                    % (
                        h.get("modelMaxContext"),
                        h.get("nCtxSeq"),
                        h.get("kvPoolTokens"),
                        h.get("decodeMode"),
                        h.get("memProcessRssMb") or 0,
                        h.get("memSystemAvailableMb") or 0,
                        (h.get("capacity") or {}).get("seatsTotal"),
                    ),
                    flush=True,
                )

            for truth, picks in (("accepted", picks_acc), ("denied", picks_den)):
                for sz, k, rec in picks:
                    doc = curator_doc(k)
                    prompt = doc + "\n\n---\n\n" + review_prompt
                    try:
                        c, secs = await ask(client, a.url, prompt, a.temperature)
                    except Exception as e:  # noqa: BLE001
                        print(f"  [{truth}] {k[:44]}: TRANSPORT {e}", flush=True)
                        rows.append(
                            dict(
                                key=k, truth=truth, verdict="error", detail=str(e)[:200]
                            )
                        )
                        continue
                    parsed = parse_llm_json(c.get("text") or "")
                    v = (
                        (parsed or {}).get("verdict")
                        if isinstance(parsed, dict)
                        else None
                    )
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
                            issues=[
                                str(i) for i in ((parsed or {}).get("issues") or [])
                            ][:8],
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

            for fl, k in picks_parked:
                fd = fitted_doc(k, usable)
                if fd is None:
                    print(
                        f"  [prk] {k[:44]}: no rung fits {usable:,} tokens", flush=True
                    )
                    rows.append(dict(key=k, truth="parked", verdict="unfit", floor=fl))
                    continue
                doc, rung, dtok = fd
                prompt = doc + "\n\n---\n\n" + review_prompt
                try:
                    c, secs = await ask(client, a.url, prompt, a.temperature)
                except Exception as e:  # noqa: BLE001
                    print(f"  [prk] {k[:44]}: TRANSPORT {e}", flush=True)
                    rows.append(
                        dict(
                            key=k,
                            truth="parked",
                            verdict="error",
                            detail=str(e)[:200],
                            rung=rung,
                            doc_tokens=dtok,
                            floor=fl,
                        )
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
                        truth="parked",
                        verdict=got,
                        seconds=secs,
                        prompt_tokens=c.get("promptTokens"),
                        generated=c.get("generatedTokens"),
                        truncated=bool(c.get("truncated")),
                        rung=rung,
                        doc_tokens=dtok,
                        floor=fl,
                        summary=str((parsed or {}).get("summary") or "")[:600],
                        issues=[str(i) for i in ((parsed or {}).get("issues") or [])][
                            :8
                        ],
                        raw=(c.get("text") or "")[:300],
                    )
                )
                print(
                    f"  [prk] {k[:42]:<44} {rung:<6} {dtok:>8,} tok -> {got:<12} "
                    f"{secs:>7.1f}s  prompt={c.get('promptTokens')}",
                    flush=True,
                )

        finally:
            # The challenger must never be left resident by a crash: the remote
            # curate lanes name muse and would error on every turn until restored.
            target = a.restore_model or previous
            if target and not a.no_restore:
                print(f"\nrestoring {target} ...", flush=True)
                r = await client.post(
                    a.url,
                    json={"query": SWAP, "variables": {"n": target}},
                    timeout=1800.0,
                )
                sw = ((r.json().get("data") or {}).get("swapModel")) or {}
                print(
                    f"  restore ok={sw.get('ok')} error={sw.get('error')}", flush=True
                )

    json.dump(rows, open(a.out, "w"), indent=1)
    print(f"\nwrote {a.out}")
    scored = [r for r in rows if r.get("verdict") != "error" and r["truth"] != "parked"]
    for truth in ("accepted", "denied"):
        sub = [r for r in scored if r["truth"] == truth]
        if not sub:
            continue
        ok = sum(1 for r in sub if r["agree"])
        print(f"  {truth:<9} agreement {ok}/{len(sub)} = {100*ok/len(sub):.0f}%")
    if scored:
        ok = sum(1 for r in scored if r["agree"])
        print(f"  OVERALL   {ok}/{len(scored)} = {100*ok/len(scored):.0f}%")
    prk = [r for r in rows if r["truth"] == "parked"]
    if prk:
        done = [r for r in prk if r["verdict"] in ("accepted", "denied")]
        secs = sorted(r["seconds"] for r in done)
        print(
            f"  PARKED    {len(done)}/{len(prk)} returned a verdict "
            f"({sum(1 for r in done if r['verdict']=='accepted')} accept, "
            f"{sum(1 for r in done if r['verdict']=='denied')} deny); "
            f"unparseable {sum(1 for r in prk if r['verdict']=='unparseable')}, "
            f"transport {sum(1 for r in prk if r['verdict']=='error')}, "
            f"unfit {sum(1 for r in prk if r['verdict']=='unfit')}"
            + (f"; median {secs[len(secs)//2]:.0f}s per paper" if secs else "")
        )
    scored = [r for r in scored if r["truth"] != "parked"]
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
    ap.add_argument(
        "--parked", type=int, default=0, help="oversize-parked papers to try"
    )
    ap.add_argument(
        "--seat-tokens", type=int, default=262_144, help="the challenger's seat"
    )
    ap.add_argument("--wait-idle-s", type=float, default=1200.0)
    ap.add_argument(
        "--no-restore", action="store_true", help="leave the challenger resident"
    )
    ap.add_argument(
        "--restore-model",
        default="",
        help="model to restore afterwards (default: whatever the swap displaced)",
    )
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
