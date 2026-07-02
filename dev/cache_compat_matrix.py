#!/usr/bin/env python3
"""Cache-mode compatibility probe for the CURRENTLY LOADED model.

One row of the architecture × cache-mode matrix (dev/CACHE_STATE.md):
against a live server whose config REQUESTS the full resident stack
(resident_seq_cache + snapshots), measure what actually engages and
whether correctness holds:

  - resident gate verdict (health.residentActive — the can_shift gate)
  - session continuity mode + cost: 3 turns over a ~1.5k-token doc;
    turn-2/3 freshPrefillTokens tells resident (≈turn only) from
    full-replay (≈whole history) apart
  - needle recall across turns (correctness, not just plumbing)
  - snapshot tier: capture → end → fork → needle → purge; the capture's
    `resident` flag says hot-seq vs replay fallback; the fork turn's
    freshPrefill measures the fork cost

Emits one JSON line; the runner collects rows into the matrix.
    uv run python dev/cache_compat_matrix.py [--endpoint URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx  # noqa: E402

START = """
mutation StartSession($config: SessionConfig!) {
  startSession(config: $config) { sessionId }
}"""
TURN = """
query SessionCompletion($request: SessionTurnRequest!) {
  sessionCompletion(request: $request) {
    text promptTokens cachedPrefixTokens freshPrefillTokens generatedTokens
  }
}"""
SNAP = """
mutation Snap($sessionId: String!, $key: String!) {
  sessionSnapshot(sessionId: $sessionId, key: $key) { key tokens resident }
}"""
PURGE = "mutation Purge($key: String!) { purgeSnapshot(key: $key) }"
END = "mutation End($sessionId: String!) { endSession(sessionId: $sessionId) }"
HEALTH = "query { health { status residentActive } }"

NEEDLES = {"alpha": "the alpha constant is 7391", "beta": "the beta code is QX-88"}


def build_doc(target_tokens: int = 1500) -> str:
    filler = (
        "Section {i}. Samples were prepared, annealed, and archived under "
        "standard laboratory protocol with complete provenance records. "
    )
    n = target_tokens // 25
    parts = []
    for i in range(n):
        parts.append(filler.format(i=i))
        if i == n // 4:
            parts.append(f"IMPORTANT: {NEEDLES['alpha']}.")
        if i == (3 * n) // 4:
            parts.append(f"IMPORTANT: {NEEDLES['beta']}.")
    return "\n\n".join(parts)


class C:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.http = httpx.AsyncClient(timeout=1800.0)

    async def gql(self, q, v=None, ok=True):
        r = await self.http.post(self.endpoint, json={"query": q, "variables": v or {}})
        r.raise_for_status()
        d = r.json()
        if "errors" in d:
            if ok:
                raise RuntimeError(d["errors"][0]["message"])
            return {"__error__": d["errors"][0]["message"]}
        return d["data"]

    async def turn(self, sid, prompt, max_tokens=1024):
        d = await self.gql(
            TURN,
            {
                "request": {
                    "sessionId": sid,
                    "prompt": prompt,
                    "maxTokens": max_tokens,
                    "temperature": 0.0,
                }
            },
        )
        return d["sessionCompletion"]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8008/graphql")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    c = C(args.endpoint)
    row: dict = {"label": args.label, "errors": []}

    def err(stage, e):
        row["errors"].append(f"{stage}: {e}"[:160])

    health = await c.gql(HEALTH)
    row["resident_active"] = bool(health["health"]["residentActive"])

    doc = build_doc()
    # ── session continuity: 3 turns, needle at turn 3 ────────────────
    try:
        sid = (await c.gql(START, {"config": {"ttlSeconds": 600}}))["startSession"][
            "sessionId"
        ]
        t1 = await c.turn(sid, doc + "\n\nRead the document. Reply exactly: OK")
        t2 = await c.turn(sid, "Reply exactly: READY")
        t3 = await c.turn(
            sid, "From the document, what is the alpha constant? Value only."
        )
        row["turn1_fresh"] = int(t1["freshPrefillTokens"] or 0)
        row["turn2_fresh"] = int(t2["freshPrefillTokens"] or 0)
        row["turn3_fresh"] = int(t3["freshPrefillTokens"] or 0)
        row["session_needle_ok"] = "7391" in (t3["text"] or "")
        # Resident appends only the new turn; full-replay re-prefills the
        # whole history (>= turn1's doc size again by turn 3).
        row["session_mode"] = (
            "resident-live"
            if row["turn3_fresh"] < row["turn1_fresh"] / 4
            else "full-replay"
        )
        # ── snapshot tier from the live session ──────────────────────
        try:
            snap = (await c.gql(SNAP, {"sessionId": sid, "key": "compat:doc"}))[
                "sessionSnapshot"
            ]
            row["snapshot_resident"] = bool(snap["resident"])
            await c.gql(END, {"sessionId": sid})
            fsid = (
                await c.gql(
                    START, {"config": {"ttlSeconds": 600, "fromSnapshot": "compat:doc"}}
                )
            )["startSession"]["sessionId"]
            t0 = time.time()
            f = await c.turn(
                fsid, "From the document, what is the beta code? Value only."
            )
            row["fork_fresh"] = int(f["freshPrefillTokens"] or 0)
            row["fork_s"] = round(time.time() - t0, 1)
            row["fork_needle_ok"] = "QX-88" in (f["text"] or "")
            row["fork_mode"] = (
                "hot" if row["fork_fresh"] < row["turn1_fresh"] / 4 else "cold/replay"
            )
            await c.gql(END, {"sessionId": fsid})
        except Exception as e:  # noqa: BLE001
            err("snapshot", e)
            await c.gql(END, {"sessionId": sid}, ok=False)
        finally:
            await c.gql(PURGE, {"key": "compat:doc"}, ok=False)
    except Exception as e:  # noqa: BLE001
        err("session", e)

    print(json.dumps(row, ensure_ascii=False))
    return 0 if not row["errors"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
