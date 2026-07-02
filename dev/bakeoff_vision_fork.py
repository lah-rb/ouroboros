#!/usr/bin/env python3
"""Vision bake-off lane through LLMVP's llama-cpp-python fork (mtmd).

The one-runtime consolidation probe: the fork ships libmtmd + a
dedicated Gemma4ChatHandler, and its llama.cpp core is new enough for
the gemma4 TEXT architecture (the system llama-mtmd-cli is not). Runs
ONE model per invocation (crash isolation — load failures or Metal
deaths never take sibling lanes down) over the SAME 20-figure sample
and prompt as dev/bakeoff_vision.py, appending a comparable row.

STATUS 2026-07-02 (later): UNBLOCKED by the fork advance to 0.3.40
(rev 8b38e72c) — gemma4uv projector supported; both models run 20/20.

Run under llmvp's venv:
    llmvp/.venv/bin/python dev/bakeoff_vision_fork.py --model gemma-4-12b
    llmvp/.venv/bin/python dev/bakeoff_vision_fork.py --model gemma-4-31b
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
RESULTS_DIR = Path(__file__).parent / "bakeoff_results"

MODELS = {
    "gemma-4-12b": {
        "gguf": "~/.lmstudio/models/lmstudio-community/gemma-4-12B-it-QAT-GGUF/gemma-4-12B-it-QAT-Q4_0.gguf",
        "mmproj": "~/.lmstudio/models/lmstudio-community/gemma-4-12B-it-QAT-GGUF/mmproj-gemma-4-12B-it-QAT-BF16.gguf",
    },
    "gemma-4-31b": {
        "gguf": "~/.lmstudio/models/lmstudio-community/gemma-4-31B-it-QAT-GGUF/gemma-4-31B-it-QAT-Q4_0.gguf",
        "mmproj": "~/.lmstudio/models/lmstudio-community/gemma-4-31B-it-QAT-GGUF/mmproj-gemma-4-31B-it-QAT-BF16.gguf",
    },
}


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=sorted(MODELS), required=True)
    ap.add_argument("--databank", default="~/corpora/ouroboros-hea/databank")
    ap.add_argument("--figures", type=int, default=20)
    args = ap.parse_args()

    fr = _load(_ROOT / "tools" / "fig_review" / "fig_review.py", "fig_review")
    bv = _load(_ROOT / "dev" / "bakeoff_vision.py", "bakeoff_vision")
    databank = Path(os.path.expanduser(args.databank))
    figures = bv._sample_figures(databank, args.figures)
    if not figures:
        raise SystemExit("no figures to sample")

    from llama_cpp import Llama
    from llama_cpp.llama_chat_format import Gemma4ChatHandler

    paths = MODELS[args.model]
    t_load = time.time()
    # thinking OFF: figtext is a dense factual reading, and the other
    # lanes don't spend tokens on CoT — apples to apples.
    handler = Gemma4ChatHandler(
        clip_model_path=os.path.expanduser(paths["mmproj"]),
        enable_thinking=False,
        verbose=False,
    )
    llm = Llama(
        model_path=os.path.expanduser(paths["gguf"]),
        chat_handler=handler,
        n_ctx=8192,
        n_gpu_layers=-1,
        verbose=False,
    )
    print(f"loaded {args.model} in {time.time() - t_load:.0f}s")

    results = {}
    for key, fig_path, caption in figures:
        md_path = databank / "markdown" / f"{key}.md"
        md = md_path.read_text() if md_path.is_file() else ""
        b64 = base64.b64encode(fig_path.read_bytes()).decode()
        t0 = time.time()
        try:
            out = llm.create_chat_completion(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": fr._FIG_PROMPT.format(
                                    caption=caption or "(no caption located)"
                                ),
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{b64}"
                                },
                            },
                        ],
                    }
                ],
                max_tokens=fr._MAX_FIGTEXT_TOKENS,
                temperature=0.2,
            )
            text = str(out["choices"][0]["message"]["content"] or "").strip()
            if not text:
                results[f"{key}/{fig_path.name}"] = {"error": "empty output"}
                continue
            results[f"{key}/{fig_path.name}"] = {
                "figtext": text[:4000],
                "overlap": round(fr.numeric_overlap(text, md), 3),
                "seconds": round(time.time() - t0, 1),
            }
            print(f"  {key[:36]:36s}/{fig_path.name} {time.time() - t0:5.1f}s")
        except Exception as e:  # noqa: BLE001 — book, continue
            results[f"{key}/{fig_path.name}"] = {"error": f"{type(e).__name__}: {e}"}
            print(f"  {key[:36]:36s}/{fig_path.name} ERROR {e}")

    RESULTS_DIR.mkdir(exist_ok=True)
    merged_path = RESULTS_DIR / "vision_results.json"
    merged = json.loads(merged_path.read_text()) if merged_path.is_file() else {}
    merged[f"{args.model} (fork-mtmd)"] = results
    merged_path.write_text(json.dumps(merged, indent=1, ensure_ascii=False))

    ok = [r for r in results.values() if "figtext" in r]
    errs = len(results) - len(ok)
    mo = sum(r["overlap"] for r in ok) / len(ok) if ok else 0
    ms = sum(r["seconds"] for r in ok) / len(ok) if ok else 0
    row = f"| {args.model} (fork-mtmd) | {len(ok)} | {errs} | {mo:.2f} | {ms:.1f} |"
    print(row)
    with open(RESULTS_DIR / "vision_report.md", "a") as f:
        f.write(f"\n{row}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
