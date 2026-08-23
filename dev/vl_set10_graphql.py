"""Run the 10-figure held-out set through LLMVP's GraphQL visionCompletion.

THE CANONICAL PATH. Earlier attempts drove llama-cpp-python directly
(dev/vl_set10_bench.py) and then the /v1 REST shim; both are scaffolding.
`visionCompletion` is what production uses, and going through it buys the
framework's real machinery: the family seal (vision_stop_strings), channel
cleaning (vision_text.clean — terminator cut, longest content pass, marker
strip), the vision instance pool with per-request context clear, and — the
reason this file exists rather than a config swap — `model` routing to a HOT
SECONDARY. Both candidates stay resident and are selected per request, so
nothing is drained between them and neither gets a cold-cache advantage.

It is also measurably better than the direct harness: on the 2026-08-11 set
the same question through the endpoint scored 155/192 against 145/192.

LOW THINKING BUDGET. Figure transcription is EXTRACTION, not deliberation,
and house policy routes mechanical steps low. run_vision_completion takes no
`reasoning` argument because the mtmd handler builds its prompt from the
MODEL'S OWN chat template rather than our renderer — so the switch is that
template's own directive, appended to BOTH candidates so the prompt stays
byte-identical across models.

  llmvp/.venv/bin/python dev/vl_set10_graphql.py <primary_label> [secondary_label ...]
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
OUT = SCRATCH / "vl_set10_results.jsonl"
SET = json.loads((SCRATCH / "vl_set10.json").read_text())
GQL = "http://localhost:8008/graphql"

# Byte-identical to dev/vl_set10_bench.py's QUESTION — a different question
# would break comparability with the recorded bake-off methodology.
QUESTION = (
    "This is a figure from a scientific paper. Extract its content as precisely "
    "as you can, covering: (1) which measurement technique or data type it "
    "shows; (2) how many panels there are and what distinguishes them, using "
    "the panel letters exactly as printed; (3) for each panel the x-axis label "
    "with units, the y-axis label with units, and the numeric range of each "
    "axis; (4) EVERY labelled peak, legend entry, sample name, field label and "
    "annotation, transcribed EXACTLY as printed including capitalisation, "
    "element symbols, chemical formulae and any misspellings you see; (5) the "
    "material or sample measured and any identifiers such as database card "
    "numbers, sample codes, mineral names or radiation wavelength; (6) the "
    "approximate position and height of the most prominent features in the "
    "figure's own units. If you cannot read something, say so rather than "
    "guessing — a stated uncertainty is worth more than a confident invention."
)

# LOW is REQUESTED, not faked. An earlier draft appended the qwen template's
# `/no_think` token to the prompt — a hack that also suppressed thinking
# entirely rather than shortening it, and that only one family understands.
# The dial now goes through the API (`reasoning`), and each family delivers
# its own spelling of low from its own spec: harmony renders
# `Reasoning strength: low`, qwen38 renders its effort sentence. Thinking
# stays ON; it is asked to be brief.
REASONING = "low"

VISION_MUTATION = """
mutation($req: VisionCompletionRequest!) {
  visionCompletion(request: $req) {
    text
    generatedTokens
    promptTokens
    visionModel
    handler
    decodeMs
  }
}
"""
LOAD_MUTATION = """
mutation($name: String!) {
  loadModel(name: $name) { ok name state footprintGb residentCount detail }
}
"""


def gql(query: str, variables: dict, timeout: float = 2400.0) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        GQL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read())
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"])[:400])
    return payload["data"]


labels = sys.argv[1:]
if not labels:
    raise SystemExit("usage: vl_set10_graphql.py <primary_label> [secondary ...]")

# Secondaries go HOT alongside the primary; the primary is whatever the server
# already serves and is addressed by passing model=None.
routed: dict[str, str | None] = {labels[0]: None}
for name in labels[1:]:
    res = gql(LOAD_MUTATION, {"name": name}, timeout=3600.0)["loadModel"]
    if not res["ok"]:
        raise SystemExit(f"loadModel {name} refused: {res.get('detail')}")
    print(
        f"  hot: {name} ({res.get('footprintGb')}GB, {res['residentCount']} resident)",
        flush=True,
    )
    routed[name] = name

for label, route in routed.items():
    for item in SET:
        key, img = item["key"], item["file"]
        req = {
            "prompt": QUESTION,
            "images": [{"path": img}],
            "maxTokens": 4096,
            "temperature": 0.2,
            "reasoning": REASONING,
        }
        if route:
            req["model"] = route
        t = time.time()
        try:
            data = gql(VISION_MUTATION, {"req": req})["visionCompletion"]
        except Exception as e:  # noqa: BLE001 — bench boundary
            print(
                f"  {label} :: {key}: ERROR {type(e).__name__}: {str(e)[:150]}",
                flush=True,
            )
            with open(OUT, "a") as f:
                f.write(
                    json.dumps({"model": label, "key": key, "error": str(e)[:300]})
                    + "\n"
                )
            continue
        dt = time.time() - t
        text = (data.get("text") or "").strip()
        if not text:
            print(f"  {label} :: {key}: EMPTY", flush=True)
            with open(OUT, "a") as f:
                f.write(
                    json.dumps({"model": label, "key": key, "error": "empty"}) + "\n"
                )
            continue
        think = "<think>" in text
        print(
            f"  {label} :: {key}: {dt:.0f}s {data.get('generatedTokens')} tok "
            f"{len(text)} chars" + ("  !! THINK BLOCK" if think else ""),
            flush=True,
        )
        with open(OUT, "a") as f:
            f.write(
                json.dumps(
                    {
                        "model": label,
                        "key": key,
                        "seconds": round(dt, 2),
                        "answer": text,
                        "harness": "llmvp_graphql_vision",
                        "vision_model": data.get("visionModel"),
                        "generated_tokens": data.get("generatedTokens"),
                        "handler": data.get("handler"),
                        "decode_ms": data.get("decodeMs"),
                        "think_block": think,
                    }
                )
                + "\n"
            )
