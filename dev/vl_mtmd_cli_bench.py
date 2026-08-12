"""VL bench via the upstream llama-mtmd-cli binary.

WHY A SECOND GGUF HARNESS. dev/vl_gguf_bench.py drives llama-cpp-python's
GenericMTMDChatHandler, which returns ZERO tokens for Qwen3.6-35B-A3B and logs

    find_slot: non-consecutive token position 287 after 237 for sequence 0

i.e. it mis-positions the KV after splicing the image embedding. The mmproj is
correctly paired (name Qwen3.6-35B-A3B, projector_type qwen3vl_merger); the
distinguishing header is `clip.vision.is_deepstack_layers` — Qwen3-VL injects
visual features at MULTIPLE decoder layers, and the C++ mtmd path does that
bookkeeping while our binding does not. The upstream CLI answers the same
figure correctly on an OLDER build (9910 vs our b10355), so this is a binding
gap, not a model or build capability limit.

Same question and same figures as the other harnesses, so ANSWERS are
comparable. WALL CLOCK IS NOT — this reloads the model per image and runs a
different runtime.

  python vl_mtmd_cli_bench.py <label> <model.gguf> <mmproj.gguf> [img_key ...]
"""

import json
import subprocess
import sys
import time
from pathlib import Path

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
ASSETS = SCRATCH / "vl_assets"
OUT = SCRATCH / "vl_gguf_results.jsonl"

# The two-figure set the earlier bake-off scored, by short key.
IMAGES = {
    "libs": "doi_10.1590_s0103-50532007000300002_p7_f1.png",
    "xrd": "doi_10.1017_s0885715624000150_p5_f9.png",
}

QUESTION = (
    "This is a figure from a materials-science paper. Extract its quantitative "
    "content as precisely as you can, covering: (1) which measurement technique "
    "it shows; (2) how many panels there are and what distinguishes them; "
    "(3) for each panel the x-axis label with units, the y-axis label with "
    "units, and the numeric range of each axis; (4) EVERY labelled peak or "
    "annotation, transcribed exactly as printed including element symbols and "
    "wavelength or position values; (5) the material or sample measured and any "
    "identifiers such as database card numbers, chemical formulae, or radiation "
    "wavelength; (6) the approximate position and height of the most prominent "
    "peaks in the figure's own units. If you cannot read something, say so "
    "rather than guessing."
)

label, model_path, mmproj = sys.argv[1], sys.argv[2], sys.argv[3]
keys = sys.argv[4:] or list(IMAGES)


def record(row):
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")


for key in keys:
    img = ASSETS / IMAGES.get(key, key)
    if not img.is_file():
        print(f"  {label} :: {key}: MISSING {img}")
        continue
    t = time.time()
    proc = subprocess.run(
        [
            "llama-mtmd-cli",
            "-m", model_path,
            "--mmproj", mmproj,
            "--image", str(img),
            "-p", QUESTION,
            "-n", "1400",
            "--temp", "0.2",
            "-ngl", "99",
            "-c", "8192",
        ],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    dt = time.time() - t
    # The CLI prints loader chatter on stderr and the answer on stdout.
    text = (proc.stdout or "").strip()
    if not text:
        print(f"  {label} :: {key}: EMPTY (rc={proc.returncode})")
        record({"model": label, "image": key, "error": f"empty rc={proc.returncode}",
                "stderr_tail": (proc.stderr or "")[-400:], "harness": "mtmd-cli"})
        continue
    print(f"  {label} :: {key}: {dt:.0f}s {len(text)} chars", flush=True)
    record({
        "model": label,
        "image": key,
        "seconds": round(dt, 2),
        "answer": text,
        "harness": "mtmd-cli",
    })
