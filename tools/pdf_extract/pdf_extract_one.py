#!/usr/bin/env python3
"""One-shot PDF → markdown/text for agent missions (GAIA attachments etc.).

Two engines behind one entrypoint, so mission objective notes never change:

  paddle  (default) — the format-robust PaddleOCR-VL pipeline from
          extract_batch.py (vector-figure text, no-text-layer pages, layout
          fidelity), minus the databank layout / figure sidecars / corpus
          verification bookkeeping. One invocation = one OS process owning
          its own VLM server child (crash isolation, fig_review model).
  pymupdf — the raw text layer (fitz get_text), seconds-fast comparator and
          fallback. Pages with no text layer are flagged, never silent.

The paddle engine serves its per-region VLM calls from EITHER backend —
--vl-backend llamacpp (default, a GGUF pair) or mlx (the bundled 8-bit, a
station-dependent speed opt-in). Same layout pipeline, same crops, same
prompts, measurably identical fidelity; only the engine differs.

Usage:
  .venv/bin/python pdf_extract_one.py --pdf doc.pdf [--out doc.pdf.md]
      [--engine paddle|pymupdf] [--dpi 160] [--port 0]
      [--vl-backend llamacpp|mlx] [--model <gguf | mlx dir>]
      [--mmproj mmproj.gguf] [--vl-parallel 4]

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
from extract_batch import (
    _DEFAULT_MMPROJ,
    _DEFAULT_VL_BACKEND,
    _VL_BACKENDS,
    _default_vl_model,
    _free_port,
    _spawn_vl_server,
    _vl_pipe_kwargs,
    _wait_health,
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


def extract_paddle(
    pdf_path: str, model: str, dpi: int, port: int, backend: str = _DEFAULT_VL_BACKEND
) -> str:
    """PaddleOCR-VL page loop from extract_batch.extract_paper, layout-only:
    no figure sidecars, no databank paths, no verification tallies."""
    import fitz  # pymupdf

    from paddleocr import PaddleOCRVL

    pipe = PaddleOCRVL(**_vl_pipe_kwargs(backend, model, port))
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
                f"=== page {i + 1} ===\n"
                + ("\n".join(p for p in parts if p).strip() or _NO_TEXT_MARKER)
            )
    doc.close()
    return "\n\n".join(page_mds)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", default="", help="write here (default: stdout)")
    ap.add_argument("--engine", choices=("paddle", "pymupdf"), default="paddle")
    ap.add_argument(
        "--model",
        default="",
        help="VLM to serve — a GGUF (llamacpp) or an MLX model dir. "
        "Default: the backend's entry under models/",
    )
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument(
        "--port",
        type=int,
        default=0,
        help="0 = spawn a private VLM server; N = reuse one (paddle)",
    )
    ap.add_argument(
        "--vl-backend",
        choices=_VL_BACKENDS,
        default=_DEFAULT_VL_BACKEND,
        help=f"engine serving the per-region VLM calls "
        f"(default: {_DEFAULT_VL_BACKEND})",
    )
    ap.add_argument(
        "--mmproj",
        default="",
        help="projector GGUF for --vl-backend llamacpp "
        "(default: the entry under models/)",
    )
    ap.add_argument("--vl-parallel", type=int, default=4, help="llamacpp server slots")
    args = ap.parse_args()

    # Resolve the pair together — an explicit model with a defaulted projector
    # from a different quant is a mix that would load and quietly misbehave.
    if not args.model:
        args.model, default_mmproj = _default_vl_model(args.vl_backend)
        if not args.mmproj:
            args.mmproj = default_mmproj
    elif args.vl_backend == "llamacpp" and not args.mmproj:
        args.mmproj = _DEFAULT_MMPROJ

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
                server = _spawn_vl_server(
                    args.vl_backend,
                    args.model,
                    args.mmproj,
                    port,
                    args.vl_parallel,
                )
            if not _wait_health(port):
                print(
                    f"pdf_extract_one: {args.vl_backend} server failed to start",
                    file=sys.stderr,
                )
                return 3
            text = extract_paddle(args.pdf, args.model, args.dpi, port, args.vl_backend)
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
        print(
            json.dumps(
                {
                    "out": args.out,
                    "engine": args.engine,
                    "chars": len(text),
                    "seconds": round(time.time() - t0, 1),
                }
            )
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())


# Exposed for the pymupdf comparator + tests (imported without paddle/mlx):
# extract_pymupdf, _NO_TEXT_MARKER.
