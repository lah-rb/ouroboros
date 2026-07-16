#!/usr/bin/env python3
"""Validate the per-request reasoning HEAD-SWAP (config.model.reasoning_head_swap).

Runs the SAME reasoning-heavy prompt through the SESSION path at reasoning=low,
high, and default(None), each in a FRESH session (turn 0 = where the head-swap
installs). Expectation if the swap works:
  - tokens(low)  <<  tokens(high)   (low head suppresses CoT)
  - every response non-empty + coherent (no KV corruption / empty-strip)
  - repeat low a second time → same ballpark (deterministic, no drift)

Usage: python dev/reasoning_headswap_spike.py   (server must be up)
"""
import json
import urllib.request

GQL = "http://localhost:8008/graphql"
Q = (
    "A farmer has chickens and cows. Together they have 30 heads and 74 legs. "
    "How many chickens and how many cows? Show your reasoning step by step, "
    "then verify the totals."
)


def gql(query, variables):
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(GQL, body, {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req, timeout=300))
    if r.get("errors"):
        raise RuntimeError(str(r["errors"])[:300])
    return r["data"]


def start():
    d = gql(
        "mutation($c: SessionConfig){ startSession(config:$c){ sessionId } }",
        {"c": {"ttlSeconds": 300}},
    )
    return d["startSession"]["sessionId"]


def turn(sid, reasoning):
    req = {"sessionId": sid, "prompt": Q, "maxTokens": 4000, "temperature": 0.0}
    if reasoning:
        req["reasoning"] = reasoning
    d = gql(
        "query($r: SessionTurnRequest!){ sessionCompletion(request:$r){ text tokensGenerated } }",
        {"r": req},
    )
    return d["sessionCompletion"]


def end(sid):
    try:
        gql("mutation($s: String!){ endSession(sessionId:$s) }", {"s": sid})
    except Exception:
        pass


print(f"{'level':10} {'tokens':>8}  {'ans_chars':>9}  answer_tail")
results = {}
for level in ["high", "low", "default", "low"]:
    sid = start()
    try:
        r = turn(sid, None if level == "default" else level)
        tail = (r["text"] or "").replace("\n", " ")[-70:]
        print(f"{level:10} {r['tokensGenerated']:>8}  {len(r['text'] or ''):>9}  {tail!r}")
        results.setdefault(level, []).append(r["tokensGenerated"])
    except Exception as e:
        print(f"{level:10}  ERROR: {e}")
    finally:
        end(sid)

print("\n--- verdict ---")
if results.get("low") and results.get("high"):
    lo = min(results["low"])
    hi = max(results["high"])
    print(f"low={results['low']} high={results['high']} default={results.get('default')}")
    print("HEAD-SWAP WORKS ✅" if lo < hi * 0.8 else "NO CLEAR SWING ⚠ (heads may not differ)")
