"""Multi-turn SESSION probe for Mistral Small 4 / tekken empty-turn glitch.

The completion-path probe (dev/tekken_think_swallow_probe.py) showed the model
emits no [THINK] at all, so the unclosed-think swallow is latent there. But the
reported glitch is on multi-turn SESSIONS, which differ in ways that can bite:

  * prior assistant turns are replayed into the stream, so a [/THINK] emitted
    in ANY earlier turn makes _bracket_think_start_phase see a closer with no
    opener -> case (2) -> start in THINKING -> swallow from the top;
  * turn-transition tokens (tekken after_generation) enter the stream;
  * the session accumulates, so failures may only appear at depth.

This drives a real session for N turns and reports, per turn, the generated
token count and the extracted content length — so an empty turn is visible the
moment it happens, along with whether it ever recovers.

Usage:  python dev/tekken_session_probe.py [turns]
"""

from __future__ import annotations

import json
import sys
import urllib.request

URL = "http://localhost:8008/graphql"

START = """mutation{ startSession{ sessionId } }"""
TURN = """query($r: SessionTurnRequest!){
  sessionCompletion(request:$r){ text tokensGenerated truncated }
}"""
END = """mutation($s:String!){ endSession(sessionId:$s) }"""


def gql(query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.loads(resp.read())


# Prompts mimic an agent loop: a mix of reasoning, structured output, and
# terse mechanical turns — the shape that precedes the reported failure.
PROMPTS = [
    "List three rooms for a text adventure. One line each.",
    "Now give me a JSON object with keys 'name' and 'exits' for the first room.",
    "Think carefully: if a player carries 3 items and drops 1, then picks up 2, how many? Answer with the number only.",
    "Write a one-line Python function that reverses a string.",
    "Summarize what you have produced so far in one sentence.",
    'Return JSON: {"status": "ok"} and nothing else.',
    "What was the first room you named?",
    "Give one more room name.",
]


def main() -> None:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    res = gql(START)
    sid = ((res.get("data") or {}).get("startSession") or {}).get("sessionId")
    if not sid:
        print("could not start session:", res)
        return
    print(f"session {sid}\n")
    print(f"{'turn':>4} {'tok':>5} {'content_len':>11}  {'temp':>5}  head")
    print("-" * 76)

    empties = 0
    first_empty = None
    recovered_after_empty = False
    try:
        for i in range(turns):
            prompt = PROMPTS[i % len(PROMPTS)]
            # Sweep temperature the way the flows do: some steps run cold.
            temp = 0.1 if i % 4 == 2 else 0.5
            try:
                r = gql(
                    TURN,
                    {
                        "r": {
                            "sessionId": sid,
                            "prompt": prompt,
                            "maxTokens": 300,
                            "temperature": temp,
                        }
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"{i:>4}  TURN ERROR: {exc}")
                continue
            if r.get("errors"):
                print(f"{i:>4}  GQL ERROR: {str(r['errors'])[:160]}")
                continue
            d = (r.get("data") or {}).get("sessionCompletion") or {}
            text = d.get("text") or ""
            tok = d.get("tokensGenerated") or 0
            empty = not text.strip()
            if empty:
                empties += 1
                if first_empty is None:
                    first_empty = i
            elif first_empty is not None:
                recovered_after_empty = True
            flag = "  <-- EMPTY" if empty else ""
            print(
                f"{i:>4} {tok:>5} {len(text):>11}  {temp:>5}  " f"{text[:44]!r}{flag}"
            )
    finally:
        gql(END, {"s": sid})

    print(f"\nempty turns: {empties}/{turns}")
    if first_empty is not None:
        print(f"first empty at turn {first_empty}")
        print(
            "recovered after first empty: "
            f"{recovered_after_empty}"
            + ("" if recovered_after_empty else "   <-- TERMINAL (matches report)")
        )


if __name__ == "__main__":
    main()
