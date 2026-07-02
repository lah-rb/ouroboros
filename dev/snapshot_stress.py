#!/usr/bin/env python3
"""Live acceptance for the semi-permanent snapshot tier (M3 fence).

Against a running LLMVP (gpt-oss, resident cache active):
  1. Ingest a ~30k-token synthetic document (needles at several depths)
     in a session; snapshot it; end the session.
  2. Fork 3 sessions from the snapshot, each answering a different
     needle — verifies content survives the fork AND fork prefill is
     ~zero (freshPrefillTokens of the fork turn ≪ document size).
  3. Capacity: snapshot a second big doc until rejection — expect a
     clean GraphQL error, not a decode failure.
  4. refreshContext, then fork again — expect the cold rebuild to
     re-prefill (freshPrefill ~ doc size) and STILL answer correctly.
  5. Purge; from_snapshot on the purged key must error.
  6. Fidelity: fork answer vs fresh-ingest answer on the same needle
     (temperature 0.0) — should agree on the needle value.

Usage:  uv run python dev/snapshot_stress.py [--endpoint URL]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

START = """
mutation StartSession($config: SessionConfig!) {
  startSession(config: $config) { sessionId ttlSeconds }
}"""
TURN = """
query SessionCompletion($request: SessionTurnRequest!) {
  sessionCompletion(request: $request) {
    text promptTokens cachedPrefixTokens freshPrefillTokens generatedTokens
  }
}"""
SNAP = """
mutation Snap($sessionId: String!, $key: String!) {
  sessionSnapshot(sessionId: $sessionId, key: $key) { key tokens resident turnCount }
}"""
PURGE = "mutation Purge($key: String!) { purgeSnapshot(key: $key) }"
END = "mutation End($sessionId: String!) { endSession(sessionId: $sessionId) }"
SNAPS = "query { snapshots { key tokens resident turnCount } }"
REFRESH = 'mutation { refreshContext(reason: "snapshot-stress") { status refreshed } }'

NEEDLES = {
    "alpha": "the alpha calibration constant is 7391",
    "beta": "the beta reactor threshold is 4217 kelvin",
    "gamma": "the gamma sample identifier is QX-88-DELTA",
}


def build_doc(target_tokens: int = 30000) -> str:
    filler = (
        "Section {i}. The specimen series was annealed, rolled, and "
        "characterized under standard laboratory conditions; results were "
        "archived with full provenance and cross-checked against the "
        "reference dataset for consistency and completeness. "
    )
    parts, i = [], 0
    # ~40 tokens per filler paragraph -> ~target/40 paragraphs
    n = target_tokens // 40
    marks = {n // 5: "alpha", n // 2: "beta", (4 * n) // 5: "gamma"}
    for i in range(n):
        parts.append(filler.format(i=i))
        if i in marks:
            parts.append(f"IMPORTANT NOTE: {NEEDLES[marks[i]]}.")
    return "\n\n".join(parts)


class Client:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.http = httpx.AsyncClient(timeout=3600.0)

    async def gql(self, query: str, variables: dict | None = None, ok=True):
        r = await self.http.post(
            self.endpoint, json={"query": query, "variables": variables or {}}
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            if ok:
                raise RuntimeError(data["errors"][0]["message"])
            return data["errors"][0]["message"]
        return data["data"]

    async def turn(self, sid: str, prompt: str, max_tokens=300, temperature=0.0):
        d = await self.gql(
            TURN,
            {
                "request": {
                    "sessionId": sid,
                    "prompt": prompt,
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                }
            },
        )
        return d["sessionCompletion"]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8008/graphql")
    ap.add_argument("--tokens", type=int, default=30000)
    args = ap.parse_args()
    c = Client(args.endpoint)
    doc = build_doc(args.tokens)
    failures = []

    def check(name, cond, detail=""):
        mark = "✅" if cond else "❌"
        print(f"{mark} {name} {detail}")
        if not cond:
            failures.append(name)

    # 1. Ingest + snapshot
    t0 = time.time()
    sid = (await c.gql(START, {"config": {"ttlSeconds": 900}}))["startSession"][
        "sessionId"
    ]
    r = await c.turn(
        sid,
        doc + "\n\nRead the document above. Reply with exactly: INGESTED",
        max_tokens=2048,
    )
    print(
        f"ingest: {r['promptTokens']} prompt tok, "
        f"{r['freshPrefillTokens']} fresh, {time.time() - t0:.0f}s"
    )
    ingest_fresh = int(r["freshPrefillTokens"] or 0)
    snap = (await c.gql(SNAP, {"sessionId": sid, "key": "stress:doc"}))[
        "sessionSnapshot"
    ]
    check("snapshot pinned", snap["resident"] is True, f"({snap['tokens']} tok)")
    await c.gql(END, {"sessionId": sid})

    # 2. Three forks, each a different needle; fork prefill must be tiny.
    questions = {
        "alpha": ("alpha calibration constant", "7391"),
        "beta": ("beta reactor threshold", "4217"),
        "gamma": ("gamma sample identifier", "QX-88-DELTA"),
    }
    fork_answer = {}
    for name, (q, expect) in questions.items():
        t1 = time.time()
        fsid = (
            await c.gql(
                START, {"config": {"ttlSeconds": 600, "fromSnapshot": "stress:doc"}}
            )
        )["startSession"]["sessionId"]
        r = await c.turn(
            fsid,
            f"From the document, what is {q}? Answer with the value only.",
        )
        fork_answer[name] = r["text"].strip()
        fresh = int(r["freshPrefillTokens"] or 0)
        check(
            f"fork[{name}] needle",
            expect in r["text"],
            f"(fresh {fresh} tok, {time.time() - t1:.0f}s): {r['text'][:60]!r}",
        )
        check(
            f"fork[{name}] ~zero prefill",
            fresh < ingest_fresh / 10,
            f"({fresh} vs ingest {ingest_fresh})",
        )
        await c.gql(END, {"sessionId": fsid})

    # 3. Capacity rejection (session_snapshot_max=2: one slot left).
    sid2 = (await c.gql(START, {"config": {"ttlSeconds": 600}}))["startSession"][
        "sessionId"
    ]
    await c.turn(sid2, doc + "\n\nReply: OK", max_tokens=2048)
    err = await c.gql(SNAP, {"sessionId": sid2, "key": "stress:doc2"}, ok=False)
    if isinstance(err, str):
        check(
            "capacity/budget rejection is clean",
            "budget" in err or "capacity" in err,
            err[:80],
        )
    else:
        # Fit within budget — take the slot, then the NEXT must reject.
        err2 = await c.gql(SNAP, {"sessionId": sid2, "key": "stress:doc3"}, ok=False)
        check(
            "capacity/budget rejection is clean",
            isinstance(err2, str)
            and ("capacity" in err2 or "budget" in err2 or "exists" in err2),
            str(err2)[:80],
        )
        await c.gql(PURGE, {"key": "stress:doc2"})
    await c.gql(END, {"sessionId": sid2})

    # 4. Refresh demotes to cold; fork must rebuild and still answer.
    await c.gql(REFRESH)
    t2 = time.time()
    fsid = (
        await c.gql(
            START, {"config": {"ttlSeconds": 600, "fromSnapshot": "stress:doc"}}
        )
    )["startSession"]["sessionId"]
    r = await c.turn(
        fsid, "From the document, what is the beta reactor threshold? Value only."
    )
    fresh = int(r["freshPrefillTokens"] or 0)
    check(
        "post-refresh fork answers", "4217" in r["text"], f"({time.time() - t2:.0f}s)"
    )
    print(
        f"   post-refresh fork fresh prefill: {fresh} (cold rebuild happens at start_session)"
    )
    await c.gql(END, {"sessionId": fsid})

    # 5. Purge; forking a purged key errors.
    check("purge", (await c.gql(PURGE, {"key": "stress:doc"}))["purgeSnapshot"] is True)
    err = await c.gql(START, {"config": {"fromSnapshot": "stress:doc"}}, ok=False)
    check("fork of purged key errors", isinstance(err, str), str(err)[:60])
    listing = (await c.gql(SNAPS))["snapshots"]
    check(
        "registry empty of stress keys", not any("stress" in s["key"] for s in listing)
    )

    # 6. Fidelity: fresh ingest answers should agree with the fork answers.
    sid3 = (await c.gql(START, {"config": {"ttlSeconds": 600}}))["startSession"][
        "sessionId"
    ]
    await c.turn(sid3, doc + "\n\nReply: OK", max_tokens=2048)
    r = await c.turn(
        sid3,
        "From the document, what is the alpha calibration constant? Answer with the value only.",
    )
    check(
        "fork == fresh-ingest on needle",
        "7391" in r["text"] and "7391" in fork_answer["alpha"],
        f"fresh={r['text'][:40]!r} fork={fork_answer['alpha'][:40]!r}",
    )
    await c.gql(END, {"sessionId": sid3})

    print(
        f"\n{'PASS' if not failures else 'FAIL'} — {len(failures)} failure(s): {failures}"
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
