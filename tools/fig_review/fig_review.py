#!/usr/bin/env python3
"""Figure → figtext sidecar: a VLM reads each extracted figure into text.

Curator stage support tool. One agent dispatch = one invocation = one
OS process.

THE FIGURES ARE READ BY LLMVP NOW (--vl-backend, default llmvp). This file
used to say "LLMVP is text-only by design; vision runs here"; that stopped
being true on 2026-08-12, and the port followed the same day. The private
mlx_vlm.server child is still available and still crash-isolated, but it is
the opt-in, not the arrangement.

Two things changed together, deliberately. The TRANSPORT: no VLM subprocess
per dispatch, one HTTP call per figure to a server that is already resident,
so the figure work costs no second model load. And the MODEL: the old
FIG_MODEL (Qwen3-VL-8B-8bit) was a "mid-size default" that the 2026-08-11
bake-off had already beaten — its whole family was dominated on speed and
quality in the initial pass — and simply never got replaced. The endpoint
serves that bake-off's winner. Model choice now belongs to LLMVP's config,
which is the point: this tool asks for a figure to be read and does not
decide what reads it.

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
      --out-dir <databank>/figtext \
      [--vl-backend llmvp|mlx] [--llmvp-url http://127.0.0.1:8008] \
      [--model <mlx-vlm model dir>   # --vl-backend mlx only]

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

# ── Resolution floor ─────────────────────────────────────────────────
# MEASURED 2026-08-12. The vision tower tokenises by PIXEL AREA at ~715 px per
# token, so one token spans ~26.7 px of whatever we send. A feature smaller
# than that is averaged into a neighbouring patch and the model falls back to
# completing the pattern from the rest of the figure — which is how a 13-bar
# stacked chart acquired mineral categories that were not in it.
#
# Upscaling a small crop changes the sampling rate relative to the feature and
# is the cheapest general fix: at 1 Mpx one token spans ~15 native px instead
# of ~27, and phantom categories on the test figure fell from 5 per run to 1
# with NO prompt change. It is not a complete fix and is not sold as one.
#
# A FLOOR, NOT A TARGET. Crops already above the floor are left alone —
# downscaling them to hit a number would discard native detail we already have
# in order to save a few seconds.
#
# PLAIN INTERPOLATION ONLY. A learned upscaler (Upscayl-lite 4x, ESRGAN class)
# rewrote glyphs on the same figure — `Botswana` became `Bolswana` in 4 of 4
# runs — and the model transcribed the invention faithfully. Lanczos at the
# same output size and the same token cost was clean.
#
# Cost at the floor: ~1400 image tokens and ~9s prefill, against ~516 and ~3s
# for a typical native panel crop.
_MIN_VISION_PIXELS = int(os.environ.get("OUROBOROS_VISION_MIN_PIXELS", 1_000_000))
# Beyond ~3190px on the long side the server resizes back down, so upscaling
# past it buys tokens and no resolution (measured: 3x cost more than 2x and
# scored worse).
_MAX_VISION_LONG_SIDE = int(os.environ.get("OUROBOROS_VISION_MAX_SIDE", 3190))

# The caption block used to be injected raw, with nothing said about what it
# was for — and the model copied it. Measured 2026-08-12 on 4 figures: 3 of 4
# answers reproduced caption text VERBATIM, up to 199 characters, which the
# curator then reads as a VLM claim about the image. Worse, it corrupts the one
# signal meant to catch that: numeric_overlap_rate scores figtext numerics
# against the paper markdown, so a figtext that echoes the caption scores
# perfect grounding by construction (one such answer measured 1.000).
#
# The caption still earns its place — without it the same figures produced
# 205-943 chars against 1390-2652 with it. So it stays, and its ROLE is stated.
_FIG_PROMPT = """You are reading one figure from a scientific paper on materials science.

CONTEXT ONLY — the paper's caption and surrounding prose, to orient you:
{caption}

That text is not your source and not your output. Do not quote or paraphrase
it. Anything you report must be something you can SEE in the image; where the
context names something you cannot find in the image, say that you cannot see
it rather than repeating the claim.

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


# ── Which VLM reads the figure ────────────────────────────────────────
# llmvp (default): the fleet server's own vision endpoint, serving whatever
# model the active config holds. mlx: a private mlx_vlm.server child, the
# original arrangement, kept because it needs nothing else running.
#
# THE DEFAULT IS ALSO A MODEL CHANGE, and that is the point. FIG_MODEL was
# Qwen3-VL-8B-8bit, a "mid-size default" that the 2026-08-11 bake-off beat:
# the whole Qwen3-VL MLX family entered the initial pass with every other
# mmproj-bearing model and lost on speed AND quality to the larger suite. The
# endpoint serves the winner of the final instead — muse-glimmer-30b, 145/192
# on the 10-figure held-out set, 155/192 re-measured through this endpoint.
#
# KNOWN AND ACCEPTED: muse fabricates on 5 of 10 figures, more than
# qwen3.6-27b's 3. That is tolerable HERE specifically because figtext is a
# CLAIM and is never gated on — numeric_overlap_rate is advisory and the
# curator's review pass judges the inlined figtext in context. Do not carry
# this default into a path that trusts figtext directly.
_VL_BACKENDS = ("llmvp", "mlx")
_DEFAULT_VL_BACKEND = os.environ.get("OUROBOROS_FIG_BACKEND", "llmvp")
_LLMVP_URL = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008").rstrip("/")


def _llmvp_ready(url: str, timeout: float = 5.0) -> bool:
    """LLMVP's health lives on GraphQL — there is no REST /health.

    Probed ONCE rather than waited on: we do not own this server, so an
    absent one is an operator error to report, not a race to sleep through.
    """
    req = urllib.request.Request(
        f"{url}/graphql",
        data=json.dumps({"query": "{ health { status } }"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return ((data.get("data") or {}).get("health") or {}).get("status") == "ok"
    except Exception:
        return False


def upscale_factor(width: int, height: int) -> float:
    """How much to enlarge a crop to clear the resolution floor.

    1.0 means send it unchanged. Pure arithmetic and no I/O, so the policy is
    testable without an image or a server.
    """
    px = width * height
    if px <= 0 or px >= _MIN_VISION_PIXELS:
        return 1.0
    f = (_MIN_VISION_PIXELS / px) ** 0.5
    # Never past the point where the server resizes it back down again.
    long_side = max(width, height)
    if long_side * f > _MAX_VISION_LONG_SIDE:
        f = _MAX_VISION_LONG_SIDE / long_side
    return max(1.0, f)


def _read_at_floor(image_path: str) -> bytes:
    """The image bytes, enlarged to the floor if it sits below it.

    Failure here must never cost the batch a figure: any problem reading or
    resizing falls back to the original bytes, because a small image still
    produces a usable figtext and a crashed dispatch produces none.
    """
    with open(image_path, "rb") as f:
        raw = f.read()
    try:
        import io

        from PIL import Image  # lazy: module scope stays stdlib-only

        im = Image.open(io.BytesIO(raw))
        f_up = upscale_factor(*im.size)
        if f_up <= 1.0:
            return raw
        w, h = int(im.width * f_up), int(im.height * f_up)
        buf = io.BytesIO()
        # LANCZOS, deliberately — see the _MIN_VISION_PIXELS note on what a
        # learned upscaler does to printed text.
        im.convert("RGB").resize((w, h), Image.LANCZOS).save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:  # noqa: BLE001 — never lose a figure to resizing
        print(f"fig_review: resize skipped for {image_path}: {exc}", file=sys.stderr)
        return raw


def _chat_figure(
    endpoint: str, model: str, image_path: str, caption: str, send_model: bool
) -> tuple[str, str]:
    """One vision call with the figure attached. Returns (text, served model).

    Both backends take the SAME OpenAI-shaped message with a base64 data URI
    and answer with the same choices[0].message.content, so only the URL and
    the model field differ. Base64 rather than a path on purpose: LLMVP reads
    paths only under model.vision_image_roots, and the figure lives wherever
    the mission's workspace happens to be.
    """
    b64 = base64.b64encode(_read_at_floor(image_path)).decode()
    payload = {
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
    if send_model:
        # mlx_vlm.server loads per request, so the name IS the model. LLMVP
        # fixed its model at boot and ignores the field.
        payload["model"] = model
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    text = str(data["choices"][0]["message"]["content"] or "").strip()
    return text, str(data.get("model") or model)


def review_paper(
    endpoint: str,
    model: str,
    key: str,
    figures_root: str,
    markdown_dir: str,
    out_dir: str,
    send_model: bool = True,
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
            # "._*" = macOS AppleDouble resource forks (USB/cross-machine
            # stowaways): not images, and feeding one to the vision endpoint
            # is a guaranteed 500. Skip them wherever they appear.
            if f.endswith(".png") and not f.startswith("._")
        )
        entries = []
        served = os.path.basename(model.rstrip("/")) if model else "unknown"
        for fig in figs:
            caption = caption_context(md, key, fig)
            figtext, served = _chat_figure(
                endpoint, model, os.path.join(fig_dir, fig), caption, send_model
            )
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
                # The model the SERVER reports, not the one we asked for —
                # with LLMVP the caller does not choose it, and "which model
                # read this figure" is per-record provenance.
                {"paper_key": key, "model": served, "figs": entries},
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
    ap.add_argument(
        "--vl-backend",
        choices=_VL_BACKENDS,
        default=_DEFAULT_VL_BACKEND,
        help=f"which VLM reads the figures (default: {_DEFAULT_VL_BACKEND})",
    )
    ap.add_argument(
        "--model", default="", help="MLX VLM model directory (--vl-backend mlx only)"
    )
    ap.add_argument(
        "--llmvp-url", default=_LLMVP_URL, help="LLMVP base URL (--vl-backend llmvp)"
    )
    ap.add_argument("--port", type=int, default=0, help="reuse a running mlx server")
    args = ap.parse_args()

    server = None
    if args.vl_backend == "llmvp":
        url = args.llmvp_url.rstrip("/")
        if not _llmvp_ready(url):
            # An unreachable LLMVP is an operator condition, not a transient:
            # nothing here can start it, so say which server and stop.
            print(
                json.dumps(
                    {
                        "error": f"LLMVP not reachable at {url} — start it, or "
                        "pass --vl-backend mlx to use a private mlx server"
                    }
                )
            )
            return 3
        endpoint, send_model = f"{url}/v1/vision", False
    else:
        if not args.model:
            print(json.dumps({"error": "--model is required for --vl-backend mlx"}))
            return 2
        port = args.port or _free_port()
        if not args.port:
            server = subprocess.Popen(
                [sys.executable, "-m", "mlx_vlm.server", "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if not _wait_health(port):
            print(json.dumps({"error": "mlx server failed to start"}))
            return 3
        endpoint, send_model = f"http://127.0.0.1:{port}/v1/chat/completions", True

    try:
        for key in args.keys:
            report = review_paper(
                endpoint,
                args.model,
                key,
                args.figures_root,
                args.markdown_dir,
                args.out_dir,
                send_model,
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
