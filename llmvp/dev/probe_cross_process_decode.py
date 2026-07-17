#!/usr/bin/env python3
"""Cross-process dual-model concurrent decode probe (P0.b's counterpart).

P0.b showed IN-PROCESS dual-model concurrent decode is dirty (one greedy
divergence, no throughput gain — Metal serializes the kernels). This
probe asks the same question in the CROSS-PROCESS regime that the boss
flow actually uses: LLMVP serving its resident model in-process while
LMStudio serves a second model in its own process, BOTH addressed
through the production GraphQL surface (the remote leg goes through
remote_router -> openai_compat -> LMStudio). "Have we been lucky, or is
the separation durable?"

Protocol per question (P0.b's): solo A x2 and solo B x2 (self-
consistency control — a prompt whose solo runs differ can't byte-
compare), then A+B CONCURRENT, byte-compared against solo. Alternated
pass gives the throughput comparison; cross-process should show real
parallel speedup where in-process showed none.

Run with the production server up, arms paused, LMStudio on :1234:
    .venv/bin/python dev/probe_cross_process_decode.py
"""

import asyncio
import sys
import time

import httpx

ENDPOINT = "http://localhost:8008/graphql"
REMOTE_MODEL = "boss-lmstudio"
MAX_TOKENS = 128

QUESTIONS = [
    "Name the four Galilean moons of Jupiter.",
    "What is the capital of Australia, and what is it NOT?",
    "Explain the difference between a mutex and a semaphore in two sentences.",
    "List the first six prime numbers.",
    "What does the acronym RAID stand for in storage?",
    "Summarise the plot of Hamlet in one sentence.",
]

QUERY = (
    "query($r: CompletionRequest!) { completion(request: $r) "
    "{ text generatedTokens } }"
)


async def ask(client: httpx.AsyncClient, question: str, model: str | None) -> dict:
    r = {"prompt": question, "maxTokens": MAX_TOKENS, "temperature": 0.0}
    if model:
        r["model"] = model
    t0 = time.perf_counter()
    try:
        resp = await client.post(ENDPOINT, json={"query": QUERY, "variables": {"r": r}})
        data = resp.json()
        if data.get("errors"):
            msg = "; ".join(e.get("message", "?") for e in data["errors"])
            return {"error": msg, "s": time.perf_counter() - t0}
        c = data["data"]["completion"]
        return {"text": c["text"], "s": time.perf_counter() - t0}
    except Exception as exc:  # noqa: BLE001 — errors ARE the measurement
        return {"error": f"{type(exc).__name__}: {exc}", "s": time.perf_counter() - t0}


async def main() -> None:
    fails, table = [], []
    async with httpx.AsyncClient(timeout=600.0) as client:
        # Preflight both legs.
        for label, model in (("local", None), ("remote", REMOTE_MODEL)):
            r = await ask(client, "Say OK.", model)
            if "error" in r:
                print(f"PREFLIGHT FAIL [{label}]: {r['error']}")
                sys.exit(1)
            print(f"preflight {label}: ok ({r['s']:.1f}s)")

        for i, q in enumerate(QUESTIONS):
            solo = {}
            for label, model in (("local", None), ("remote", REMOTE_MODEL)):
                r1 = await ask(client, q, model)
                r2 = await ask(client, q, model)
                consistent = (
                    "error" not in r1 and "error" not in r2 and r1["text"] == r2["text"]
                )
                solo[label] = {"r": r1, "consistent": consistent}
                for r in (r1, r2):
                    if "error" in r:
                        fails.append(f"q{i} solo {label}: {r['error']}")

            t0 = time.perf_counter()
            ca, cb = await asyncio.gather(
                ask(client, q, None), ask(client, q, REMOTE_MODEL)
            )
            conc_wall = time.perf_counter() - t0

            for label, c in (("local", ca), ("remote", cb)):
                if "error" in c:
                    fails.append(f"q{i} concurrent {label}: {c['error']}")
                elif (
                    solo[label]["consistent"] and c["text"] != solo[label]["r"]["text"]
                ):
                    fails.append(
                        f"q{i} concurrent {label}: OUTPUT DIVERGED from "
                        f"self-consistent solo"
                    )

            solo_sum = solo["local"]["r"]["s"] + solo["remote"]["r"]["s"]
            table.append((solo_sum, conc_wall))
            cons = "".join(
                "LR"[j] if solo[k]["consistent"] else "."
                for j, k in enumerate(("local", "remote"))
            )
            print(
                f"q{i}: solo-sum {solo_sum:5.1f}s | concurrent {conc_wall:5.1f}s "
                f"| consistent[{cons}]"
            )

    print("\n--- verdict ---")
    if fails:
        print(f"DIRTY — {len(fails)} failure(s):")
        for f in fails:
            print(f"  {f}")
    else:
        speedup = sum(t[0] for t in table) / max(sum(t[1] for t in table), 1e-9)
        print("CLEAN — zero errors, zero divergences across all rounds.")
        print(f"concurrent speedup vs solo-sum: {speedup:.2f}x")
        print("=> cross-process separation is durable, not luck.")


if __name__ == "__main__":
    asyncio.run(main())
