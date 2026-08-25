#!/usr/bin/env python3
"""W6a: what does a first-page triage actually cost, on a QUIET machine?

CORRECTED after a first attempt got three things wrong:
  * it called the REST /v1/vision shim; the canonical route is the GraphQL
    `visionCompletion` mutation
  * it shelled out to tools/pdf_extract/.venv to render a page; pymupdf is
    in the ROOT venv, so there was never a subprocess to make
  * it ran against a mission at full tilt sharing GPU1, so every latency
    it reported was contention, not cost — and that number was then used
    to argue the design away

THREE ARMS, because "muse vision is too expensive" was an assumption:
  A  muse vision on the rendered page          (the original design)
  B  paddle OCR the page -> muse TEXT triage   (cheap path; paddle is a
     hot secondary reachable by `model: paddle-ocr-vl` on the same
     mutation, and a page of text is a small text turn, not a vision one)
  C  render only, no model                     (the floor: what rendering costs)

Read-only. Renders under ~/corpora so the mutation's `path` input resolves
(model.vision_image_roots) and no base64 round-trip is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
GQL = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008") + "/graphql"
#: Renders live here so `path` resolves under vision_image_roots.
SHOT_DIR = os.path.expanduser("~/corpora/_preocr_probe")

VISION_Q = """
mutation V($r: VisionCompletionRequest!) {
  visionCompletion(request: $r) { text visionModel }
}
"""
TEXT_Q = """
mutation C($r: CompletionRequest!) {
  createCompletion(request: $r) { text }
}
"""

TRIAGE_PROMPT = (
    "This is the first page of a scientific paper.\n"
    "Is it ORIGINAL RESEARCH reporting new measurements, or a "
    "REVIEW/overview/preface/editorial?\n"
    "Also name the main measurement technique, and whether the subject is "
    "minerals, rocks, pigments or geological materials.\n"
    "End your reply with exactly these three lines:\n"
    "TYPE: research|review\n"
    "TECHNIQUE: raman|ftir|libs|xrd|reflectance|xrf|other|none\n"
    "GEOLOGICAL: yes|no"
)


def gql(query: str, variables: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        GQL,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    if body.get("errors"):
        raise RuntimeError(str(body["errors"])[:300])
    return body["data"]


def render_page(pdf_path: str, out_png: str, dpi: int) -> tuple[bool, str, int, float]:
    """Page 1 -> PNG, in-process. pymupdf is a root-venv dependency."""
    import pymupdf

    t0 = time.time()
    try:
        doc = pymupdf.open(pdf_path)
        n = doc.page_count
        if n == 0:
            return False, "no pages", 0, time.time() - t0
        doc[0].get_pixmap(dpi=dpi).save(out_png)
        return True, "", n, time.time() - t0
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:120], 0, time.time() - t0


def tail_verdict(text: str) -> dict:
    """Read the ANSWER off the tail. This family reasons out loud first, so
    a JSON-only instruction returns prose and a strict parser gets nothing —
    which is how the first run scored 5/5 on an empty dict."""
    out = {}
    for line in (text or "").splitlines():
        s = line.strip().upper()
        for key in ("TYPE", "TECHNIQUE", "GEOLOGICAL"):
            if s.startswith(key + ":"):
                out[key.lower()] = s.split(":", 1)[1].strip().lower()
    return out


def arm_vision(png: str, model: str | None = None) -> tuple[str, float]:
    r = {
        "prompt": TRIAGE_PROMPT,
        "images": [{"path": png}],
        "maxTokens": 700,
        "temperature": 0.1,
    }
    if model:
        r["model"] = model
    t0 = time.time()
    d = gql(VISION_Q, {"r": r})
    return d["visionCompletion"]["text"], time.time() - t0


def arm_paddle_then_text(png: str) -> tuple[str, float, float, int]:
    """Paddle OCRs the page; muse reads the TEXT. The bet is that a page of
    text through the text path is far cheaper than the same page as pixels."""
    t0 = time.time()
    d = gql(
        VISION_Q,
        {
            "r": {
                "prompt": "Transcribe all text on this page in reading order.",
                "images": [{"path": png}],
                "maxTokens": 1600,
                "temperature": 0.0,
                "model": "paddle-ocr-vl",
            }
        },
    )
    page_text = d["visionCompletion"]["text"] or ""
    t_ocr = time.time() - t0

    t1 = time.time()
    d2 = gql(
        TEXT_Q,
        {
            "r": {
                "prompt": TRIAGE_PROMPT + "\n\nPAGE TEXT:\n" + page_text[:6000],
                "maxTokens": 700,
                "temperature": 0.1,
            }
        },
    )
    return d2["createCompletion"]["text"], t_ocr, time.time() - t1, len(page_text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument("--arms", default="C,A,B")
    args = ap.parse_args()

    os.makedirs(SHOT_DIR, exist_ok=True)
    triage = json.load(open(os.path.join(ROOT, "ocr_queue_triage.json")))
    picks = [r for r in triage if r["score"] < 0][: args.n] + [
        r for r in triage if r["score"] >= 2
    ][: args.n]

    import asyncio
    from agent.effects.local import LocalEffects
    from agent.actions.scholarly_actions import read_databank

    bank = asyncio.run(read_databank(LocalEffects(ROOT)))

    def pdf_of(key):
        p = (bank.get(key) or {}).get("pdf_path")
        if not p:
            return None
        f = p if os.path.isabs(p) else os.path.join(ROOT, p)
        return f if os.path.exists(f) else None

    arms = [a.strip().upper() for a in args.arms.split(",")]
    stats: dict[str, list] = {"C": [], "A": [], "B_ocr": [], "B_text": []}
    verdicts: list[tuple[str, dict, dict]] = []

    print(f"quiet-machine probe, dpi={args.dpi}, {len(picks)} papers\n")
    for r in picks:
        f = pdf_of(r["key"])
        if not f:
            continue
        png = os.path.join(SHOT_DIR, f"{r['key'][:40].replace('/', '_')}.png")
        ok, note, npages, t_render = render_page(f, png, args.dpi)
        stats["C"].append(t_render)
        if not ok:
            print(f"  RENDER FAIL  {note[:50]}  {r['title'][:40]}")
            continue
        label = "non_primary" if r["score"] < 0 else "primary"
        line = f"  [{label:11s}] render {t_render:4.1f}s"
        va = vb = {}
        if "A" in arms:
            try:
                txt, dt = arm_vision(png)
                stats["A"].append(dt)
                va = tail_verdict(txt)
                line += f" | muse-vision {dt:5.1f}s type={va.get('type','?'):8s}"
            except Exception as e:  # noqa: BLE001
                line += f" | muse-vision ERR {str(e)[:40]}"
        if "B" in arms:
            try:
                txt, t_ocr, t_txt, nchars = arm_paddle_then_text(png)
                stats["B_ocr"].append(t_ocr)
                stats["B_text"].append(t_txt)
                vb = tail_verdict(txt)
                line += (
                    f" | paddle {t_ocr:4.1f}s +text {t_txt:4.1f}s "
                    f"({nchars}c) type={vb.get('type','?'):8s}"
                )
            except Exception as e:  # noqa: BLE001
                line += f" | paddle+text ERR {str(e)[:40]}"
        print(line + f"  {r['title'][:34]}")
        verdicts.append((label, va, vb))

    print("\n" + "=" * 72)
    for k, lab in (
        ("C", "render only"),
        ("A", "muse vision"),
        ("B_ocr", "paddle OCR"),
        ("B_text", "muse text"),
    ):
        v = stats[k]
        if v:
            print(
                f"  {lab:14s} n={len(v):2d}  median {statistics.median(v):6.1f}s  "
                f"min {min(v):5.1f}  max {max(v):5.1f}"
            )
    if stats["B_ocr"] and stats["B_text"]:
        b = statistics.median(stats["B_ocr"]) + statistics.median(stats["B_text"])
        print(f"  {'ARM B total':14s}      median {b:6.1f}s")
        if stats["A"]:
            a = statistics.median(stats["A"])
            print(
                f"  => arm B is {a / b:.1f}x {'CHEAPER' if b < a else 'dearer'} than arm A"
            )

    for arm_key, name in ((1, "muse vision"), (2, "paddle+text")):
        rows = [
            (l, v[arm_key - 1] if arm_key == 1 else v[1])
            for l, *v in [(l, a, b) for l, a, b in verdicts]
        ]
        got = [(l, d) for l, d in rows if d.get("type")]
        if not got:
            continue
        tp = sum(
            1 for l, d in got if l == "non_primary" and d["type"].startswith("rev")
        )
        npn = sum(1 for l, _ in got if l == "non_primary")
        fp = sum(1 for l, d in got if l == "primary" and d["type"].startswith("rev"))
        pn = sum(1 for l, _ in got if l == "primary")
        print(
            f"\n  {name}: parsed {len(got)}/{len(rows)}  "
            f"non-primary caught {tp}/{npn}  primary WRONGLY flagged {fp}/{pn}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
