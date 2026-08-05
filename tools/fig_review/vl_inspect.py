#!/usr/bin/env python3
"""One-shot image → text: ask the local VLM a question about one image.

Generic sibling of fig_review.py (same venv, same crash-isolation model:
one invocation = one OS process owning its own mlx_vlm.server child;
LLMVP stays text-only by design — vision runs here). Built for agent
missions that hit an image they cannot read as text (GAIA attachments,
UI screenshots): the mission's objective carries the invocation line and
the answer comes back on stdout.

Usage:
  .venv/bin/python vl_inspect.py --image photo.jpg \
      --question "What text appears on the sign?" \
      [--model mlx-community/Qwen3-VL-8B-Instruct-8bit] \
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
from fig_review import _free_port, _wait_health

_DEFAULT_MODEL = "mlx-community/Qwen3-VL-8B-Instruct-8bit"
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
    """OpenAI-compatible chat payload with one image part (pure, testable)."""
    return {
        "model": model,
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


def ask(port: int, model: str, image_path: str, question: str, max_tokens: int) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    mime = _MIME.get(os.path.splitext(image_path)[1].lower(), "image/png")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
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
    ap.add_argument("--model", default=_DEFAULT_MODEL)
    ap.add_argument(
        "--port",
        type=int,
        default=0,
        help="0 = spawn a private mlx_vlm.server; N = reuse one",
    )
    ap.add_argument("--max-tokens", type=int, default=_DEFAULT_MAX_TOKENS)
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        print(f"vl_inspect: no such image: {args.image}", file=sys.stderr)
        return 2

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
            print("vl_inspect: mlx server failed to start", file=sys.stderr)
            return 3
        print(ask(port, args.model, args.image, args.question, args.max_tokens))
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
