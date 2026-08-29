#!/usr/bin/env python3
"""SHAPE A/B: one full-page request instead of ~30 region crops — what does
it cost in recall, and what does it buy in GPU-seconds?

WHY. The encode-split probe found the vision tower pads every crop to a
minimum patch grid: a 32-px text line costs 144-189 image tokens and
57-73 ms of projector forward — the same order as a much larger region.
paddlex's layout stage sends ~20-30 such crops per page, so a page pays
the minimum-grid constant ~25x, ~2.2 s of encode — while ONE full-page
encode is ~1.0 s for 1,240 tokens. Page-shape halves encode and kills
~29 per-request overheads. The open question is recall: PaddleOCR-VL is
trained for element-level recognition inside the PP-Structure pipeline;
full-page one-shot is off-label.

METHOD. The same pages the 2026-08-29 sweep cells extracted (6 papers x
4 pages, region-pipeline recall booked at numeric 0.674 / span 0.819 on
pool serving), sent as ONE request each ("Transcribe all text...") to
whatever paddle server is up on 8008, scored with the SAME
_verify_page numeric/span checker against pymupdf prose truth.

PRE-REGISTERED PREDICTION (2026-08-29): page-shape recall DEGRADES
materially (one-shot page OCR is off-label; expect numeric well below
the region pipeline's 0.674 on the same pages), so the shape lever is
NOT free — if it holds within a few points instead, it beats every
serving-side lever measured today and the pipeline question moves to
the operator.

RUN (paddle server up on 8008; run from tools/pdf_extract/.venv for the
verifier):
  tools/pdf_extract/.venv/bin/python llmvp/probe_ocr_page_shape.py
"""

from __future__ import annotations

import base64
import json
import os
import statistics
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = (
    "/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad"
)
WD = "/home/lah-rb/corpora/ouroboros-spectra"
OUT = os.path.join(ROOT, "llmvp", "probe_out", "ocr_page_shape.jsonl")
URL = "http://127.0.0.1:8008/v1/vision"
PAGES_PER_PAPER = 4
DPI = 160
MAX_TOKENS = 2048

PROMPT = (
    "Transcribe ALL text on this page as plain markdown, in reading order. "
    "Include headers, body text, captions, footnotes and table contents. "
    "Do not describe the page; output only the transcription."
)


def call(png_bytes: bytes) -> tuple[float, str, int]:
    b = base64.b64encode(png_bytes).decode()
    body = json.dumps({
        "max_tokens": MAX_TOKENS, "temperature": 0.8,
        "messages": [{"role": "user", "content": [
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b}"}},
            {"type": "text", "text": PROMPT}]}],
    }).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        out = json.loads(resp.read())
    wall = time.time() - t0
    u = out.get("usage") or {}
    return wall, (out["choices"][0]["message"].get("content") or ""), int(
        u.get("completion_tokens") or 0)


def main() -> int:
    sys.path.insert(0, os.path.join(ROOT, "tools", "pdf_extract"))
    from extract_batch import _prose_text, _verify_page

    import pymupdf

    man = json.load(open(os.path.join(SCRATCH, "ocr_probe", "manifest.json")))
    papers = man["papers"][:6]
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sink = open(OUT, "w")

    nh = nt = sh = st = 0
    walls, out_toks = [], []
    verified = unverified = 0
    print(f"{'paper':40s} {'pg':>3s} {'wall':>6s} {'tok':>5s} "
          f"{'num':>6s} {'span':>6s}")
    print("-" * 72)
    for p in papers:
        doc = pymupdf.open(os.path.join(WD, p["pdf"]))
        for i in range(min(PAGES_PER_PAPER, len(doc))):
            page = doc[i]
            png = page.get_pixmap(dpi=DPI).tobytes("png")
            truth = _prose_text(page)
            try:
                wall, md, ct = call(png)
            except Exception as exc:  # noqa: BLE001 — a page may fail
                sink.write(json.dumps({
                    "paper": p["key"], "page": i, "error": str(exc)[:200],
                }) + "\n")
                print(f"{p['key'][:40]:40s} {i:>3d}  ERROR {str(exc)[:40]}")
                continue
            walls.append(wall)
            out_toks.append(ct)
            row = {"paper": p["key"], "page": i, "wall_s": round(wall, 2),
                   "completion_tokens": ct, "md_chars": len(md)}
            if len(truth.strip()) >= 200 and md.strip():
                h, t, s, stt = _verify_page(md, truth)
                nh += h
                nt += t
                sh += s
                st += stt
                verified += 1
                row.update(num=f"{h}/{t}", span=f"{s}/{stt}")
                print(f"{p['key'][:40]:40s} {i:>3d} {wall:>5.1f}s {ct:>5d} "
                      f"{(h / t if t else 0):>6.3f} {(s / stt if stt else 0):>6.3f}")
            else:
                unverified += 1
                print(f"{p['key'][:40]:40s} {i:>3d} {wall:>5.1f}s {ct:>5d} "
                      f"{'(unverifiable page)':>15s}")
            sink.write(json.dumps(row) + "\n")
        doc.close()
    sink.close()

    print("\n" + "=" * 72)
    print("PAGE-SHAPE AGGREGATE")
    print("=" * 72)
    num = nh / nt if nt else 0.0
    span = sh / st if st else 0.0
    print(f"  verified pages       {verified}  (unverifiable {unverified})")
    print(f"  numeric recall       {num:.4f}   (region pipeline, same "
          f"workload, pool: 0.6744)")
    print(f"  span recall          {span:.4f}   (region pipeline: 0.8194)")
    if walls:
        print(f"  wall/page median     {statistics.median(walls):.1f} s  "
              f"(region pipeline: ~5.3-5.8 s/page serving time)")
        print(f"  output tokens median {statistics.median(out_toks):.0f}")
    if nt == 0:
        # Every rate gate must FAIL on zero checkable items.
        print("\n  !! ZERO checkable numerics — the comparison is VOID, "
              "not passed.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
