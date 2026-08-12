#!/usr/bin/env python3
"""One-shot image → text: ask the local VLM a question about one image.

Generic sibling of fig_review.py (same venv, same crash-isolation model:
one invocation = one OS process owning its own mlx_vlm.server child).
Built for agent missions that hit an image they cannot read as text (GAIA
attachments, UI screenshots): the mission's objective carries the
invocation line and the answer comes back on stdout.

ANSWERS COME FROM LLMVP NOW (--vl-backend, default llmvp). The header used
to say "LLMVP stays text-only by design — vision runs here"; that stopped
being true on 2026-08-12 and this tool followed fig_review onto the
endpoint the same day. The mlx path remains for a station with no server
running, but it needs an explicit --model: the old default was Qwen3-VL-8B,
whose family lost the bake-off's initial pass on speed and quality and
whose weights are no longer on disk.

KNOWN LIMITATION of the endpoint path, measured on the port: a REASONING
model can leak its deliberation into the answer. The mtmd handler builds
the prompt from the model's own chat template, so the vision path bypasses
LLMVP's format machinery and its FSM channel extraction entirely — there
is nothing separating an analysis channel from a final one. A descriptive
instruction ("describe X; no preamble") comes back clean; an open question
can come back as "Probably… Might also be… I'll respond:". THIS TOOL'S
STDOUT BECOMES A SIDECAR THE AGENT READS, so phrase --question as an
instruction, not a riddle, until the endpoint learns to strip channels.

Usage:
  .venv/bin/python vl_inspect.py --image photo.jpg \
      --question "What text appears on the sign?" \
      [--vl-backend llmvp|mlx] [--llmvp-url http://127.0.0.1:8008] \
      [--model <mlx model>  # required for --vl-backend mlx] \
      [--port 0 (spawn; N = reuse a running server)] [--max-tokens 800]

stdout = the model's answer (exit 0); errors go to stderr (exit non-0).

Module-level imports are stdlib-only ON PURPOSE (fig_review's convention)
so tests can import build_payload without the mlx stack.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

# Server plumbing shared with fig_review.py (same directory → importable
# when invoked by path; also stdlib-only at module level).
from fig_review import (
    _DEFAULT_VL_BACKEND,
    _LLMVP_URL,
    _VL_BACKENDS,
    _free_port,
    _llmvp_ready,
    _wait_health,
)

# NO DEFAULT MODEL. This was "mlx-community/Qwen3-VL-8B-Instruct-8bit" until
# 2026-08-12, when that family was retired (beaten on speed AND quality in the
# bake-off's initial pass) and its weights deleted. A name here would make the
# mlx path silently re-download 9.2GB of a model we chose to stop using, so
# the mlx backend now REQUIRES --model and the default backend needs none.
_DEFAULT_MODEL = ""
_DEFAULT_MAX_TOKENS = 800

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def build_payload(
    model: str,
    image_b64: str,
    mime: str,
    question: str,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> dict:
    """OpenAI-compatible chat payload with one image part (pure, testable).

    Both backends take this same shape; `model` is omitted when falsy, which
    is the llmvp case — that server fixed its model at boot and naming one
    here could only disagree with it.
    """
    payload = {
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    if model:
        payload["model"] = model
    return payload


def ask(
    endpoint: str, model: str, image_path: str, question: str, max_tokens: int
) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    mime = _MIME.get(os.path.splitext(image_path)[1].lower(), "image/png")
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(build_payload(model, b64, mime, question, max_tokens)).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return str(data["choices"][0]["message"]["content"] or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--question", required=True)
    ap.add_argument("--model", default=_DEFAULT_MODEL, help="--vl-backend mlx only")
    ap.add_argument(
        "--vl-backend",
        choices=_VL_BACKENDS,
        default=_DEFAULT_VL_BACKEND,
        help=f"which VLM answers (default: {_DEFAULT_VL_BACKEND})",
    )
    ap.add_argument("--llmvp-url", default=_LLMVP_URL)
    ap.add_argument(
        "--port",
        type=int,
        default=0,
        help="0 = spawn a private mlx_vlm.server; N = reuse one (mlx only)",
    )
    ap.add_argument("--max-tokens", type=int, default=_DEFAULT_MAX_TOKENS)
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print(f"vl_inspect: no such image: {args.image}", file=sys.stderr)
        return 2

    server = None
    if args.vl_backend == "llmvp":
        url = args.llmvp_url.rstrip("/")
        if not _llmvp_ready(url):
            print(
                f"vl_inspect: LLMVP not reachable at {url} — start it, or pass "
                "--vl-backend mlx --model <mlx model>",
                file=sys.stderr,
            )
            return 3
        endpoint, model = f"{url}/v1/vision", ""
    else:
        if not args.model:
            print(
                "vl_inspect: --vl-backend mlx requires --model (there is no "
                "default any more — the Qwen3-VL family was retired)",
                file=sys.stderr,
            )
            return 2
        port = args.port or _free_port()
        if not args.port:
            server = subprocess.Popen(
                [sys.executable, "-m", "mlx_vlm.server", "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if not _wait_health(port):
            print("vl_inspect: mlx server failed to start", file=sys.stderr)
            return 3
        endpoint, model = f"http://127.0.0.1:{port}/v1/chat/completions", args.model

    try:
        print(ask(endpoint, model, args.image, args.question, args.max_tokens))
        return 0
    except Exception as e:  # noqa: BLE001 — CLI boundary: report, non-zero exit
        print(f"vl_inspect: {e}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    sys.exit(main())
