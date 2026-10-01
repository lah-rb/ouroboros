#!/usr/bin/env python3
"""Serve the PaddleOCR-VL layout model (PP-DocLayoutV3) over HTTP, on a GPU (tool venv).

extract_batch.py --layout-url http://<host>:<port> sends each page here instead of running
the layout model on its own CPU. Region recognition already goes to LLMVP's paddle; this
moves the other half of the pipeline onto the same box (2026-09-30: layout cost ~2 s per
page on this machine's CPU, about a quarter of each page).

The model is built by the SAME factory the tool uses (extract_batch._vl_pipe_kwargs), so
its configuration cannot drift from the in-process one; only `device` differs.

  .venv/bin/python layout_server.py --device gpu:0 --port 8010 [--host 0.0.0.0]

  GET  /health  -> {"model", "device", "batch_size", "paddle", "cuda"}
  POST /layout  {"images": [encode_image(..)], "kwargs": encode_value({...})}
             -> {"results": [{"boxes": encode_value([...]), "input_path", "page_index"}], "ms"}

Requests are served one model call at a time (the predictor is not thread-safe); several
OCR lanes simply queue here.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import layout_wire as wire  # noqa: E402

log = logging.getLogger("layout_server")


def build_model(device: str):
    """The pipeline's own layout model, on `device`."""
    import paddle
    from paddleocr import PaddleOCRVL

    import extract_batch as eb

    if device.startswith("gpu") and not paddle.device.is_compiled_with_cuda():
        raise SystemExit(
            f"--device {device}: this venv's paddle is a CPU build; install paddlepaddle-gpu"
        )
    pipe = PaddleOCRVL(**eb._vl_pipe_kwargs("llmvp", "", 0), device=device)
    inner = pipe.paddlex_pipeline._pipeline
    return inner.layout_det_model, paddle


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="gpu:0")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8010)
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    model, paddle = build_model(args.device)
    lock = threading.Lock()
    info = {
        "model": "PP-DocLayoutV3",
        "device": args.device,
        "batch_size": int(model.batch_sampler.batch_size),
        "paddle": paddle.__version__,
        "cuda": bool(paddle.device.is_compiled_with_cuda()),
    }
    log.info("layout model ready: %s", info)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._send(200, info)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/layout":
                self._send(404, {"error": "not found"})
                return
            try:
                req = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", 0)))
                )
                images = [wire.decode_image(d) for d in req["images"]]
                kwargs = wire.decode_value(req.get("kwargs") or {})
                t0 = time.monotonic()
                with lock:
                    results = list(model(images, **kwargs))
                ms = (time.monotonic() - t0) * 1000
                out = [
                    {
                        "boxes": wire.encode_value(list(r["boxes"])),
                        "input_path": r.get("input_path"),
                        "page_index": r.get("page_index"),
                    }
                    for r in results
                ]
                log.info("layout: %d page(s) in %.0f ms", len(images), ms)
                self._send(200, {"results": out, "ms": round(ms, 1)})
            except Exception as exc:  # noqa: BLE001 — report, keep serving
                log.exception("layout request failed")
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, *a):  # quiet the per-request access log
            return

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("serving on http://%s:%d", args.host, args.port)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
