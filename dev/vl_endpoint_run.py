"""Run the 10-figure held-out set through LLMVP's OWN vision endpoint.

The bake-off drove llama-cpp-python directly (dev/vl_set10_bench.py). This
drives POST /v1/vision instead, with the SAME question and the SAME figures, so
the comparison answers one question: does the endpoint assemble the prompt
equivalently to the bench, or has a layer changed the model's input?

A large gap in either direction is a plumbing finding, not a model finding.
"""

import json
import time
import urllib.request
from pathlib import Path

FIGS = Path("/Users/lah-rb/ouroboros-runs/vl_bakeoff_20260811/figures")
OUT = Path("/Users/lah-rb/ouroboros-runs/vl_bakeoff_20260811/endpoint_answers.jsonl")
URL = "http://localhost:8008/v1/vision"

# Byte-identical to dev/vl_set10_bench.py — a different question would make the
# comparison meaningless.
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

KEYS = [
    "xrd_stick", "histogram", "card_ocr", "scatter", "ir_spectra",
    "refidx_2panel", "mineralogy", "lunar_a", "lunar_b", "craters",
]

OUT.write_text("")
for key in KEYS:
    img = FIGS / f"{key}.png"
    payload = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": QUESTION},
                        {"type": "image_path", "path": str(img)},
                    ],
                }
            ],
            "max_tokens": 2048,
            "temperature": 0.2,
        }
    ).encode()
    req = urllib.request.Request(
        URL, data=payload, headers={"Content-Type": "application/json"}
    )
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            d = json.loads(r.read())
    except Exception as exc:  # noqa: BLE001 — bench boundary
        print(f"  {key:<14} FAILED {type(exc).__name__}: {str(exc)[:120]}")
        with open(OUT, "a") as f:
            f.write(json.dumps({"key": key, "error": str(exc)[:300]}) + "\n")
        continue
    dt = time.time() - t
    if "error" in d:
        print(f"  {key:<14} REJECTED {d['error'][:100]}")
        continue
    text = d["choices"][0]["message"]["content"] or ""
    ct = (d.get("usage") or {}).get("completion_tokens") or 0
    print(f"  {key:<14} {dt:>5.0f}s {ct:>5} tok {len(text):>6} chars", flush=True)
    with open(OUT, "a") as f:
        f.write(
            json.dumps(
                {
                    "key": key,
                    "seconds": round(dt, 1),
                    "completion_tokens": ct,
                    "answer": text,
                    "harness": "endpoint",
                }
            )
            + "\n"
        )
print(f"\nwrote {OUT}")
