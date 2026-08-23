"""Run the 10-figure held-out set through LLMVP's OWN vision endpoint.

WHY NOT dev/vl_set10_bench.py: that harness drives llama-cpp-python directly
and bypasses everything the framework provides — the family seal
(vision_stop_strings), channel cleaning (vision_text.clean: terminator cut,
longest content pass, marker strip), the instance pool, and model routing.
It is also measurably WORSE: on the 2026-08-11 set the same question served
by POST /v1/vision scored 155/192 against the direct harness's 145/192.
Measure with the polished tool, not the scaffold it replaced.

LOW THINKING BUDGET. Figure transcription is EXTRACTION, not deliberation,
and house policy routes mechanical steps low. run_vision_completion has no
`reasoning` parameter because the mtmd handler builds its prompt from the
MODEL'S OWN chat template rather than our renderer — so the switch is that
template's own directive, appended to BOTH candidates so the prompt stays
byte-identical across models. Measured why this matters: at a 1600-token cap
a thinking qwen3.8 opened <think>, never closed it, and produced NO ANSWER,
while a non-thinking muse spent the whole budget answering. Scoring that
compares thinking modes, not vision.

  llmvp/.venv/bin/python dev/vl_set10_endpoint.py <label>
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
OUT = SCRATCH / "vl_set10_results.jsonl"
SET = json.loads((SCRATCH / "vl_set10.json").read_text())
URL = "http://localhost:8008/v1/vision"

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
NO_THINK_SUFFIX = " /no_think"

label = sys.argv[1]
question = QUESTION + NO_THINK_SUFFIX


def ask(img: str) -> tuple[str, float, dict]:
    payload = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_path", "path": img},
                    ],
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.2,
        }
    ).encode()
    req = urllib.request.Request(
        URL, data=payload, headers={"Content-Type": "application/json"}
    )
    t = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        body = json.loads(r.read())
    dt = time.time() - t
    text = (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return text.strip(), dt, body


for item in SET:
    key, img = item["key"], item["file"]
    try:
        text, dt, body = ask(img)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(
            f"  {label} :: {key}: ERROR {type(e).__name__}: {str(e)[:160]}", flush=True
        )
        with open(OUT, "a") as f:
            f.write(
                json.dumps({"model": label, "key": key, "error": str(e)[:300]}) + "\n"
            )
        continue
    if not text:
        print(f"  {label} :: {key}: EMPTY", flush=True)
        with open(OUT, "a") as f:
            f.write(json.dumps({"model": label, "key": key, "error": "empty"}) + "\n")
        continue
    thinking = "<think>" in text
    print(
        f"  {label} :: {key}: {dt:.0f}s {len(text)} chars"
        + ("  !! THINK BLOCK PRESENT" if thinking else ""),
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
                    "harness": "llmvp_vision",
                    "think_block": thinking,
                }
            )
            + "\n"
        )
