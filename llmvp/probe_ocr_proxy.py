#!/usr/bin/env python3
"""Logging pass-through in front of LLMVP, to capture the REAL OCR request mix.

WHY A PROXY. The stage-split question ("how much of an OCR request is
decode?") cannot be answered with hand-made inputs. paddlex runs its own
layout model and sends TIGHT REGION CROPS with its own prompt; a
hand-cropped page band is a different distribution, and paddle degenerates
on it ("The text in the image is the last part of the image...") while
transcribing a real page cleanly. Measuring the fabricated shape would
have produced a confident number about a request OCR never makes.

So: point extract_batch.py at this proxy instead of at the server
(OUROBOROS_LLMVP_URL), run the real tool over real PDFs, and record what
actually crosses the wire — crop pixel area, tokens generated, wall time,
concurrency in flight.

WHAT IT ENABLES. Output length varies naturally across crops, and so does
crop area, so the production trace itself carries the regression variance
an artificial max_tokens sweep would have had to manufacture:

    wall = a + b*completion_tokens + c*pixel_area

b*tokens is the decode term; a + c*area is encode + prefill + fixed. The
area regressor matters because bigger regions hold more text, so crop size
and output length are CORRELATED — a single-variable fit would credit the
decode slope with encode cost and overstate decode's share, which is the
direction that would wrongly justify building the batched path.

Overhead is a localhost hop (~1 ms against ~500 ms requests); it applies
equally to every row and cancels out of the ratio.

RUN:
  llmvp/.venv/bin/python llmvp/probe_ocr_proxy.py --port 8010 \
      --upstream http://127.0.0.1:8008 --out probe_out/ocr_wire.jsonl
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_lock = threading.Lock()
_inflight = 0
_sink = None
_upstream = "http://127.0.0.1:8008"
_DATA_URI = re.compile(rb'"url"\s*:\s*"data:image/[a-zA-Z]+;base64,([A-Za-z0-9+/=]+)"')


def _image_dims(raw: bytes) -> tuple[int, int]:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        return im.width, im.height
    except Exception:  # noqa: BLE001 — a dim we cannot read is not fatal
        return (0, 0)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence per-request stderr noise
        return

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler API
        global _inflight
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""

        # Measure the image WITHOUT parsing the whole JSON: these bodies are
        # multi-MB base64 and json.loads on every request would show up in the
        # very timing we are trying to measure.
        area = w = h = 0
        n_images = 0
        for m in _DATA_URI.finditer(body):
            n_images += 1
            if area == 0:
                try:
                    raw = base64.b64decode(m.group(1))
                    w, h = _image_dims(raw)
                    area = w * h
                except Exception:  # noqa: BLE001
                    pass

        with _lock:
            _inflight += 1
            conc = _inflight
        t0 = time.time()
        status = 0
        out = b""
        try:
            req = urllib.request.Request(
                _upstream + self.path,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                status = resp.status
                out = resp.read()
        except Exception as exc:  # noqa: BLE001 — record, then propagate a 502
            out = json.dumps({"error": str(exc)[:300]}).encode()
            status = 502
        wall_ms = (time.time() - t0) * 1000.0
        with _lock:
            _inflight -= 1

        ctok = ptok = 0
        server_ms = 0.0
        try:
            j = json.loads(out)
            u = j.get("usage") or {}
            ctok = int(u.get("completion_tokens") or 0)
            ptok = int(u.get("prompt_tokens") or 0)
            server_ms = float((j.get("vision") or {}).get("decode_ms") or 0.0)
        except Exception:  # noqa: BLE001
            pass

        if _sink is not None:
            with _lock:
                _sink.write(
                    json.dumps(
                        {
                            "t": round(t0, 3),
                            "path": self.path,
                            "status": status,
                            "wall_ms": round(wall_ms, 1),
                            "server_ms": server_ms,
                            "completion_tokens": ctok,
                            "prompt_tokens": ptok,
                            "img_w": w,
                            "img_h": h,
                            "area": area,
                            "n_images": n_images,
                            "req_bytes": len(body),
                            "concurrency": conc,
                        }
                    )
                    + "\n"
                )
                _sink.flush()

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802 — health/model probes come through here too
        try:
            with urllib.request.urlopen(_upstream + self.path, timeout=60) as resp:
                out, status = resp.read(), resp.status
        except Exception as exc:  # noqa: BLE001
            out, status = json.dumps({"error": str(exc)[:200]}).encode(), 502
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    global _sink, _upstream
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--upstream", default="http://127.0.0.1:8008")
    ap.add_argument("--out", default="probe_out/ocr_wire.jsonl")
    args = ap.parse_args()
    _upstream = args.upstream.rstrip("/")
    path = args.out
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _sink = open(path, "a")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    srv.daemon_threads = True
    print(f"proxy :{args.port} -> {_upstream}  logging {path}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
