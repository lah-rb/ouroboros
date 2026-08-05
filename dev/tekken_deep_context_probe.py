"""Deep-context session probe — stress the save_state path vs session_full_replay.

Mistral Small 4 is the ONLY fleet config with no `session_full_replay`, so its
sessions use the legacy save_state/load_state KV-surgery path. That path is the
one already implicated elsewhere in this repo: it is what `session_full_replay`
was introduced to bypass for qwen's degeneration, and a gpt-oss control hit
`SystemError: Negative size passed to PyBytes_FromStringAndSize` inside
save_state at deep context.

Critically it also explains "never recovers": a corrupted saved state is
RELOADED every subsequent turn, so once poisoned the session stays poisoned —
unlike a one-off bad sample.

The earlier 12-turn probe missed it because tiny prompts keep the state blob
small. This one deliberately grows context fast (multi-KB prompts) and reports
the turn at which content goes empty, plus whether it ever comes back.

Run it once on the CURRENT config, then again after setting
`session_full_replay: true`, and compare.

Usage:  python dev/tekken_deep_context_probe.py [turns] [label]
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

# ~1.6KB of stable filler per turn so context grows quickly without the model
# needing to reason about it. Content is deliberately mundane.
FILLER = "Reference notes for the world builder. " + " ".join(
    f"Room {i} is connected to room {i + 1} via a corridor and contains a "
    f"lantern, a crate, and a note describing the history of area {i}."
    for i in range(24)
)

ASKS = [
    "Given the notes above, name one room. One line only.",
    'Return JSON only: {"ok": true}',
    "How many rooms were described in the notes? Number only.",
    "Write a one-line Python function that returns its argument.",
]


def gql(query: str, variables: dict | None = None) -> dict:
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=1200) as resp:
        return json.loads(resp.read())


def main() -> None:
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    label = sys.argv[2] if len(sys.argv) > 2 else "arm"

    res = gql(START)
    sid = ((res.get("data") or {}).get("startSession") or {}).get("sessionId")
    if not sid:
        print("could not start session:", res)
        return
    print(f"[{label}] session {sid}, {turns} deep-context turns\n")
    print(f"{'turn':>4} {'tok':>5} {'len':>6} {'trunc':>6}  head")
    print("-" * 72)

    empties = 0
    first_empty = None
    recovered = False
    errors = 0
    try:
        for i in range(turns):
            prompt = f"{FILLER}\n\n(turn {i}) {ASKS[i % len(ASKS)]}"
            temp = 0.1 if i % 4 == 2 else 0.5
            try:
                r = gql(
                    TURN,
                    {
                        "r": {
                            "sessionId": sid,
                            "prompt": prompt,
                            "maxTokens": 200,
                            "temperature": temp,
                        }
                    },
                )
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"{i:>4}  TRANSPORT ERROR: {str(exc)[:110]}")
                continue
            if r.get("errors"):
                errors += 1
                print(f"{i:>4}  GQL ERROR: {str(r['errors'])[:140]}")
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
                recovered = True
            flag = "  <-- EMPTY" if empty else ""
            print(
                f"{i:>4} {tok:>5} {len(text):>6} {str(d.get('truncated')):>6}  "
                f"{text[:40]!r}{flag}"
            )
    finally:
        gql(END, {"s": sid})

    print(f"\n[{label}] empty={empties}/{turns}  errors={errors}")
    if first_empty is not None:
        print(f"[{label}] first empty at turn {first_empty}; recovered={recovered}")
        if not recovered:
            print(f"[{label}] -> TERMINAL: never recovered (matches the report)")
    else:
        print(f"[{label}] -> no empty turns")


if __name__ == "__main__":
    main()
