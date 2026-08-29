#!/usr/bin/env python3
"""What IS the ~167 ms floor under every OCR request?

CONTEXT. The wire-trace fit (probe_ocr_analyze.py over 170 queue-free rows)
decomposed a production OCR request as:

    fixed 166.8 ms (80.1%) | encode 14.7 ms (7.1%) | decode 26.6 ms (12.8%)

and the observed p10 wall (168 ms) sits on the fitted intercept, so the floor
is real rather than an artefact of the fit. Decode's 12.8% already kills the
batched-engine plan. But the sweep then found that WIDENING the vision pool
(2 -> 4 contexts, iso-memory) made throughput WORSE, which no queueing story
explains. Both results point at the same place: a large per-request cost that
does not scale with the image, does not scale with the output, and does not
parallelize across pool contexts.

This probe asks what that cost is made of, because the answer decides where
the lever is:

  * a constant cost inside the VISION TOWER (CLIP runs a minimum patch grid
    however small the crop) -> irreducible per request; the lever is FEWER,
    BIGGER requests, and nothing in our serving code will help.
  * a FRAMEWORK cost we own (HTTP + intake + instance checkout + the
    per-request context clear in acquire_vision_instance) -> the lever is in
    our code, and it is worth roughly 5 s per 30-crop page.

ARMS (all at max_tokens=8, so decode is a fixed small term):
  text     — no image at all, through the same server. Isolates HTTP +
             routing + generation setup. THE MODEL-FREE-ISH CONTROL.
  blank32  — a 32x32 white PNG: the smallest possible image. Everything
             above `text` is vision-path cost that does not depend on
             content or size.
  crop     — a real paddlex-sized crop (~20-30k px).
  page     — a full page render (~2.4 Mpx), 100x the crop's area.

READING IT. blank32 - text  = the constant vision-path cost (tower + intake
+ checkout + clear). page - crop = the area-proportional part. If blank32 is
already near the 167 ms floor, the floor is constant vision cost and pool
width was never going to help.

RUN (paddle server up alone on 8008, nothing else in flight):
  llmvp/.venv/bin/python llmvp/probe_ocr_floor.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import statistics
import time
import urllib.request

URL = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008")
SCRATCH = (
    "/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad/ocr_probe"
)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "probe_out", "ocr_floor.jsonl")
PROMPT = "Transcribe all text in this image as plain markdown."
REPEATS = 12
MAX_TOKENS = 8


def _post(path: str, payload: dict, timeout: float = 300.0) -> tuple[float, dict]:
    req = urllib.request.Request(
        f"{URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read())
    return (time.time() - t0) * 1000.0, out


def vision(img_b64: str | None) -> tuple[float, int]:
    if img_b64 is None:
        # Same server, same generation machinery, no image and no vision
        # instance checkout.
        ms, out = _post("/v1/chat/completions", {
            "max_tokens": MAX_TOKENS, "temperature": 0.8,
            "messages": [{"role": "user", "content": PROMPT}],
        })
    else:
        ms, out = _post("/v1/vision", {
            "max_tokens": MAX_TOKENS, "temperature": 0.8,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                {"type": "text", "text": PROMPT}]}],
        })
    u = out.get("usage") or {}
    return ms, int(u.get("completion_tokens") or 0)


def main() -> int:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 255, 255)).save(buf, format="PNG")
    blank = base64.b64encode(buf.getvalue()).decode()

    man = json.load(open(os.path.join(SCRATCH, "manifest.json")))
    crop_p = man["crops"][0]["path"]
    page_p = man["pages"][0]["path"]
    # Re-crop to a REAL paddlex line-region size; the manifest's "crop" is a
    # page band (~400k px), which the wire trace showed is 15-60x bigger than
    # what paddlex actually sends.
    im = Image.open(page_p)
    line = im.crop((int(im.width * 0.1), int(im.height * 0.35),
                    int(im.width * 0.1) + 880, int(im.height * 0.35) + 32))
    lb = io.BytesIO()
    line.save(lb, format="PNG")
    arms = [
        ("text", None, 0),
        ("blank32", blank, 32 * 32),
        ("crop", base64.b64encode(lb.getvalue()).decode(), 880 * 32),
        ("page", base64.b64encode(open(page_p, "rb").read()).decode(),
         im.width * im.height),
    ]

    sink = open(OUT, "w")
    res: dict[str, list[float]] = {}
    for name, img, area in arms:
        # One warm-up per arm, discarded: the first vision request also pays
        # for the lazy pool build, and the first text request for its own
        # first-token path.
        try:
            vision(img)
        except Exception as exc:  # noqa: BLE001
            print(f"{name}: warm-up failed — {exc}")
            continue
        vals = []
        for _ in range(REPEATS):
            try:
                ms, tok = vision(img)
            except Exception as exc:  # noqa: BLE001
                sink.write(json.dumps({"arm": name, "error": str(exc)[:200]}) + "\n")
                continue
            vals.append(ms)
            sink.write(json.dumps(
                {"arm": name, "area": area, "ms": round(ms, 1), "tok": tok}) + "\n")
        sink.flush()
        res[name] = vals
    sink.close()

    print("=" * 62)
    print(f"OCR REQUEST FLOOR   (max_tokens={MAX_TOKENS}, n={REPEATS} each)")
    print("=" * 62)
    print(f"  {'arm':10s} {'area px':>10s} {'median ms':>10s} {'p10':>8s} {'p90':>8s}")
    med: dict[str, float] = {}
    for name, _, area in arms:
        v = sorted(res.get(name) or [])
        if not v:
            print(f"  {name:10s} {area:>10,}          (no data)")
            continue
        med[name] = statistics.median(v)
        print(f"  {name:10s} {area:>10,} {med[name]:>10.1f} "
              f"{v[len(v)//10]:>8.1f} {v[9*len(v)//10]:>8.1f}")

    print("\n" + "-" * 62)
    if "text" in med and "blank32" in med:
        const_vision = med["blank32"] - med["text"]
        print(f"  constant VISION cost (blank32 - text)  {const_vision:8.1f} ms")
        print(f"  HTTP + generation setup (text)         {med['text']:8.1f} ms")
    if "crop" in med and "page" in med:
        dv = med["page"] - med["crop"]
        da = arms[3][2] - arms[2][2]
        print(f"  area-proportional (page - crop)        {dv:8.1f} ms "
              f"over {da/1e6:.2f} Mpx -> {dv/(da/1e6):.0f} ms/Mpx")
    if "crop" in med:
        print(f"\n  A REAL CROP COSTS {med['crop']:.0f} ms, of which")
        if "text" in med:
            print(f"    {med['text']:.0f} ms is HTTP + generation setup")
        if "blank32" in med and "text" in med:
            print(f"    {med['blank32'] - med['text']:.0f} ms is constant vision cost")
        if "blank32" in med:
            print(f"    {med['crop'] - med['blank32']:.0f} ms is this crop's own size")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
