#!/usr/bin/env python3
"""Out-of-band OCR + pack for the BINDER stack, using Sonnet for the vision.

WHY THIS EXISTS. The binder papers (dev/ingest_reading_list.py, `binder:
true`) need to be in tomorrow's training export, but the local pipeline
cannot take them: the OCR lane is disabled because the curate seat took
its VRAM (OUROBOROS_DISABLE_LANES=ocr), and re-enabling it would push
1,535 pages of paddle work in front of the curate backlog. So the vision
step runs on Sonnet instead, off this machine's GPU.

WHAT OCR MEANS HERE, precisely. It is NOT text recovery -- most of these
PDFs already carry a text layer. It is the LAYOUT TRANSITION: pdf ->
markdown with tables rebuilt as tables and figures read in place. That is
why the pipeline paddle-OCRs everything including text-layer PDFs, and
why the embedded text layer is kept as GROUND TRUTH for checking the
transition was clean rather than used as the output.

FIDELITY IS MEASURED WITH THE PIPELINE'S OWN VERIFIER, not a lookalike.
`_verify_page` and `_repeat_words` are imported from
tools/pdf_extract/extract_batch.py (stdlib + pymupdf + PIL at module
level, so the root venv can import it), and the gates are the constants
from agent/actions/extraction_actions.py. A binder paper's
extraction_quality therefore means exactly what every other paper's
means: numerics and word-spans present in the PUBLISHER's text layer are
present in our markdown (truth subset of md -- the VLM legitimately adds
figure-internal text, so the inverse direction is not checked).

Stages:
  render   pages -> PNG + per-page truth text + a job manifest
  verify   assemble transcriptions, score with the real verifier, gate,
           and book extraction fields ONLY for docs that pass
  (packing is a separate stage; a doc that fails verify never reaches it)

Usage:
  .venv/bin/python dev/binder_ocr_pack.py render --keys <k> [--max-pages N]
  .venv/bin/python dev/binder_ocr_pack.py verify --keys <k> [--apply]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
WORK = os.path.expanduser("~/tmp/binder_ocr")

sys.path.insert(0, REPO)

# The extractor is a foreign-venv tool; import it by path, the convention
# its own tests use. Its module-level imports are stdlib + pymupdf + PIL,
# all present in the root venv.
_spec = importlib.util.spec_from_file_location(
    "extract_batch", os.path.join(REPO, "tools", "pdf_extract", "extract_batch.py")
)
_eb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_eb)

from agent.actions.extraction_actions import (  # noqa: E402
    MAX_REPEAT_WORDS,
    MIN_NUMERIC_RATE,
    MIN_SPAN_RATE,
    UNVERIFIED_MIN_MD_BYTES,
)

#: Claude downscales any image whose long side exceeds ~1568 px, so
#: rendering above that is thrown away. Dense pages therefore get their
#: resolution from SPLITTING, not from dpi -- the same finding as the
#: figure-digitizer sliver work: thin glyphs are a resolution limit, and
#: 2x-by-split beats a bigger single image.
LONG_SIDE_PX = 1540
#: Chars of text layer above which a page is split into overlapping
#: halves. Below it, one image resolves the page fine.
DENSE_CHARS = 2600
#: Fraction of page height each half extends past the midpoint, so a line
#: sitting exactly on the seam appears whole in one of the two images.
SPLIT_OVERLAP = 0.04
#: A page with less text layer than this cannot be scored (the pipeline
#: uses the same floor before calling _verify_page).
MIN_TRUTH_CHARS = 200


def binder_records() -> dict[str, dict]:
    last: dict[str, dict] = {}
    with open(os.path.join(CORPUS, "databank/papers.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = rec.get("paper_key")
            if key:
                last[key] = rec
    return {k: v for k, v in last.items() if v.get("binder") and v.get("pdf_path")}


def render(keys: list[str], max_pages: int | None) -> None:
    import pymupdf

    for key in keys:
        rec = binder_records().get(key)
        if not rec:
            print(f"  {key}: not a binder record")
            continue
        pdf = os.path.join(CORPUS, rec["pdf_path"])
        out = os.path.join(WORK, key)
        os.makedirs(os.path.join(out, "pages"), exist_ok=True)
        doc = pymupdf.open(pdf)
        n = len(doc) if max_pages is None else min(len(doc), max_pages)
        pages = []
        for i in range(n):
            page = doc[i]
            truth = page.get_text()
            rect = page.rect
            zoom = LONG_SIDE_PX / max(rect.width, rect.height)
            imgs = []
            if len(truth.strip()) >= DENSE_CHARS:
                # Split: each half rendered at the full budget => ~2x the
                # pixels per glyph of a whole-page render.
                mid = rect.height / 2
                over = rect.height * SPLIT_OVERLAP
                halves = [
                    pymupdf.Rect(rect.x0, rect.y0, rect.x1, mid + over),
                    pymupdf.Rect(rect.x0, mid - over, rect.x1, rect.y1),
                ]
                for h, clip in enumerate(halves):
                    z = LONG_SIDE_PX / max(clip.width, clip.height)
                    name = f"p{i:04d}_{h}.png"
                    page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=clip).save(
                        os.path.join(out, "pages", name)
                    )
                    imgs.append(name)
            else:
                name = f"p{i:04d}.png"
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(
                    os.path.join(out, "pages", name)
                )
                imgs.append(name)
            pages.append({"page": i, "images": imgs, "truth": truth})
        doc.close()
        with open(os.path.join(out, "truth.json"), "w", encoding="utf-8") as fh:
            json.dump({"paper_key": key, "pages": pages}, fh)
        split = sum(1 for p in pages if len(p["images"]) > 1)
        print(
            f"  {key[:52]:54s} {n:4d} pages -> "
            f"{sum(len(p['images']) for p in pages):4d} images ({split} split)"
        )
        print(f"      work dir: {out}")


def verify(keys: list[str], apply: bool) -> int:
    """Score transcriptions with the pipeline's verifier and apply its gates."""
    from agent.actions.scholarly_actions import append_extraction_records  # noqa: F401

    results = []
    for key in keys:
        out = os.path.join(WORK, key)
        truth_path = os.path.join(out, "truth.json")
        if not os.path.exists(truth_path):
            print(f"  {key}: not rendered")
            continue
        meta = json.load(open(truth_path, encoding="utf-8"))
        page_mds, missing = [], 0
        num_hit = num_total = span_hit = span_total = 0
        verified = unverified = 0
        for p in meta["pages"]:
            md_path = os.path.join(out, "md", f"p{p['page']:04d}.md")
            if not os.path.exists(md_path):
                missing += 1
                page_mds.append("")
                continue
            page_md = open(md_path, encoding="utf-8").read()
            page_mds.append(page_md)
            if len(p["truth"].strip()) >= MIN_TRUTH_CHARS and page_md.strip():
                nh, nt, sh, st = _eb._verify_page(page_md, p["truth"])
                num_hit += nh
                num_total += nt
                span_hit += sh
                span_total += st
                verified += 1
            else:
                unverified += 1
        assembled = "\n\n---\n\n".join(page_mds)
        numeric = num_hit / num_total if num_total else 0.0
        span = span_hit / span_total if span_total else 0.0
        repeat = _eb._repeat_words(assembled)
        unverified_clean = (
            verified <= 0
            and len(meta["pages"]) > 0
            and repeat <= MAX_REPEAT_WORDS
            and len(assembled) >= UNVERIFIED_MIN_MD_BYTES
        )
        ok = unverified_clean or (
            verified > 0
            and numeric >= MIN_NUMERIC_RATE
            and span >= MIN_SPAN_RATE
            and repeat <= MAX_REPEAT_WORDS
        )
        verdict = "PASS" if ok else "FAIL"
        if missing:
            verdict = "INCOMPLETE"
            ok = False
        print(
            f"  {verdict:10s} {key[:46]:48s} numeric {numeric:.3f} "
            f"span {span:.3f} repeat {repeat:4d} "
            f"verified {verified}/{len(meta['pages'])} missing {missing}"
        )
        results.append(
            (key, ok, numeric, span, repeat, verified, unverified, assembled)
        )

    if not apply:
        print("\nDRY RUN — pass --apply to write markdown and book extraction.")
        return 0
    return _book(results)


def _book(results) -> int:
    import asyncio

    from agent.actions.scholarly_actions import append_extraction_records
    from agent.effects.local import LocalEffects

    rows = []
    for key, ok, numeric, span, repeat, verified, unverified, assembled in results:
        if not ok:
            print(f"  skip (not passing): {key}")
            continue
        md_rel = os.path.join("databank", "markdown", f"{key}.md")
        with open(os.path.join(CORPUS, md_rel), "w", encoding="utf-8") as fh:
            fh.write(assembled)
        rows.append(
            {
                "paper_key": key,
                "extraction_status": "extracted",
                "failure_reason": "",
                "md_path": md_rel,
                # Figures are read IN PLACE by the transcriber and marked
                # with the curator's own VLM-reading marker, so there is no
                # separate figtext artifact and _figtext_ready passes on
                # figure_count == 0. The provenance is in the doc, not lost.
                "figure_count": 0,
                "extraction_method": "sonnet-vision+text-layer-verified",
                "extraction_quality": {
                    "numeric_match_rate": round(numeric, 4),
                    "span_pass_rate": round(span, 4),
                    "max_repeat_words": repeat,
                    "verified_pages": verified,
                    "unverified_pages": unverified,
                    "pages": verified + unverified,
                },
            }
        )
    if not rows:
        print("nothing to book")
        return 1

    async def go():
        effects = LocalEffects(working_dir=CORPUS)
        await append_extraction_records(effects, rows)

    asyncio.run(go())
    print(f"booked {len(rows)} extraction record(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=("render", "verify", "list"))
    ap.add_argument("--keys", default="")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.stage == "list":
        for k, r in sorted(binder_records().items()):
            print(f"  {k[:60]:62s} {str(r.get('title'))[:50]}")
        return 0

    keys = [k for k in args.keys.split(",") if k.strip()]
    if not keys:
        print("--keys is required")
        return 2
    if args.stage == "render":
        render(keys, args.max_pages)
        return 0
    return verify(keys, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
