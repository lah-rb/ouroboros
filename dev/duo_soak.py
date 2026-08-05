#!/usr/bin/env python3
"""Duo-pool soak: interleaved multi-turn decodes across both persona slots —
the exact shape that exposed the Metal error-latch failures. Run against a
server booted with gpt-oss-120b-a5-duo.

Per round: one turn on the agent session, one on the user_sim session
(sessions pinned across ALL rounds), wired-memory sample, error accounting.
A failed turn is recorded and the soak CONTINUES — with latch-healing in
place the slot should recover by the next round (that recovery is exactly
what this script demonstrates).

Usage: .venv/bin/python dev/duo_soak.py [rounds=30]
Exit 0 iff zero decode errors; exit 2 = errors seen but heals recovered;
exit 1 = a slot stayed dead.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8008/graphql"


def gql(q: str, variables=None, timeout=300):
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


def wired_gb() -> float:
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "wired" in line:
            return round(int(line.split()[-1].rstrip(".")) * 16384 / 1e9, 1)
    return 0.0


def turn(sid: str, prompt: str):
    q = """query($sid:String!,$p:String!){sessionCompletion(
        request:{sessionId:$sid,prompt:$p,maxTokens:120}){text}}"""
    r = gql(q, {"sid": sid, "p": prompt})
    if r.get("errors"):
        return None, r["errors"][0]["message"][:160]
    return r["data"]["sessionCompletion"]["text"], None


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    a = gql("mutation{startSession(config:{ttlSeconds:36000}){sessionId}}")
    u = gql(
        'mutation{startSession(config:{ttlSeconds:36000, persona:"user_sim"}){sessionId}}'
    )
    a_id = a["data"]["startSession"]["sessionId"]
    u_id = u["data"]["startSession"]["sessionId"]
    print(f"sessions: agent={a_id[:8]} user_sim={u_id[:8]} rounds={rounds}")

    errors, heals, consecutive_dead = [], 0, {"agent": 0, "user": 0}
    sids = {"agent": a_id, "user": u_id}
    personas = {"agent": None, "user": "user_sim"}
    for i in range(1, rounds + 1):
        w = wired_gb()
        for label in ("agent", "user"):
            prompt = (
                f"Round {i}: name a prime number above {i * 7}."
                if label == "agent"
                else f"Round {i}: as the customer, ask one short follow-up about your order."
            )
            text, err = turn(sids[label], prompt)
            if err:
                errors.append((i, label, err))
                consecutive_dead[label] += 1
                print(f"[{i:03d}] {label} ERR: {err}")
                # Session-level recovery (what a real caller does): the latch
                # heal terminates the session and heals the slot — start a
                # fresh session and continue. The NEXT round proves the heal.
                p = personas[label]
                persona_part = f', persona:"{p}"' if p else ""
                cfg = "{ttlSeconds:36000" + persona_part + "}"
                ns = gql("mutation{startSession(config:" + cfg + "){sessionId}}")
                fresh = (ns.get("data") or {}).get("startSession")
                if fresh:
                    sids[label] = fresh["sessionId"]
                    heals += 1
                    print(
                        f"[{i:03d}] {label} new session {sids[label][:8]} (slot healed)"
                    )
            else:
                consecutive_dead[label] = 0
                print(f"[{i:03d}] {label} ok ({len(text)}ch) wired={w}G")
            if consecutive_dead[label] >= 3:
                print(
                    f"FATAL: {label} slot dead 3 rounds straight — healing not working"
                )
                return 1
        time.sleep(1)

    h = gql("query{health{availableInstances}}")
    raw = gql(
        "query{health{availableInstances}}"
    )  # counters via backend info not exposed; read log instead
    print(f"\nsoak done: {rounds} rounds, {len(errors)} decode errors")
    for e in errors[:10]:
        print("  ", e)
    for sid in set(sids.values()):
        gql("mutation($s:String!){endSession(sessionId:$s)}", {"s": sid})
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
