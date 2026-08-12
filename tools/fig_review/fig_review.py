#!/usr/bin/env python3
"""Figure → figtext sidecar: a VLM reads each extracted figure into text.

Curator stage support tool. One agent dispatch = one invocation = one
OS process owning its own mlx_vlm.server child (the extractor's
crash-isolation model).

This file used to say "LLMVP is text-only by design; vision runs here."
That stopped being true on 2026-08-12: LLMVP now serves vision natively
over POST /v1/vision (mtmd projector bound to the resident model, private
single-sequence context — see llmvp/configs/reference.yaml, the VISION
block). The MLX subprocess here is now a CHOICE, not a necessity, and its
remaining justification is crash isolation plus the MLX-only models it can
reach. Moving this behind the endpoint is a live option; it wants its own
measurement, because FIG_MODEL here predates the 2026-08-11 bake-off.

Per paper: for each databank/figures/<paper_key>/fig_NN.png, locate its
reference in the extracted markdown, take the surrounding paragraphs as
caption context, and ask the VLM for a text representation of the
figure's DATA content (axes, units, series, notable values, trends).
figtext is a CLAIM — it is never gated here (no vision oracle exists);
the advisory numeric_overlap_rate records how many of its numerics also
appear in the paper markdown, and the curator's review pass judges the
inlined figtext in context.

Output: databank/figtext/<paper_key>.json per paper, one JSON report
line per paper on stdout:
  {"paper_key": ..., "figtext_path": ..., "figs": N, "seconds": ..., "error": ""}

Usage:
  .venv/bin/python fig_review.py --keys k1 k2 \
      --figures-root <databank>/figures --markdown-dir <databank>/markdown \
      --out-dir <databank>/figtext --model <mlx-vlm model dir>

Module-level imports are stdlib-only ON PURPOSE: the main repo's tests
import caption_context/numeric_overlap from here without the mlx stack.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

# Mirrors tools/pdf_extract/extract_batch.py (separate venvs — keep in sync).
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d{2,}")

_MAX_CAPTION_CHARS = 1400
_MAX_FIGTEXT_TOKENS = 800

_FIG_PROMPT = """You are reading one figure from a scientific paper on materials science.

Caption / surrounding text from the paper:
{caption}

Describe the figure's DATA content as plain text: what is plotted (axes and
units), the series/conditions shown, notable values, and the trends. Report
numbers VERBATIM only where clearly legible in the image — never estimate or
guess a value you cannot read. If the figure is a schematic or micrograph,
describe what it depicts instead. Be dense and factual; no preamble."""


# ── Pure helpers (imported by repo tests — keep stdlib-only) ──────────


def caption_context(md: str, paper_key: str, fig_name: str) -> str:
    """Paragraphs around the markdown's reference to this figure.

    The extractor rewrites image refs to ../figures/<key>/<fig>.png; the
    enclosing paragraph plus one neighbor each side approximates the
    caption + citing sentence. Missing reference -> empty string (the
    figure was extracted but the layout model didn't anchor it).
    """
    ref = f"figures/{paper_key}/{fig_name}"
    paragraphs = md.split("\n\n")
    for i, para in enumerate(paragraphs):
        if ref in para:
            window = paragraphs[max(0, i - 1) : i + 2]
            text = "\n\n".join(window)
            # Strip the image/html markup itself; keep prose.
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
            return text[:_MAX_CAPTION_CHARS]
    return ""


def numeric_overlap(figtext: str, md: str) -> float:
    """Advisory: fraction of figtext numerics that appear in the paper
    markdown. Low overlap is NOT failure — figures legitimately show
    values the prose omits — it flags where review attention should go.
    """
    tokens = _NUM_RE.findall(figtext)
    if not tokens:
        return 1.0
    compact = re.sub(r"\s", "", md)
    return sum(1 for t in tokens if t in md or t in compact) / len(tokens)


# ── Server plumbing (mirrors extract_batch.py) ────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(port: int, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
            return True
        except Exception:
            time.sleep(2)
    return False


def _chat_figure(port: int, model: str, image_path: str, caption: str) -> str:
    """One OpenAI-compatible chat call with the figure attached."""
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": model,
        "max_tokens": _MAX_FIGTEXT_TOKENS,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _FIG_PROMPT.format(
                            caption=caption or "(no caption located)"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return str(data["choices"][0]["message"]["content"] or "").strip()


def review_paper(
    port: int, model: str, key: str, figures_root: str, markdown_dir: str, out_dir: str
) -> dict:
    t0 = time.time()
    report = {
        "paper_key": key,
        "figtext_path": "",
        "figs": 0,
        "seconds": 0.0,
        "error": "",
    }
    try:
        fig_dir = os.path.join(figures_root, key)
        md_path = os.path.join(markdown_dir, f"{key}.md")
        md = open(md_path).read() if os.path.isfile(md_path) else ""
        figs = sorted(
            f
            for f in (os.listdir(fig_dir) if os.path.isdir(fig_dir) else [])
            if f.endswith(".png")
        )
        entries = []
        for fig in figs:
            caption = caption_context(md, key, fig)
            figtext = _chat_figure(port, model, os.path.join(fig_dir, fig), caption)
            entries.append(
                {
                    "fig": fig,
                    "caption": caption,
                    "figtext": figtext,
                    "numeric_overlap_rate": round(numeric_overlap(figtext, md), 4),
                }
            )
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{key}.json")
        with open(out_path, "w") as f:
            json.dump(
                {
                    "paper_key": key,
                    "model": os.path.basename(model.rstrip("/")),
                    "figs": entries,
                },
                f,
                ensure_ascii=False,
                indent=1,
            )
        report["figtext_path"] = out_path
        report["figs"] = len(entries)
    except Exception as e:  # noqa: BLE001 - report, don't crash the batch
        report["error"] = f"{type(e).__name__}: {e}"
    report["seconds"] = round(time.time() - t0, 1)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", nargs="+", required=True)
    ap.add_argument("--figures-root", required=True)
    ap.add_argument("--markdown-dir", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--model", required=True, help="MLX VLM model directory")
    ap.add_argument("--port", type=int, default=0, help="reuse a running server")
    args = ap.parse_args()

    port = args.port or _free_port()
    server = None
    if not args.port:
        server = subprocess.Popen(
            [sys.executable, "-m", "mlx_vlm.server", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        if not _wait_health(port):
            print(json.dumps({"error": "mlx server failed to start"}))
            return 3
        for key in args.keys:
            report = review_paper(
                port,
                args.model,
                key,
                args.figures_root,
                args.markdown_dir,
                args.out_dir,
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
