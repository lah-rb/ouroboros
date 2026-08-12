"""VL bench over the 10-figure held-out set.

The 2-figure bake-off ranked the field but two figures cannot tell a real
capability gap from a fluke, so this runs a held-out set of 10 (5 papers,
4 aspect classes, deliberately mixed figure TYPES: stick pattern, histogram,
dense-text card, scatter, IR spectra, two-panel refractive index, stacked
mineralogy, two reflectance comparisons, and photographs).

TWO HARNESSES, because one model needs it. llama-cpp-python's
GenericMTMDChatHandler returns zero tokens for Qwen3.6-35B-A3B and logs
`find_slot: non-consecutive token position` — it mis-positions the KV after
splicing the image embedding, which the deepstack projector (qwen3vl_merger,
clip.vision.is_deepstack_layers) requires handling for. The upstream C++
llama-mtmd-cli drives the same model+mmproj correctly on an OLDER build, so
--harness cli routes through that instead. Answers stay comparable; WALL CLOCK
DOES NOT, since the CLI reloads the model per image.

  python vl_set10_bench.py <label> <model.gguf> <mmproj.gguf> [--harness cli]
"""

import base64
import json
import subprocess
import sys
import time
from pathlib import Path

SCRATCH = Path(
    "/private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
OUT = SCRATCH / "vl_set10_results.jsonl"
SET = json.loads((SCRATCH / "vl_set10.json").read_text())

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

harness = "py"
argv = sys.argv[1:]
if "--harness" in argv:
    i = argv.index("--harness")
    harness = argv[i + 1]
    del argv[i : i + 2]
label, model_path, mmproj = argv[0], argv[1], argv[2]


def record(row):
    with open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")


def run_cli(img: Path) -> tuple[str, float]:
    t = time.time()
    p = subprocess.run(
        ["llama-mtmd-cli", "-m", model_path, "--mmproj", mmproj,
         "--image", str(img), "-p", QUESTION, "-n", "1600",
         "--temp", "0.2", "-ngl", "99", "-c", "8192"],
        capture_output=True, text=True, timeout=2400,
    )
    return (p.stdout or "").strip(), time.time() - t


if harness == "cli":
    for item in SET:
        img = Path(item["file"])
        text, dt = run_cli(img)
        if not text:
            print(f"  {label} :: {item['key']}: EMPTY")
            record({"model": label, "key": item["key"], "error": "empty",
                    "harness": "cli"})
            continue
        print(f"  {label} :: {item['key']}: {dt:.0f}s {len(text)} chars", flush=True)
        record({"model": label, "key": item["key"], "seconds": round(dt, 2),
                "answer": text, "harness": "cli"})
    raise SystemExit(0)

from llama_cpp import Llama  # noqa: E402
from llama_cpp.llama_chat_format import GenericMTMDChatHandler  # noqa: E402

t0 = time.time()
try:
    llm = Llama(
        model_path=model_path,
        chat_handler=GenericMTMDChatHandler(
            chat_format=None, mmproj_path=mmproj, verbose=False
        ),
        n_ctx=8192, n_gpu_layers=-1, n_batch=2048, verbose=False,
    )
except Exception as e:  # noqa: BLE001 — bench boundary
    print(f"  {label}: LOAD FAILED {type(e).__name__}: {str(e)[:200]}")
    record({"model": label, "error": f"load: {type(e).__name__}: {str(e)[:300]}"})
    raise SystemExit(0)
print(f"  {label}: loaded in {time.time()-t0:.0f}s", flush=True)

for item in SET:
    b64 = base64.b64encode(Path(item["file"]).read_bytes()).decode()
    t = time.time()
    try:
        r = llm.create_chat_completion(
            messages=[{"role": "user", "content": [
                {"type": "text", "text": QUESTION},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            max_tokens=1600, temperature=0.2,
        )
    except Exception as e:  # noqa: BLE001
        print(f"  {label} :: {item['key']}: FAILED {str(e)[:140]}")
        record({"model": label, "key": item["key"], "error": str(e)[:300],
                "harness": "py"})
        continue
    dt = time.time() - t
    msg = r["choices"][0]["message"]
    text = (msg.get("content") or "") + (msg.get("reasoning_content") or "")
    ct = int((r.get("usage") or {}).get("completion_tokens") or 0)
    print(f"  {label} :: {item['key']}: {dt:.0f}s {ct} tok "
          f"{ct/dt if dt else 0:.1f} tok/s", flush=True)
    record({"model": label, "key": item["key"], "seconds": round(dt, 2),
            "completion_tokens": ct, "tok_per_s": round(ct / dt, 2) if dt else 0,
            "answer": text, "harness": "py"})
