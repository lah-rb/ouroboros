#!/usr/bin/env python3
"""Artificial multi-turn session probe at ~200k occupancy (Luke, 2026-07-21).

Shape: 3 WORKER sessions each hold a distinct ~50k document resident
(~150k pool + growth); 1 COORDINATOR session stays shallow and free. Until
the time limit, rounds alternate:
  even  — scripted fact-probe to a worker (rotating: passphrase / ALPHA /
          BETA / ALPHA+BETA), deterministically SCORED → recall-over-time
          curve for deep resident sessions (the 2/4 wiggle-caveat stress).
  odd   — coordinator reads the last worker answer and GENERATES a
          follow-up question; the worker answers it (realistic integrate→
          ask→answer traffic; scored qualitatively by length>0 only).
Logs per turn: session, turn#, decode tok/s, wall, prefill (cache check),
score. Run with the ctxprobe config (224k pool).

Usage: ctx_multiturn_probe.py [minutes=12] > ctxprobe3.jsonl
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from ctx_decode_probe import build_doc, wired_gb  # noqa: E402

URL = "http://127.0.0.1:8008/graphql"
TURN_Q = """query($sid:String!,$p:String!,$m:Int!){sessionCompletion(
  request:{sessionId:$sid,prompt:$p,maxTokens:$m,temperature:0.3}){
  text prefillMs decodeMs generatedTokens}}"""


def gql(q, variables=None, timeout=1200):
    body = {"query": q}
    if variables:
        body["variables"] = variables
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def turn(sid, prompt, max_tokens=256):
    t0 = time.time()
    r = gql(TURN_Q, {"sid": sid, "p": prompt, "m": max_tokens})
    wall = round(time.time() - t0, 1)
    if r.get("errors"):
        return {"err": r["errors"][0]["message"][:140], "wall_s": wall, "text": ""}
    c = r["data"]["sessionCompletion"]
    gen = c.get("generatedTokens") or 0
    dms = c.get("decodeMs") or 0
    return {
        "wall_s": wall,
        "generated": gen,
        "prefill_s": round((c.get("prefillMs") or 0) / 1000, 1),
        "decode_tps": round(gen / (dms / 1000), 1) if dms else None,
        "text": c.get("text") or "",
    }


def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    deadline = time.time() + minutes * 60

    # ingest 3 workers (serial, prefill paid once)
    workers, truth = [], {}
    for i in range(3):
        sid = gql("mutation{startSession(config:{ttlSeconds:7200}){sessionId}}")[
            "data"
        ]["startSession"]["sessionId"]
        prompt, needle, want = build_doc(50000, seed=888000 + i)
        doc = prompt.split("\n\n---\n")[0]
        # ground truth: needle + the two constants (recover a,b from want? we
        # need them individually — re-derive from the doc text)
        import re

        a = int(re.search(r"ALPHA: (\d+)", doc).group(1))
        b = int(re.search(r"BETA: (\d+)", doc).group(1))
        truth[sid] = {"needle": needle, "a": a, "b": b}
        res = turn(sid, doc + "\n\nReply with exactly: INGESTED", 8)
        workers.append(sid)
        print(
            json.dumps(
                {
                    "phase": "ingest",
                    "i": i,
                    **{k: v for k, v in res.items() if k != "text"},
                    "wired_gb": wired_gb(),
                }
            ),
            flush=True,
        )
        if "err" in res:
            return

    # coordinator: shallow briefing
    coord = gql("mutation{startSession(config:{ttlSeconds:7200}){sessionId}}")["data"][
        "startSession"
    ]["sessionId"]
    turn(
        coord,
        "You are coordinating an investigation over three archived "
        "documents held by three analysts. You will receive their "
        "answers one at a time; after each, you may ask ONE short "
        "follow-up question. Reply with exactly: READY",
        8,
    )

    probes = [
        (
            "passphrase",
            lambda t: (
                "What is the recovery passphrase? Answer with just the passphrase.",
                lambda txt: t["needle"] in txt,
            ),
        ),
        (
            "alpha",
            lambda t: (
                "What is CALIBRATION CONSTANT ALPHA? Answer with just the number.",
                lambda txt: str(t["a"]) in txt,
            ),
        ),
        (
            "beta",
            lambda t: (
                "What is CALIBRATION CONSTANT BETA? Answer with just the number.",
                lambda txt: str(t["b"]) in txt,
            ),
        ),
        (
            "sum",
            lambda t: (
                "What is ALPHA + BETA? Answer with just the integer.",
                lambda txt: str(t["a"] + t["b"]) in txt,
            ),
        ),
    ]

    r = 0
    last_answer = ""
    scored = {"total": 0, "hit": 0}
    while time.time() < deadline:
        w = workers[r % 3]
        t = truth[w]
        if r % 2 == 0:
            name, mk = probes[(r // 2) % len(probes)]
            q, check = mk(t)
            res = turn(w, q, 128)
            ok = check(res.get("text", ""))
            scored["total"] += 1
            scored["hit"] += int(ok)
            print(
                json.dumps(
                    {
                        "phase": "probe",
                        "round": r,
                        "worker": r % 3,
                        "probe": name,
                        "ok": ok,
                        **{k: v for k, v in res.items() if k != "text"},
                    }
                ),
                flush=True,
            )
            last_answer = res.get("text", "")[:400]
        else:
            cq = turn(
                coord,
                'Analyst answer: "'
                + last_answer.replace('"', "'")
                + '"\nAsk ONE short follow-up question about their '
                "document (one sentence, question mark).",
                96,
            )
            question = (
                cq.get("text") or "What else does the document describe?"
            ).strip()[:300]
            res = turn(w, question, 192)
            print(
                json.dumps(
                    {
                        "phase": "followup",
                        "round": r,
                        "worker": r % 3,
                        "coord_tps": cq.get("decode_tps"),
                        "q": question[:90],
                        **{k: v for k, v in res.items() if k != "text"},
                    }
                ),
                flush=True,
            )
            last_answer = res.get("text", "")[:400]
        r += 1

    print(
        json.dumps(
            {
                "phase": "DONE",
                "rounds": r,
                "scored": scored,
                "recall_rate": round(scored["hit"] / max(scored["total"], 1), 3),
                "wired_gb": wired_gb(),
            }
        ),
        flush=True,
    )
    for sid in workers + [coord]:
        gql("mutation($s:String!){endSession(sessionId:$s)}", {"s": sid})


if __name__ == "__main__":
    main()
