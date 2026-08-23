#!/usr/bin/env python3
"""Does a batched engine mix modalities, or do vision and text take turns?

THE DECISION THIS SERVES. The scraper runs two lanes: OCR over page images and
text turns over the extracted markdown. Today they are separate processes on
separate cards, and the OCR lane pays a full toolchain respawn per dispatch
(~50% of its wall). If ONE batched engine can serve a vision request and a text
request concurrently without them serializing, both lanes collapse into a single
resident model — no respawn, no second server, and LLMVP's per-device placement
problem stops mattering because there is only one device in play.

muse is itself a VL model (mmproj-kquant.gguf), so all three arms run against
ONE llama-server instance with `--parallel 2`. That is the point: this measures
what happens INSIDE a batch, not cross-device overlap, which
dev/CUDA_SWARM_2026-08-14.md already put at S = 0.0011 (free).

    text+text     the known-good case, and the control
    vision+vision two image requests in one batch
    vision+text   the mixed case the pipeline actually wants

SERIALIZATION, the same metric as the swarm bench so the numbers compare:

    S = (t_concurrent - max(ta, tb)) / (ta + tb - max(ta, tb))

    S = 0.0  the second request was free — true batching
    S = 1.0  it cost its full solo time — the engine took turns

WHY THIS MIGHT SERIALIZE, stated before measuring. A vision request runs the
projector over the image and injects embeddings before any token is decoded.
That is a compute-bound burst in the middle of a bandwidth-bound decode loop,
and llama.cpp processes an mtmd chunk as its own evaluation. If the server
holds the batch while one slot encodes an image, the mixed arm serializes even
though text+text does not — and the single-engine plan dies. The prediction is
that vision+text sits BETWEEN the two pure arms and closer to the vision one.
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_TEXT_PROMPTS = [
    "Explain why memory bandwidth rather than FLOPs usually bounds token generation.",
    "Describe the trade-offs between a large batch and low latency when serving.",
]
_IMAGE_QUESTION = (
    "Transcribe every axis label, tick value, legend entry and caption in this "
    "figure. Then state what is plotted against what."
)


def wait_ready(port: int, timeout: float = 900.0) -> bool:
    """HTTP 200 on /health — the server binds its port before it loads."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as fh:
                if fh.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def _post(port: int, body: dict, timeout: float = 1800.0) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def text_call(port: int, idx: int, n_predict: int) -> dict:
    t0 = time.time()
    d = _post(
        port,
        {
            "messages": [
                {"role": "user", "content": _TEXT_PROMPTS[idx % len(_TEXT_PROMPTS)]}
            ],
            "max_tokens": n_predict,
            "temperature": 0.0,
            "top_k": 1,
        },
    )
    u = d.get("usage") or {}
    return {
        "kind": "text",
        "seconds": time.time() - t0,
        "tokens": u.get("completion_tokens") or 0,
    }


def vision_call(port: int, data_uri: str, n_predict: int) -> dict:
    t0 = time.time()
    d = _post(
        port,
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _IMAGE_QUESTION},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            "max_tokens": n_predict,
            "temperature": 0.0,
            "top_k": 1,
        },
    )
    u = d.get("usage") or {}
    return {
        "kind": "vision",
        "seconds": time.time() - t0,
        "tokens": u.get("completion_tokens") or 0,
    }


def serialization(ta: float, tb: float, tc: float) -> float:
    slower = max(ta, tb)
    denom = ta + tb - slower
    return (tc - slower) / denom if denom > 0 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--binary", default="/home/lah-rb/Repos/llama.cpp/build/bin/llama-server"
    )
    ap.add_argument(
        "--model", default="/home/lah-rb/models/muse-glimmer-30B-kquant-dynamic.gguf"
    )
    ap.add_argument("--mmproj", default="/home/lah-rb/models/mmproj-kquant.gguf")
    ap.add_argument("--image", required=True)
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--port", type=int, default=8310)
    ap.add_argument("--n-predict", type=int, default=192)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()

    with open(args.image, "rb") as fh:
        data_uri = "data:image/png;base64," + base64.b64encode(fh.read()).decode()

    # -c IS TOTAL AND DIVIDED ACROSS SLOTS. A vision request carries an image's
    # worth of embeddings, so a slot sized like a text slot truncates it — the
    # arm would then measure truncation, not concurrency.
    proc = subprocess.Popen(
        [
            "env",
            f"CUDA_VISIBLE_DEVICES={args.gpu}",
            args.binary,
            "-m",
            args.model,
            "--mmproj",
            args.mmproj,
            "--port",
            str(args.port),
            "--host",
            "127.0.0.1",
            "-ngl",
            "99",
            "-c",
            "16384",
            "--parallel",
            "2",
            "--no-webui",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_ready(args.port):
            print("server never answered 200", file=sys.stderr)
            return 2
        # Warm BOTH paths: the first vision request pays projector setup and
        # the first text request pays kernel autotune. Left unwarmed, whichever
        # arm ran first would carry both costs and read as serialized.
        text_call(args.port, 0, 16)
        vision_call(args.port, data_uri, 16)

        def solo(fn):
            runs = [fn() for _ in range(args.repeats)]
            return statistics.median(r["seconds"] for r in runs), statistics.median(
                r["tokens"] for r in runs
            )

        t_text, tok_text = solo(lambda: text_call(args.port, 0, args.n_predict))
        t_vis, tok_vis = solo(lambda: vision_call(args.port, data_uri, args.n_predict))

        def pair(a, b):
            walls = []
            for _ in range(args.repeats):
                t0 = time.time()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    fa = pool.submit(a)
                    fb = pool.submit(b)
                    ra, rb = fa.result(), fb.result()
                walls.append((time.time() - t0, ra, rb))
            walls.sort(key=lambda w: w[0])
            return walls[len(walls) // 2]

        arms = []
        for tag, mk_a, mk_b, sa, sb in (
            (
                "text+text",
                lambda: text_call(args.port, 0, args.n_predict),
                lambda: text_call(args.port, 1, args.n_predict),
                t_text,
                t_text,
            ),
            (
                "vision+vision",
                lambda: vision_call(args.port, data_uri, args.n_predict),
                lambda: vision_call(args.port, data_uri, args.n_predict),
                t_vis,
                t_vis,
            ),
            (
                "vision+text",
                lambda: vision_call(args.port, data_uri, args.n_predict),
                lambda: text_call(args.port, 0, args.n_predict),
                t_vis,
                t_text,
            ),
        ):
            wall, ra, rb = pair(mk_a, mk_b)
            toks = (ra["tokens"] or 0) + (rb["tokens"] or 0)
            arms.append(
                {
                    "arm": tag,
                    "concurrent_wall_s": round(wall, 2),
                    "solo_a_s": round(sa, 2),
                    "solo_b_s": round(sb, 2),
                    "serialization": round(serialization(sa, sb, wall), 4),
                    "aggregate_speedup": round((sa + sb) / wall, 3),
                    "tokens": toks,
                    "aggregate_tok_s": round(toks / wall, 1),
                }
            )
            print(json.dumps(arms[-1]), flush=True)

        print(
            "\n"
            + json.dumps(
                {
                    "solo_text_s": round(t_text, 2),
                    "solo_text_tokens": tok_text,
                    "solo_vision_s": round(t_vis, 2),
                    "solo_vision_tokens": tok_vis,
                    "arms": arms,
                },
                indent=1,
            )
        )
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
