#!/usr/bin/env python3
"""Where does an OCR request's wall time actually GO — decode, or encode?

QUESTION. The figtext campaign won 3.3x by moving vision decode into the
batched multi-sequence engine, and the 2026-08-29 head-to-head confirmed
batching beats a multi-context pool on DECODE throughput even at 0.5B
(2.55x at N=16; the pool plateaus at ~667 tok/s from N=4). Before paying
for the same migration on OCR, the question that decides it: what
FRACTION of a real paddle OCR request is decode?

WHY IT DECIDES. Batching multiplexes DECODE — N streams share one weight
read per step. It does not multiplex ENCODE: the batched path serializes
every image through MtmdEncoder's process-wide lock, whereas the POOL
path encodes independently inside each instance's own chat handler. So
the two architectures trade places depending on the mix:

    decode-dominated  -> batched wins (figtext: 1 big figure, ~800 tok out)
    encode-dominated  -> the POOL wins, and batching is actively WRONG

figtext was the first case. OCR looks like the second — many small region
crops with short outputs — but "looks like" is not a measurement, and the
whole Phase 4 build rests on the answer.

PRE-REGISTERED PREDICTION (2026-08-29, before any cell ran): OCR is
encode-dominated. Decode share below 40%, and likely below 30%, because a
crop's answer is a line or two of transcription while the image still
pays a full CLIP+projector pass. If this holds, the M-RoPE cell-debt work
should NOT be built, and OCR's lever is pool width, not batching.

METHOD. Server-side wall (`vision.decode_ms`, which brackets the whole
create_chat_completion) regressed against tokens actually produced:

    wall = a + b * completion_tokens
    a = encode + prefill + fixed overhead     b = 1 / decode_tps

At concurrency 1 there is no queue wait, so `a` is real per-request cost.
Sweeping max_tokens moves ONLY the decode term, which is what makes the
split identifiable without instrumenting the handler.

ARMS
  natural — real crops and pages, uncapped: the production output-length
            distribution. Without it the split is reported at an output
            length OCR never actually generates.
  sweep   — same images at max_tokens in {1, 8, 32, 128, 384}: the fit.
  control — a 32x32 blank PNG through the identical path. Isolates fixed
            per-request overhead (HTTP, intake, checkout) from
            image-dependent encode, so `a` is not silently credited with
            framework cost. THE MODEL-FREE ARM.

RUN (paddle server up on port 8008, mission stopped, quiet machine):
  llmvp/.venv/bin/python llmvp/probe_ocr_stage_split.py
Output: llmvp/probe_out/ocr_stage_split.jsonl + a summary table.
"""

from __future__ import annotations

import base64
import io
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008")
# EMPTY MEANS OMIT THE FIELD. LLMVP's routing is strict about `model`: a name
# is resolvable only when it names a HOT SECONDARY, and the PRIMARY's own name
# is refused ("is a local config but is not hot"). Production reaches paddle as
# a secondary on muse's server, so it sends the name; a probe standing paddle
# up alone must omit it and take the primary.
MODEL = os.environ.get("OUROBOROS_LLMVP_VL_MODEL", "")
SCRATCH = (
    "/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad/ocr_probe"
)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_out")
OUTFILE = os.path.join(OUT, "ocr_stage_split.jsonl")

# The transcription prompt paddlex sends. Kept short on purpose: a long
# instruction would inflate prefill and flatter the decode share.
PROMPT = "Transcribe all text in this image as plain markdown."
SWEEP = [1, 8, 32, 128, 384]
NATURAL_CAP = 512
REPEATS = 3


def _b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _blank_png() -> str:
    """32x32 white PNG, written by hand so the control needs no PIL."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 255, 255)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def call(img_b64: str, max_tokens: int, timeout: float = 300.0) -> dict:
    payload = {
        "max_tokens": max_tokens,
        "temperature": 0.8,
    }
    if MODEL:
        payload["model"] = MODEL
    body = json.dumps(
        {
            **payload,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                        },
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        }
    ).encode()
    req = urllib.request.Request(
        f"{URL}/v1/vision", data=body, headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    client_ms = (time.time() - t0) * 1000.0
    usage = payload.get("usage") or {}
    vis = payload.get("vision") or {}
    return {
        "client_ms": round(client_ms, 1),
        "server_ms": float(vis.get("decode_ms") or 0.0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "handler": vis.get("handler") or "",
        "text_len": len((payload.get("choices") or [{}])[0]
                        .get("message", {})
                        .get("content") or ""),
    }


def fit(points: list[tuple[int, float]]) -> tuple[float, float, int]:
    """Least squares wall = a + b*tokens. Returns (a_ms, b_ms_per_token, n)."""
    pts = [(x, y) for x, y in points if y > 0]
    n = len(pts)
    if n < 2:
        return (0.0, 0.0, n)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    den = sum((x - mx) ** 2 for x, _ in pts)
    if den <= 0:
        return (my, 0.0, n)
    b = sum((x - mx) * (y - my) for x, y in pts) / den
    return (my - b * mx, b, n)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    man_path = os.path.join(SCRATCH, "manifest.json")
    if not os.path.isfile(man_path):
        print(f"missing {man_path} — render the probe images first", file=sys.stderr)
        return 2
    man = json.load(open(man_path))

    try:
        with urllib.request.urlopen(f"{URL}/health", timeout=10) as r:
            r.read()
    except Exception:
        pass  # /health shape varies; the first real call is the true probe

    rows: list[dict] = []
    sink = open(OUTFILE, "w")

    def emit(row: dict) -> None:
        rows.append(row)
        sink.write(json.dumps(row) + "\n")
        sink.flush()

    classes: list[tuple[str, list[str]]] = [
        ("crop", [c["path"] for c in man["crops"]]),
        ("page", [p["path"] for p in man["pages"]]),
    ]
    cache = {p: _b64(p) for _, paths in classes for p in paths}

    # ── control: fixed per-request overhead, image-independent ──────
    blank = _blank_png()
    print("control (32x32 blank) ...", flush=True)
    for mt in SWEEP:
        for _ in range(REPEATS):
            try:
                r = call(blank, mt)
            except Exception as exc:  # noqa: BLE001 — a cell may fail
                emit({"arm": "control", "max_tokens": mt, "error": str(exc)[:200]})
                continue
            emit({"arm": "control", "max_tokens": mt, **r})

    # ── natural: what OCR actually generates ────────────────────────
    for cls, paths in classes:
        print(f"natural ({cls}) ...", flush=True)
        for p in paths:
            try:
                r = call(cache[p], NATURAL_CAP)
            except Exception as exc:  # noqa: BLE001
                emit({"arm": "natural", "cls": cls, "img": os.path.basename(p),
                      "error": str(exc)[:200]})
                continue
            emit({"arm": "natural", "cls": cls, "img": os.path.basename(p), **r})

    # ── sweep: the fit ──────────────────────────────────────────────
    for cls, paths in classes:
        print(f"sweep ({cls}) ...", flush=True)
        for mt in SWEEP:
            for p in paths[:6]:
                try:
                    r = call(cache[p], mt)
                except Exception as exc:  # noqa: BLE001
                    emit({"arm": "sweep", "cls": cls, "max_tokens": mt,
                          "img": os.path.basename(p), "error": str(exc)[:200]})
                    continue
                emit({"arm": "sweep", "cls": cls, "max_tokens": mt,
                      "img": os.path.basename(p), **r})
    sink.close()

    # ── report ──────────────────────────────────────────────────────
    def ok(arm: str, cls: str | None = None) -> list[dict]:
        return [
            r for r in rows
            if r.get("arm") == arm and "error" not in r
            and (cls is None or r.get("cls") == cls)
        ]

    print("\n" + "=" * 68)
    print("OCR STAGE SPLIT")
    print("=" * 68)

    ctrl = ok("control")
    c_a, c_b, c_n = fit([(r["completion_tokens"], r["server_ms"]) for r in ctrl])
    print(f"\ncontrol (blank 32x32, n={c_n}): fixed {c_a:7.1f} ms   "
          f"decode {c_b:6.2f} ms/tok")

    verdict_rows = []
    for cls, _ in classes:
        nat = ok("natural", cls)
        swp = ok("sweep", cls)
        if not nat or not swp:
            continue
        toks = sorted(r["completion_tokens"] for r in nat)
        med = statistics.median(toks)
        a, b, n = fit([(r["completion_tokens"], r["server_ms"]) for r in swp])
        dec = b * med
        share = dec / (a + dec) if (a + dec) > 0 else 0.0
        img_encode = a - c_a
        print(f"\n{cls}:")
        print(f"  natural output      median {med:6.0f} tok   "
              f"range {toks[0]}-{toks[-1]}   n={len(nat)}")
        print(f"  fit (n={n:3d})         a = {a:8.1f} ms      b = {b:6.2f} ms/tok"
              f"   ({1000.0 / b if b > 0 else 0:.1f} tok/s)")
        print(f"  image encode+prefill  {img_encode:8.1f} ms  (a minus control fixed)")
        print(f"  decode at median      {dec:8.1f} ms")
        print(f"  >> DECODE SHARE       {share * 100:8.1f} %")
        verdict_rows.append((cls, share, med, a, b))

    print("\n" + "-" * 68)
    print("PRE-REGISTERED VERDICT RULES")
    print("-" * 68)
    for cls, share, med, a, b in verdict_rows:
        pct = share * 100
        if pct < 30:
            v = "DO NOT BUILD Phase 4 — pool is right, batching would serialize encode"
        elif pct <= 50:
            v = "MARGINAL — build only if pool also plateaus below reachable width"
        else:
            v = "BUILD Phase 4 (subject to P0c byte-identical neighbour KV)"
        print(f"  {cls:5s} {pct:5.1f}%  ->  {v}")
    print(f"\nrows: {len(rows)}  errors: {sum(1 for r in rows if 'error' in r)}")
    print(f"wrote {OUTFILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
