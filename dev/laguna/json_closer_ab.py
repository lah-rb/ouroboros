"""Does a closing output-directive make laguna emit JSON on the env step?

Log archaeology could not answer this: n=25, and the direction INVERTED under
filtering (70% compliant without a directive vs 40% with). So: the real prompt,
one variable changed, repeated.

ARM A = the exact prompt that failed in production, verbatim.
ARM B = same prompt + the closing directive the ARCHITECTURE step uses, which
        laguna complies with. That step ends:
        "Now return ONLY the single fenced JSON blueprint ... Do NOT write
         files, code, prose, or commands."
"""

import json, sys, time, urllib.request
from pathlib import Path

BASE = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "5061c6f8-87e3-49d2-a506-1ab58e5b7599/scratchpad/env_prompt.txt"
).read_text()
CLOSER = (
    "\n\nNow return ONLY the single fenced JSON object defined above — the "
    "validation config itself. Do NOT write prose, preamble, explanation, or "
    "commentary; a JSON extractor consumes this directly.\n"
)
URL = "http://127.0.0.1:8008/graphql"
Q = (
    "query($p:String!,$m:Int!,$t:Float!){completion(request:{prompt:$p,maxTokens:$m,temperature:$t})"
    "{text generatedTokens finished}}"
)


def ask(prompt, temp):
    body = json.dumps(
        {"query": Q, "variables": {"p": prompt, "m": 2000, "t": temp}}
    ).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=300))["data"]["completion"]


def is_json(t):
    t = (t or "").lstrip()
    return t.startswith("{") or t.startswith("[") or t.startswith("```json")


REPS = 6
# The agent runs set_env at t*0.2 -> 0.2 with laguna's base of 1.0.
for temp in (0.2, 1.0):
    print(f"\n=== temperature {temp} ({REPS} reps per arm) ===")
    for label, prompt in (
        ("A  as-shipped        ", BASE),
        ("B  + closing directive", BASE + CLOSER),
    ):
        ok = 0
        heads = []
        for _ in range(REPS):
            r = ask(prompt, temp)
            t = r["text"] or ""
            if is_json(t):
                ok += 1
            heads.append(t.lstrip()[:44].replace("\n", " "))
        print(f"  {label}: JSON {ok}/{REPS}")
        for h in heads[:3]:
            print(f"        {h!r}")
