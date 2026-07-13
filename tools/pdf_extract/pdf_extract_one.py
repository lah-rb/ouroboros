#!/usr/bin/env python3
"""One-shot PDF → markdown/text for agent missions (GAIA attachments etc.).

Two engines behind one entrypoint, so mission objective notes never change:

  paddle  (default) — the format-robust PaddleOCR-VL pipeline from
          extract_batch.py (vector-figure text, no-text-layer pages, layout
          fidelity), minus the databank layout / figure sidecars / corpus
          verification bookkeeping. One invocation = one OS process owning
          its own mlx_vlm.server child (crash isolation, fig_review model).
  pymupdf — the raw text layer (fitz get_text), seconds-fast comparator and
          fallback. Pages with no text layer are flagged, never silent.

Usage:
  .venv/bin/python pdf_extract_one.py --pdf doc.pdf [--out doc.pdf.md]
      [--engine paddle|pymupdf] [--model <mlx dir>] [--dpi 160] [--port 0]

stdout = the markdown/text (or a one-line JSON report with --out); errors to
stderr, non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

# Server plumbing shared with extract_batch.py (same directory → importable
# when invoked by path).
from extract_batch import _free_port, _wait_health

_DEFAULT_MODEL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "PaddleOCR-VL-1.6-MLX-8bit"
)
_NO_TEXT_MARKER = "[no text layer on this page — try vl_inspect on a rendered page]"


def extract_pymupdf(pdf_path: str) -> str:
    """Raw text layer, page-marked. Flags pages without a text layer."""
    import fitz  # pymupdf

    doc = fitz.open(pdf_path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        pages.append(f"=== page {i + 1} ===\n{text or _NO_TEXT_MARKER}")
    doc.close()
    return "\n\n".join(pages)


def extract_paddle(pdf_path: str, model: str, dpi: int, port: int) -> str:
    """PaddleOCR-VL page loop from extract_batch.extract_paper, layout-only:
    no figure sidecars, no databank paths, no verification tallies."""
    import fitz  # pymupdf

    from paddleocr import PaddleOCRVL

    pipe = PaddleOCRVL(
        vl_rec_backend="mlx-vlm-server",
        vl_rec_server_url=f"http://127.0.0.1:{port}/",
        vl_rec_api_model_name=model,
    )
    doc = fitz.open(pdf_path)
    page_mds: list[str] = []
    with tempfile.TemporaryDirectory(prefix="pdfx1_") as tmp:
        for i, page in enumerate(doc):
            png = os.path.join(tmp, f"p{i}.png")
            page.get_pixmap(dpi=dpi).save(png)
            parts = []
            for res in pipe.predict(png):
                md = getattr(res, "markdown", None)
                if isinstance(md, dict):
                    parts.append(md.get("markdown_texts") or "")
                elif md:
                    parts.append(str(md))
            page_mds.append(
                f"=== page {i + 1} ===\n" + ("\n".join(p for p in parts if p).strip()
                                             or _NO_TEXT_MARKER)
            )
    doc.close()
    return "\n\n".join(page_mds)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default="", help="write here (default: stdout)")
    ap.add_argument("--engine", choices=("paddle", "pymupdf"), default="paddle")
    ap.add_argument("--model", default=_DEFAULT_MODEL, help="MLX VLM model dir (paddle)")
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument("--port", type=int, default=0,
                    help="0 = spawn a private mlx_vlm.server; N = reuse one (paddle)")
    args = ap.parse_args()

    if not os.path.isfile(args.pdf):
        print(f"pdf_extract_one: no such pdf: {args.pdf}", file=sys.stderr)
        return 2

    t0 = time.time()
    server = None
    try:
        if args.engine == "pymupdf":
            text = extract_pymupdf(args.pdf)
        else:
            port = args.port or _free_port()
            if not args.port:
                server = subprocess.Popen(
                    [sys.executable, "-m", "mlx_vlm.server", "--port", str(port)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if not _wait_health(port):
                print("pdf_extract_one: mlx server failed to start", file=sys.stderr)
                return 3
            text = extract_paddle(args.pdf, args.model, args.dpi, port)
    except Exception as e:  # noqa: BLE001 — CLI boundary: report, non-zero exit
        print(f"pdf_extract_one: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(json.dumps({
            "out": args.out, "engine": args.engine, "chars": len(text),
            "seconds": round(time.time() - t0, 1),
        }))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Exposed for the pymupdf comparator + tests (imported without paddle/mlx):
# extract_pymupdf, _NO_TEXT_MARKER.
