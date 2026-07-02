#!/usr/bin/env python3
"""Curator vision bake-off: figure -> figtext across VLM candidates.

Per model, runs the REAL fig_review pipeline (same prompt, same chat
call — the tool's functions are imported, no drift) over a sample of
corpus figures and writes a side-by-side markdown report for HUMAN
judgment plus deterministic advisory stats (numeric overlap vs the
paper markdown; low overlap on data-bearing figures = fabrication risk
OR added value — the human decides, that's why the report is
side-by-side).

MLX candidates run via a script-owned mlx_vlm.server (one at a time,
serial — unified memory). GGUF dual-duty candidates (gemma-4-31b,
qwen3.6-27b — mmproj files exist) run via llama-mtmd-cli if the binary
is on PATH; skipped gracefully otherwise.

Usage:
    tools/fig_review/.venv/bin/python dev/bakeoff_vision.py \
        --databank ~/corpora/ouroboros-hea/databank [--figures 20] \
        [--models mlx-community/Qwen3-VL-30B-A3B-Instruct-8bit ...]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).parent.parent
RESULTS_DIR = Path(__file__).parent / "bakeoff_results"

DEFAULT_MLX_MODELS = [
    "mlx-community/Qwen3-VL-4B-Instruct-8bit",
    "mlx-community/Qwen3-VL-8B-Instruct-8bit",
    "mlx-community/Qwen3-VL-30B-A3B-Instruct-8bit",
]
MTMD_MODELS = {
    "gemma-4-31b": {
        "gguf": "~/.lmstudio/models/lmstudio-community/gemma-4-31B-it-QAT-GGUF/gemma-4-31B-it-QAT-Q4_0.gguf",
        "mmproj": "~/.lmstudio/models/lmstudio-community/gemma-4-31B-it-QAT-GGUF/mmproj-gemma-4-31B-it-QAT-BF16.gguf",
    },
    "qwen3.6-27b": {
        "gguf": "~/.lmstudio/models/unsloth/Qwen3.6-27B-GGUF/Qwen3.6-27B-Q6_K.gguf",
        "mmproj": "~/.lmstudio/models/unsloth/Qwen3.6-27B-GGUF/mmproj-F32.gguf",
    },
}


def _fig_review():
    spec = importlib.util.spec_from_file_location(
        "fig_review", _ROOT / "tools" / "fig_review" / "fig_review.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _sample_figures(databank: Path, n: int) -> list[tuple[str, Path, str]]:
    """(paper_key, fig_path, caption) — spread across papers, seeded order."""
    fr = _fig_review()
    fig_root = databank / "figures"
    papers = (
        sorted(p for p in fig_root.iterdir() if p.is_dir()) if fig_root.is_dir() else []
    )
    out = []
    # Round-robin one figure per paper, then second figures, until n.
    depth = 0
    while len(out) < n and depth < 8:
        for paper in papers:
            figs = sorted(paper.glob("fig_*.png"))
            if depth < len(figs) and len(out) < n:
                key = paper.name
                md_path = databank / "markdown" / f"{key}.md"
                md = md_path.read_text() if md_path.is_file() else ""
                out.append(
                    (key, figs[depth], fr.caption_context(md, key, figs[depth].name))
                )
        depth += 1
    return out


def _run_mlx_model(model: str, figures, databank: Path) -> dict:
    fr = _fig_review()
    port = fr._free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "mlx_vlm.server", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    results = {}
    try:
        if not fr._wait_health(port):
            return {"__error__": "server failed to start"}
        for key, fig_path, caption in figures:
            md_path = databank / "markdown" / f"{key}.md"
            md = md_path.read_text() if md_path.is_file() else ""
            t0 = time.time()
            try:
                text = fr._chat_figure(port, model, str(fig_path), caption)
                if not text.strip():
                    results[f"{key}/{fig_path.name}"] = {"error": "empty output"}
                    continue
                results[f"{key}/{fig_path.name}"] = {
                    "figtext": text,
                    "overlap": round(fr.numeric_overlap(text, md), 3),
                    "seconds": round(time.time() - t0, 1),
                }
            except Exception as e:  # noqa: BLE001
                results[f"{key}/{fig_path.name}"] = {
                    "error": f"{type(e).__name__}: {e}"
                }
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    return results


def _run_mtmd_model(name: str, paths: dict, figures, databank: Path) -> dict:
    """GGUF dual-duty candidate via llama-mtmd-cli (skip if absent)."""
    cli = shutil.which("llama-mtmd-cli")
    gguf = os.path.expanduser(paths["gguf"])
    mmproj = os.path.expanduser(paths["mmproj"])
    if not cli or not os.path.isfile(gguf) or not os.path.isfile(mmproj):
        return {
            "__skipped__": f"cli={bool(cli)} gguf={os.path.isfile(gguf)} mmproj={os.path.isfile(mmproj)}"
        }
    fr = _fig_review()
    results = {}
    for key, fig_path, caption in figures:
        md_path = databank / "markdown" / f"{key}.md"
        md = md_path.read_text() if md_path.is_file() else ""
        prompt = fr._FIG_PROMPT.format(caption=caption or "(no caption located)")
        t0 = time.time()
        try:
            # Fresh process per figure — the bake-off's crash-isolation lesson.
            proc = subprocess.run(
                [
                    cli,
                    "-m",
                    gguf,
                    "--mmproj",
                    mmproj,
                    "--image",
                    str(fig_path),
                    "-p",
                    prompt,
                    "-n",
                    "800",
                    "--temp",
                    "0.2",
                ],
                capture_output=True,
                text=True,
                timeout=900,
            )
            text = proc.stdout.strip()
            if not text:
                # Empty output scores overlap 1.0 VACUOUSLY (no numerics,
                # no misses) — the same hole as 0-verified-pages. Errors
                # must look like errors.
                results[f"{key}/{fig_path.name}"] = {
                    "error": f"empty output; stderr: {proc.stderr.strip()[-300:]}"
                }
                continue
            results[f"{key}/{fig_path.name}"] = {
                "figtext": text[-4000:],
                "overlap": round(fr.numeric_overlap(text, md), 3),
                "seconds": round(time.time() - t0, 1),
            }
        except Exception as e:  # noqa: BLE001
            results[f"{key}/{fig_path.name}"] = {"error": f"{type(e).__name__}: {e}"}
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--databank", default="~/corpora/ouroboros-hea/databank")
    ap.add_argument("--figures", type=int, default=20)
    ap.add_argument("--models", nargs="*", default=DEFAULT_MLX_MODELS)
    ap.add_argument("--skip-mtmd", action="store_true")
    args = ap.parse_args()

    databank = Path(os.path.expanduser(args.databank))
    figures = _sample_figures(databank, args.figures)
    if not figures:
        raise SystemExit(
            f"no figures under {databank}/figures — run the extractor first"
        )
    print(
        f"{len(figures)} figures sampled across {len({k for k, _, _ in figures})} papers"
    )

    all_results: dict[str, dict] = {}
    for model in args.models:
        print(f"── {model}")
        all_results[model] = _run_mlx_model(model, figures, databank)
    if not args.skip_mtmd:
        for name, paths in MTMD_MODELS.items():
            print(f"── {name} (mtmd)")
            all_results[name] = _run_mtmd_model(name, paths, figures, databank)

    RESULTS_DIR.mkdir(exist_ok=True)
    (RESULTS_DIR / "vision_results.json").write_text(
        json.dumps(all_results, indent=1, ensure_ascii=False)
    )

    # Side-by-side report for human judgment.
    lines = ["# Curator vision bake-off\n"]
    lines.append("## Stats\n")
    lines.append("| model | figs ok | errors | mean overlap | mean s/fig |")
    lines.append("|---|---|---|---|---|")
    for model, res in all_results.items():
        if "__skipped__" in res:
            lines.append(f"| {model} | SKIPPED ({res['__skipped__']}) | | | |")
            continue
        ok = [r for r in res.values() if isinstance(r, dict) and "figtext" in r]
        errs = len(res) - len(ok)
        mo = sum(r["overlap"] for r in ok) / len(ok) if ok else 0
        ms = sum(r["seconds"] for r in ok) / len(ok) if ok else 0
        lines.append(f"| {model} | {len(ok)} | {errs} | {mo:.2f} | {ms:.1f} |")
    lines.append("\n## Side-by-side (judge: faithfulness, no invented values)\n")
    for key, fig_path, caption in figures:
        fig_id = f"{key}/{fig_path.name}"
        lines.append(f"### {fig_id}\n")
        lines.append(f"![]({fig_path})\n")
        lines.append(f"**Caption context:** {caption[:400]}\n")
        for model, res in all_results.items():
            entry = res.get(fig_id) or {}
            body = entry.get("figtext") or entry.get("error") or "(skipped)"
            lines.append(f"**{model}** (overlap {entry.get('overlap', '-')}):\n")
            lines.append(f"> {body[:1200]}\n")
    (RESULTS_DIR / "vision_report.md").write_text("\n".join(lines))
    print(f"wrote {RESULTS_DIR}/vision_report.md and vision_results.json")


if __name__ == "__main__":
    main()
