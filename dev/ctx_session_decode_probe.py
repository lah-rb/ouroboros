#!/usr/bin/env python3
"""Clean decode at 200k occupancy — the production-cache mirror.

Phase A (serial): 4 sessions each ingest a distinct ~50k-token document
(turn 1, tiny reply) — prefill paid ONCE into resident session KV, pool
occupancy climbs to ~200k of 224k live cells.

Phase B (concurrent): all 4 sessions fire turn 2 simultaneously — a
long-form summary (max_tokens 1024). Turn-2 prefill is just the question
(the doc KV is resident), so per-stream decodeMs measures PURE concurrent
decode at full occupancy: Luke's "how strained are we at 200k."

Phase C (wiggle): one more short turn per session + a needle check at
depth via the session (does resident recall still work), then endSession.

Usage: .venv/bin/python ctx_session_decode_probe.py > ctxprobe2.jsonl
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import time
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from ctx_decode_probe import build_doc, wired_gb  # noqa: E402

URL = "http://127.0.0.1:8008/graphql"

TURN_Q = """query($sid:String!,$p:String!,$m:Int!){sessionCompletion(
  request:{sessionId:$sid,prompt:$p,maxTokens:$m,temperature:0.3}){
  text tokensGenerated prefillMs decodeMs generatedTokens}}"""


def gql(q, variables=None, timeout=3000):
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


def turn(sid, prompt, max_tokens):
    t0 = time.time()
    r = gql(TURN_Q, {"sid": sid, "p": prompt, "m": max_tokens})
    wall = round(time.time() - t0, 1)
    if r.get("errors"):
        return {"err": r["errors"][0]["message"][:160], "wall_s": wall}
    c = r["data"]["sessionCompletion"]
    gen = c.get("generatedTokens") or c.get("tokensGenerated") or 0
    dms = c.get("decodeMs") or 0
    return {
        "wall_s": wall,
        "generated": gen,
        "prefill_s": round((c.get("prefillMs") or 0) / 1000, 1),
        "decode_tps": round(gen / (dms / 1000), 1) if dms else None,
        "text": c.get("text") or "",
    }


def main():
    sessions, needles = [], {}
    # Phase A — serial ingestion (prefill paid once per session)
    for i in range(4):
        r = gql("mutation{startSession(config:{ttlSeconds:7200}){sessionId}}")
        sid = r["data"]["startSession"]["sessionId"]
        sessions.append(sid)
        prompt, needle, want = build_doc(50000, seed=777000 + i)
        # strip the QA instruction footer; ask for a tiny ack instead
        doc = prompt.split("\n\n---\n")[0]
        needles[sid] = (needle, want)
        res = turn(sid, doc + "\n\nReply with exactly: INGESTED", 8)
        print(
            json.dumps(
                {
                    "phase": "ingest",
                    "i": i,
                    "sid": sid[:8],
                    **{k: v for k, v in res.items() if k != "text"},
                    "wired_gb": wired_gb(),
                }
            ),
            flush=True,
        )
        if "err" in res:
            print(json.dumps({"phase": "ABORT", "reason": res["err"]}), flush=True)
            return

    # Phase B — concurrent long decode at ~200k occupancy
    def long_turn(sid):
        return sid, turn(
            sid,
            "Write a detailed multi-paragraph summary of the "
            "document: its recurring themes, the kinds of "
            "measurements described, and any constants or "
            "passphrases it defined.",
            1024,
        )

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(long_turn, sessions))
    wall = time.time() - t0
    total = 0
    for sid, res in results:
        needle, want = needles[sid]
        total += res.get("generated", 0) or 0
        print(
            json.dumps(
                {
                    "phase": "decode200k",
                    "sid": sid[:8],
                    **{k: v for k, v in res.items() if k != "text"},
                    "recalls_needle": needle in res.get("text", ""),
                    "recalls_sum_inputs": all(x in res.get("text", "") for x in [])
                    or None,
                }
            ),
            flush=True,
        )
    print(
        json.dumps(
            {
                "phase": "decode200k",
                "SUMMARY": True,
                "agg_tps": round(total / wall, 1),
                "burst_wall_s": round(wall, 1),
                "wired_gb": wired_gb(),
            }
        ),
        flush=True,
    )

    # Phase C — wiggle: one more concurrent short turn (occupancy grows past
    # 204k) + resident needle recall
    def probe_turn(sid):
        needle, want = needles[sid]
        return sid, turn(
            sid,
            "What was the recovery passphrase and what is "
            "ALPHA + BETA? Answer as PASSPHRASE/SUM lines.",
            128,
        )

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(probe_turn, sessions))
    for sid, res in results:
        needle, want = needles[sid]
        print(
            json.dumps(
                {
                    "phase": "wiggle",
                    "sid": sid[:8],
                    **{k: v for k, v in res.items() if k != "text"},
                    "needle_ok": needle in res.get("text", ""),
                    "sum_ok": str(want) in res.get("text", ""),
                }
            ),
            flush=True,
        )

    for sid in sessions:
        gql("mutation($s:String!){endSession(sessionId:$s)}", {"s": sid})
    print(json.dumps({"phase": "done", "wired_gb": wired_gb()}), flush=True)


if __name__ == "__main__":
    main()
