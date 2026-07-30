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
    prefillMs decodeMs
  }
}"""
SNAP = """
mutation Snap($sessionId: String!, $key: String!) {
  sessionSnapshot(sessionId: $sessionId, key: $key) { key tokens resident }
}"""
PURGE = "mutation Purge($key: String!) { purgeSnapshot(key: $key) }"
END = "mutation End($sessionId: String!) { endSession(sessionId: $sessionId) }"
HEALTH = (
    "query { health { status residentActive sessionStrategy "
    "sessionCanShift residentRequested } }"
)

NEEDLES = {"alpha": "the alpha constant is 7391", "beta": "the beta code is QX-88"}


def build_turn_payload(turn_i: int, target_tokens: int) -> tuple[str, str, str]:
    """A deterministic ~target_tokens turn with its OWN planted fact.

    Returns (prompt, fact_key, fact_value). Deterministic per turn index so
    arms are token-comparable; each turn plants a distinct retrievable fact so
    a later needle can probe ANY depth, including past a window boundary."""
    # HASH-DERIVED, not arithmetic. The first vintage used 7000+31*i, and a
    # model that lost gamma-0 to windowing INFERRED it from surviving siblings
    # (B07/D1 read early_fact_ok=True after their fact's window had dropped) —
    # a linear sequence is a pattern, not a needle. md5 makes each value
    # independent; results are comparable only within a vintage.
    import hashlib
    fact_value = str(4096 + int(hashlib.md5(f"fact:{turn_i}".encode()).hexdigest()[:4], 16))
    fact_key = f"gamma-{turn_i}"
    filler = (
        f"Progress note {turn_i}, segment {{j}}. The apparatus was recalibrated "
        "and the observation ledger reconciled against the reference series "
        "with all deviations annotated for the archive. "
    )
    n = max(1, target_tokens // 28)
    parts = [filler.format(j=j) for j in range(n)]
    parts.insert(n // 2, f"RECORD: the {fact_key} reading is {fact_value}.")
    parts.append("Reply exactly: NOTED")
    return "\n\n".join(parts), fact_key, fact_value


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
    ap.add_argument("--run-id", default="")
    ap.add_argument("--depth", type=int, default=12,
                    help="session turns incl. doc + needle turns")
    ap.add_argument("--turn-tokens", type=int, default=0,
                    help="~tokens of per-turn payload; 0 = the classic 5-token "
                         "'Reply exactly: READY' filler. Realistic agent turns "
                         "are ~900 (the hy3 arm's measured session growth); the "
                         "5-token filler understates full_replay growth ~56x "
                         "and exists for continuity with the two prior matrices")
    args = ap.parse_args()
    c = C(args.endpoint)
    row: dict = {"label": args.label, "errors": []}

    def err(stage, e):
        row["errors"].append(f"{stage}: {e}"[:160])

    health = await c.gql(HEALTH)
    _h = health["health"]
    row["resident_active"] = bool(_h["residentActive"])
    # The EFFECTIVE strategy and the arch's answer, separately: "never requested"
    # and "requested and denied" both leave resident inactive and only the second
    # is a misconfiguration. canShift None = the question could not be put.
    row["session_strategy"] = _h.get("sessionStrategy") or ""
    row["session_can_shift"] = _h.get("sessionCanShift")
    row["resident_requested"] = bool(_h.get("residentRequested"))

    doc = build_doc()
    # ── session continuity: 3 turns, needle at turn 3 ────────────────
    try:
        sid = (await c.gql(START, {"config": {"ttlSeconds": 600}}))["startSession"][
            "sessionId"
        ]
        # DEPTH 12, not 3. Three turns proves the MODE but not the CURVE, and the
        # cost that matters is quadratic: on the 2026-07-29 hy3 arm a full-replay
        # session's per-turn prefill went 8.1s -> 58.8s across ten turns, and
        # turn-3 fresh prefill would have looked merely "a bit high".
        t1 = await c.turn(sid, doc + "\n\nRead the document. Reply exactly: OK")
        row["turn1_fresh"] = int(t1["freshPrefillTokens"] or 0)
        curve = [row["turn1_fresh"]]
        prefill_s = [round(float(t1.get("prefillMs") or 0) / 1000, 1)]
        row["turn_tokens"] = args.turn_tokens
        mid_facts: list[tuple[str, str]] = []
        for i in range(max(0, args.depth - 2)):
            if args.turn_tokens > 0:
                payload, fk, fv = build_turn_payload(i, args.turn_tokens)
                mid_facts.append((fk, fv))
                tn = await c.turn(sid, payload)
            else:
                tn = await c.turn(sid, "Reply exactly: READY")
            curve.append(int(tn["freshPrefillTokens"] or 0))
            prefill_s.append(round(float(tn.get("prefillMs") or 0) / 1000, 1))
        t3 = await c.turn(
            sid, "From the document, what is the alpha constant? Value only."
        )
        curve.append(int(t3["freshPrefillTokens"] or 0))
        prefill_s.append(round(float(t3.get("prefillMs") or 0) / 1000, 1))
        row["fresh_curve"] = curve
        row["prefill_curve_s"] = prefill_s
        row["prefill_total_s"] = round(sum(prefill_s), 1)
        row["turn2_fresh"] = curve[1]
        row["turn3_fresh"] = curve[2]
        row["turn12_fresh"] = curve[-1]
        row["depth"] = len(curve)
        # Flat vs growing, measured over the whole session rather than one step:
        # resident appends only the new turn, so late turns stay near early ones.
        row["curve_growth"] = round(curve[-1] / max(1, curve[1]), 2)
        # The needle is asked at DEPTH 12, so it also answers "did anything fall
        # out of the session on the way" for a resident config that windows.
        row["session_needle_ok"] = "7391" in (t3["text"] or "")
        row["needle_at_depth"] = len(curve)
        if mid_facts:
            # Probe the EARLIEST per-turn fact as well: on a windowed resident
            # session it may legitimately have fallen out — record which, so
            # "forgot because windowed" is distinguishable from "recall broke".
            fk, fv = mid_facts[0]
            tf = await c.turn(
                sid, f"What was the {fk} reading? Value only."
            )
            row["early_fact_ok"] = fv in (tf["text"] or "")
            curve.append(int(tf["freshPrefillTokens"] or 0))
            prefill_s.append(round(float(tf.get("prefillMs") or 0) / 1000, 1))
        # BEHAVIOURAL verdict, independent of what health reports — the point
        # is to catch a config whose declared strategy and observed behaviour
        # disagree.
        #
        # It used to test `turn3_fresh < turn1_fresh / 4`, which assumed turn 1
        # carried a big doc and later turns were nearly empty. That holds for
        # the 5-token filler and CANNOT hold at --turn-tokens 900, where every
        # turn's payload is the same order as the doc: it mislabelled all 17
        # resident cells of run cells_20260729-232251 as "full-replay". No
        # conclusion was ever drawn from it (session_strategy and curve_growth
        # were recorded alongside and are unambiguous), but a wrong field in a
        # results file that gets mined is a trap.
        #
        # The workload-independent discriminator is MONOTONICITY, not size.
        # Full-replay's fresh count is cumulative, so it rises on EVERY turn —
        # by 1.8%/turn on the 5-token filler (cell B10: 1465→1724 over eleven
        # turns) and by ~40%/turn at 900 tok. Resident prefills one turn's
        # payload and is flat. Any ratio threshold that catches B10 also
        # catches the small legitimate steps in a resident curve; counting how
        # many consecutive turns INCREASE needs no threshold tuning.
        #
        # Measured on the body only: turn 1 carries the doc, and the last two
        # entries are the needle + early-fact probes, which are tiny by design
        # and would break monotonicity for both modes.
        _body = curve[1:-2] if len(curve) >= 5 else curve[1:]
        _steps = list(zip(_body, _body[1:]))
        _rising = sum(1 for a, b in _steps if b > a)
        row["session_mode"] = (
            "full-replay"
            if _steps and _rising >= 0.8 * len(_steps)
            else "resident-live"
        )
        # Disagreement between the declared strategy and the observed one is
        # the finding, so record it rather than leaving a reader to diff two
        # fields.
        _declared = "full-replay" if row["session_strategy"] == "full_replay" else "resident-live"
        row["strategy_matches_behaviour"] = row["session_mode"] == _declared
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
