#!/usr/bin/env python3
"""Batch PDF → markdown + figures extraction with deterministic verification.

One agent dispatch = one invocation of this script = one OS process
(crash isolation: the bake-off showed long-lived vision servers
accumulate state and die mid-batch; per-batch processes bound the blast
radius). The script owns its own VLM server child for the batch —
llama-server by default, mlx_vlm.server as a station-dependent opt-in
(--vl-backend; MLX is ~1.15x faster and exists on one machine, llama.cpp
is fidelity-identical and runs everywhere).

Pipeline per paper:
  1. pymupdf renders pages (160 dpi) and extracts the per-page text
     layer — the publisher's own text, used ONLY as the verification
     oracle, never as output (one output dialect: the engine's).
  2. PaddleOCR-VL (layout pipeline native, VLM over an OpenAI-shaped
     endpoint) produces per-page markdown; pages join into
     databank/markdown/<paper_key>.md.
  3. Figure crops from the pipeline are deduped (dHash) and filtered
     (size/entropy) into databank/figures/<paper_key>/fig_NN.png; the
     markdown references figures by relative path.
  4. Verification (truth-recall direction): on pages WITH a text
     layer, the PROSE text layer's numeric tokens must appear in our
     markdown (numbers are language-invariant grounding anchors) and
     sampled truth 5-grams must land in it. Rates are corpus-weighted
     across pages. Pages without a text layer are counted unverified —
     flagged, never silently trusted. "Prose" excludes vector-figure
     text blocks (see _prose_text): publishers that draw figures as
     vector art put axis ticks in the text layer, and the engine
     legitimately renders those figures as images (live: 26 of 26
     Nature-family papers failed at numeric 0.54-0.84 while faithful;
     prose-only rescored them 0.91-1.00). Calibration (live corpus):
     faithful extractions measure numeric 0.89-0.95, span 0.83-0.88;
     residual misses are affiliation postal codes, reference page
     ranges, and crystallographic overline notation.

Output: one JSON report line per paper on stdout. The agent action
(extraction_actions.extract_pdf_batch) parses these and applies the
quality policy; this script computes metrics, it does not judge.

Usage:
  .venv/bin/python extract_batch.py --pdfs a.pdf b.pdf \
      --databank-dir /path/to/databank \
      [--keys key_a key_b] [--dpi 160] \
      [--vl-backend llamacpp|mlx] [--model <gguf|mlx dir>] \
      [--mmproj mmproj.gguf] [--vl-parallel 4]

Weights default to the backend's entry under models/ (gitignored,
operator-placed), overridable per station with OUROBOROS_PADDLE_GGUF /
OUROBOROS_PADDLE_MMPROJ / OUROBOROS_PADDLE_MLX.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import fitz  # pymupdf
from PIL import Image

# ── Verification constants ────────────────────────────────────────────

# Numeric tokens: integers/decimals incl. signs and exponents. Single
# digits are excluded — they collide with list markers and footnote
# labels the engine legitimately restructures.
_NUM_RE = re.compile(r"-?\d+\.\d+(?:[eE][+-]?\d+)?|-?\d{2,}")

_SPAN_WORDS = 5  # n-gram length for sampled span checks
_SPANS_PER_PAGE = 8

# Vector-figure text filter: a text-layer block this short whose
# characters are mostly digits is an axis tick / data label, not prose.
_PROSE_MIN_BLOCK = 30  # chars; tick blocks are tiny ("20", "0.5", "2θ (°)")
_PROSE_DIGIT_FRAC = 0.5

# ── Figure filter constants ───────────────────────────────────────────

_MIN_FIG_PX = 96  # short side below this = rule/ornament, drop
_MIN_FIG_BYTES = 4096
_MIN_ENTROPY = 2.0  # near-uniform crops (separators, blank panels)
_DHASH_SIZE = 8

# NEAR-DUPLICATE RADIUS. This was 4 bits on a 64-bit hash of an 8x8 grayscale
# downsample, which is far too loose for a spectroscopy corpus: two DIFFERENT
# spectra sharing an overall envelope — the same pattern at successive delays,
# the same diffractogram with different indexing — routinely land inside 4 bits
# and one of them was silently deleted. Real data loss, recorded only as a
# counter.
#
# Now a two-stage test: a candidate must collide at 8x8 within _DHASH_MAX_DIST
# AND at 16x16 (256-bit) within _DHASH_CONFIRM_DIST. The second stage carries
# the detail that distinguishes near-identical panels; the first keeps it cheap.
_DHASH_MAX_DIST = int(os.environ.get("OUROBOROS_DHASH_MAX_DIST", "2"))
_DHASH_CONFIRM_SIZE = 16
_DHASH_CONFIRM_DIST = int(os.environ.get("OUROBOROS_DHASH_CONFIRM_DIST", "8"))


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── VL backend: llama.cpp (default) or MLX ───────────────────────────
# The layout pipeline is PaddleOCR-VL's own either way. What changes is
# only which engine answers the per-region VLM calls, and paddleocr 3.7
# supports both natively (_SUPPORTED_VL_BACKENDS) over one OpenAI-shaped
# client. The two wire differences it applies are PNG-vs-JPEG crop
# encoding and the max_tokens field name; crops, prompts and ordering are
# identical. So this is a true either/or, not two pipelines.
#
# LLAMA.CPP IS THE DEFAULT, and that is a PORTABILITY choice made with the
# speed cost known. MLX is measurably faster here — 75-80s against 88s on a
# 10-page paper (2026-08-12, llama.cpp b10360, 4 slots) — but it exists on
# exactly one machine in the fleet, and pinning the OCR stage to Apple
# Silicon pins the whole scrape pipeline with it. llama.cpp is the runtime
# everything else already runs on, so the default is the one that travels
# and MLX is the station-dependent opt-in.
#
# THE 1.15x IS THE WHOLE COST. Fidelity is IDENTICAL between the two:
# numeric recall 0.935, span recall 0.900 against the publisher's own text
# layer, on every run, both backends, BF16 and Q8_0 alike. Precision is not
# the gap either — Q8_0 vs BF16 sits inside run-to-run noise.
#
# The original "MLX, because llama.cpp is considerably slower" verdict was
# taken on llama-server b9910 against b10360 stable, on a model whose
# architecture (paddleocr) is newer than that build. A performance verdict
# is only as good as the binary under it — cf. DeepSeek-V4, where a stale
# build cost 4.2x decode.
_VL_BACKENDS = ("llamacpp", "mlx")
_DEFAULT_VL_BACKEND = os.environ.get("OUROBOROS_VL_BACKEND", "llamacpp")
_LLAMA_SERVER = os.environ.get("OUROBOROS_LLAMA_SERVER", "llama-server")

# Weights live in models/ (gitignored — operator-placed, as the MLX model
# always has been). Env overrides let a station point elsewhere without a
# code change; the GGUF entries here are symlinks into the LM Studio tree.
_MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
_DEFAULT_MLX_MODEL = os.environ.get(
    "OUROBOROS_PADDLE_MLX", os.path.join(_MODELS_DIR, "PaddleOCR-VL-1.6-MLX-8bit")
)
_DEFAULT_GGUF = os.environ.get(
    "OUROBOROS_PADDLE_GGUF", os.path.join(_MODELS_DIR, "PaddleOCR-VL-1.6-Q8_0.gguf")
)
_DEFAULT_MMPROJ = os.environ.get(
    "OUROBOROS_PADDLE_MMPROJ", os.path.join(_MODELS_DIR, "PaddleOCR-VL-1.6-mmproj.gguf")
)


def _default_vl_model(backend: str) -> tuple[str, str]:
    """(model, mmproj) for `backend` — one place, so the batch tool, the
    one-shot tool and the agent action cannot drift apart."""
    if backend == "mlx":
        return _DEFAULT_MLX_MODEL, ""
    return _DEFAULT_GGUF, _DEFAULT_MMPROJ


def _spawn_vl_server(
    backend: str,
    model: str,
    mmproj: str = "",
    port: int = 0,
    parallel: int = 1,
    ctx: int = 2048,
) -> subprocess.Popen:
    """Start the VLM server for `backend`. Caller owns termination.

    `ctx` is PER SLOT. llama-server's -c is the TOTAL cache divided across
    --parallel slots, so a fixed -c silently shrinks every slot's window as
    slots are added — and a region crop that no longer fits comes back
    truncated with no error anywhere. Measured 2026-08-12 on a 10-page
    paper: -c 8192 held numeric recall 0.935 / span 0.900 at 1, 4 and 8
    slots, then at 16 slots (512 tokens each) dropped to 0.910 / 0.850 and
    lost 4,482 characters. Same wall time, quietly worse extraction — the
    exact shape of defect that poisons a corpus without failing a run.
    Scaling here makes the knob mean what a caller thinks it means.
    """
    if backend == "mlx":
        cmd = [sys.executable, "-m", "mlx_vlm.server", "--port", str(port)]
    elif backend == "llamacpp":
        if not mmproj:
            raise ValueError("the llamacpp backend needs --mmproj (the projector)")
        cmd = [
            _LLAMA_SERVER,
            "-m",
            model,
            "--mmproj",
            mmproj,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "-ngl",
            "99",
            "-c",
            str(ctx * max(1, parallel)),
            "--parallel",
            str(parallel),
        ]
    else:
        raise ValueError(f"unknown VL backend {backend!r}; expected {_VL_BACKENDS}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _vl_pipe_kwargs(backend: str, model: str, port: int, concurrency: int = 0) -> dict:
    """PaddleOCRVL kwargs for `backend`.

    The api model name is passed for MLX (mlx_vlm.server loads per request,
    so the name IS the model) and left unset for llama.cpp, whose model is
    fixed at spawn and discovered from /v1/models.
    """
    kwargs: dict = {
        "vl_rec_backend": "mlx-vlm-server" if backend == "mlx" else "llama-cpp-server",
        "vl_rec_server_url": f"http://127.0.0.1:{port}/",
    }
    if backend == "mlx":
        kwargs["vl_rec_api_model_name"] = model
    if concurrency:
        kwargs["vl_rec_max_concurrency"] = concurrency
    return kwargs


def _wait_health(port: int, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3)
            return True
        except Exception:
            time.sleep(2)
    return False


# ── Text normalization (shared by all verification checks) ───────────


def _norm(s: str) -> str:
    s = re.sub(r"[#*_`|\[\]()>~\-]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _prose_text(page) -> str:
    """Text-layer prose for verification — vector-figure text excluded.

    Publishers that draw figures as vector graphics (Nature's whole
    family, some RSC/Elsevier) emit axis ticks and data labels into the
    text layer. The VLM renders those figures as images, so against the
    raw text layer every axis number counts as a miss. Drop short,
    digit-dominated blocks; prose blocks keep all their numerics.
    """
    parts = []
    for block in page.get_text("blocks"):
        text = block[4]
        stripped = re.sub(r"[\s,.\-–—°%()×±]+", "", text)
        if (
            len(text.strip()) < _PROSE_MIN_BLOCK
            and stripped
            and sum(c.isdigit() for c in stripped) / len(stripped) > _PROSE_DIGIT_FRAC
        ):
            continue
        parts.append(text)
    return "\n".join(parts)


def _verify_page(md: str, truth: str) -> tuple[int, int, int, int]:
    """(numeric_match_rate, span_pass_rate) for one page.

    DIRECTION MATTERS: fidelity means everything the PUBLISHER's text
    layer contains must appear in OUR markdown (truth ⊆ md). The VLM
    legitimately extracts MORE than the text layer — figure-internal
    text, axis labels, chart numbers — so the inverse check (md ⊆
    truth) false-flags exactly the engine's added value (live: a
    figure-heavy review scored 0.77 numeric under the inverted check
    while being a faithful extraction).
    """
    md_n, truth_n = _norm(md), _norm(truth)
    md_compact = re.sub(r"\s", "", md_n)

    nums = _NUM_RE.findall(truth_n)
    num_hit = sum(1 for n in nums if n in md_compact or n in md_n)

    words = truth_n.split()
    if len(words) >= _SPAN_WORDS:
        step = max(1, (len(words) - _SPAN_WORDS) // _SPANS_PER_PAGE)
        spans = [
            " ".join(words[i : i + _SPAN_WORDS])
            for i in range(0, len(words) - _SPAN_WORDS + 1, step)
        ][:_SPANS_PER_PAGE]
        # A span passes when most of its words landed in the markdown —
        # exact-substring is too strict across reading-order changes.
        passed = 0
        md_words = set(md_n.split())
        for sp in spans:
            sw = sp.split()
            if sum(1 for w in sw if w in md_words) >= len(sw) - 1:
                passed += 1
        span_n = len(spans)
    else:
        passed = span_n = 0
    # Caller aggregates CORPUS-WEIGHTED (sum hits / sum tokens): an
    # affiliation-heavy title page with 10 numerics must not weigh the
    # same as a results page with 200 (live: postal codes dragged an
    # unweighted page-mean to 0.79 on a faithful extraction).
    return num_hit, len(nums), passed, span_n


# ── Figure dedup/filter ───────────────────────────────────────────────


def _dhash(img: Image.Image, size: int = _DHASH_SIZE) -> int:
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            i = row * (size + 1) + col
            bits = (bits << 1) | (1 if px[i] > px[i + 1] else 0)
    return bits


def _entropy(img: Image.Image) -> float:
    hist = img.convert("L").histogram()
    total = sum(hist) or 1
    import math

    return -sum((c / total) * math.log2(c / total) for c in hist if c)


def _collect_figures(src_dir: str, dest_dir: str) -> tuple[int, int, dict]:
    """Dedup + filter figure crops from src into dest as fig_NN.png.

    Returns (kept, dropped, rename_map src_basename -> dest_relpath).
    """
    seen: list[tuple[int, str, Image.Image]] = []  # (dhash8, kept_name, image)
    kept = dropped = 0
    renames: dict[str, str] = {}

    def _note(src: str, reason: str, detail: str = "") -> None:
        """Record a drop on STDERR — stdout is the JSON report channel that
        action_extract_pdf_batch parses line-by-line. A dropped figure used to
        leave only a counter, so a wrongly-deduped panel was unreviewable."""
        print(
            f"figdrop {os.path.basename(src)} reason={reason}"
            + (f" {detail}" if detail else ""),
            file=sys.stderr,
        )

    candidates = []
    for root, _dirs, files in os.walk(src_dir):
        for f in sorted(files):
            if f.lower().endswith((".png", ".jpg", ".jpeg")):
                candidates.append(os.path.join(root, f))
    for path in candidates:
        try:
            if os.path.getsize(path) < _MIN_FIG_BYTES:
                dropped += 1
                _note(path, "size", f"bytes={os.path.getsize(path)}")
                continue
            img = Image.open(path)
            if min(img.size) < _MIN_FIG_PX or _entropy(img) < _MIN_ENTROPY:
                dropped += 1
                _note(path, "px_or_entropy", f"size={img.size} H={_entropy(img):.2f}")
                continue
            h = _dhash(img)
            collision = None
            for prev_h, prev_name, prev_img in seen:
                d8 = bin(h ^ prev_h).count("1")
                if d8 > _DHASH_MAX_DIST:
                    continue
                # CONFIRM AT HIGHER RESOLUTION. 8x8 cannot tell two spectra with
                # the same envelope apart; 16x16 can. Only a collision at BOTH
                # resolutions is a real duplicate.
                d16 = bin(
                    _dhash(img, _DHASH_CONFIRM_SIZE)
                    ^ _dhash(prev_img, _DHASH_CONFIRM_SIZE)
                ).count("1")
                if d16 <= _DHASH_CONFIRM_DIST:
                    collision = (prev_name, d8, d16)
                    break
            if collision:
                dropped += 1
                _note(
                    path,
                    "dhash",
                    f"matches={collision[0]} d8={collision[1]} d16={collision[2]}",
                )
                continue
            os.makedirs(dest_dir, exist_ok=True)
            name = f"fig_{kept:02d}.png"
            img.save(os.path.join(dest_dir, name))
            seen.append((h, name, img.copy()))
            renames[os.path.basename(path)] = name
            kept += 1
        except Exception as exc:  # noqa: BLE001 — one bad crop must not sink the paper
            dropped += 1
            # Errors used to be folded into the same counter as dedup drops,
            # so a decoder failure was indistinguishable from a duplicate.
            _note(path, f"error:{type(exc).__name__}", str(exc)[:120])
    return kept, dropped, renames


# ── Per-paper extraction ──────────────────────────────────────────────


def extract_paper(pipe, pdf_path: str, key: str, databank_dir: str, dpi: int) -> dict:
    t0 = time.time()
    report = {
        "paper_key": key,
        "md_path": "",
        "pages": 0,
        "verified_pages": 0,
        "unverified_pages": 0,
        "numeric_match_rate": 0.0,
        "span_pass_rate": 0.0,
        "figures_kept": 0,
        "figures_dropped": 0,
        "seconds": 0.0,
        "error": "",
    }
    md_dir = os.path.join(databank_dir, "markdown")
    fig_dir = os.path.join(databank_dir, "figures", key)
    os.makedirs(md_dir, exist_ok=True)

    try:
        doc = fitz.open(pdf_path)
        report["pages"] = len(doc)
        page_mds: list[str] = []
        num_hit = num_total = span_hit = span_total = 0

        with tempfile.TemporaryDirectory(prefix="pdfx_") as tmp:
            for i, page in enumerate(doc):
                png = os.path.join(tmp, f"p{i}.png")
                page.get_pixmap(dpi=dpi).save(png)
                truth = _prose_text(page)

                parts = []
                out_dir = os.path.join(tmp, f"out{i}")
                for res in pipe.predict(png):
                    md = getattr(res, "markdown", None)
                    if isinstance(md, dict):
                        parts.append(md.get("markdown_texts") or "")
                        # Some pipeline versions stash crops via save;
                        # harvest both shapes.
                        imgs = md.get("markdown_images") or {}
                        os.makedirs(out_dir, exist_ok=True)
                        for rel, im in imgs.items():
                            try:
                                im.save(os.path.join(out_dir, os.path.basename(rel)))
                            except Exception:
                                pass
                    elif md:
                        parts.append(str(md))
                page_md = "\n".join(p for p in parts if p)
                page_mds.append(page_md)

                if len(truth.strip()) >= 200 and page_md.strip():
                    nh, nt, sh, st = _verify_page(page_md, truth)
                    num_hit += nh
                    num_total += nt
                    span_hit += sh
                    span_total += st
                    report["verified_pages"] += 1
                else:
                    report["unverified_pages"] += 1

            kept, droppedn, renames = _collect_figures(tmp, fig_dir)
            report["figures_kept"] = kept
            report["figures_dropped"] = droppedn

        doc.close()
        joined = "\n\n---\n\n".join(page_mds)
        # Rewrite image refs the pipeline emitted to our relative layout.
        for old, new in renames.items():
            joined = joined.replace(old, f"../figures/{key}/{new}")
        md_path = os.path.join(md_dir, f"{key}.md")
        with open(md_path, "w") as f:
            f.write(joined)
        report["md_path"] = os.path.relpath(md_path, databank_dir)
        report["numeric_match_rate"] = num_hit / num_total if num_total else 1.0
        report["span_pass_rate"] = span_hit / span_total if span_total else 1.0
    except Exception as e:  # noqa: BLE001 - report, don't crash the batch
        report["error"] = f"{type(e).__name__}: {e}"
        if os.path.isdir(fig_dir) and not os.listdir(fig_dir):
            shutil.rmtree(fig_dir, ignore_errors=True)
    report["seconds"] = round(time.time() - t0, 1)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", nargs="+", required=True)
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--databank-dir", required=True)
    ap.add_argument(
        "--model",
        default="",
        help="VLM to serve — a GGUF (llamacpp) or an MLX model dir. "
        "Default: the backend's entry under models/",
    )
    ap.add_argument("--dpi", type=int, default=160)
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
    ap.add_argument(
        "--vl-parallel",
        type=int,
        default=4,
        help="llamacpp server slots; paddle fires region crops concurrently. "
        "Measured to saturate at 4 (2026-08-12)",
    )
    args = ap.parse_args()

    # Resolve weights together, so a half-specified pair cannot silently mix
    # an explicit model with a default projector from the other quant.
    if not args.model:
        args.model, default_mmproj = _default_vl_model(args.vl_backend)
        if not args.mmproj:
            args.mmproj = default_mmproj
    elif args.vl_backend == "llamacpp" and not args.mmproj:
        args.mmproj = _DEFAULT_MMPROJ

    keys = args.keys or [os.path.splitext(os.path.basename(p))[0] for p in args.pdfs]
    if len(keys) != len(args.pdfs):
        print(json.dumps({"error": "keys/pdfs length mismatch"}))
        return 2

    port = _free_port()
    try:
        server = _spawn_vl_server(
            args.vl_backend, args.model, args.mmproj, port, args.vl_parallel
        )
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 2
    try:
        if not _wait_health(port):
            print(json.dumps({"error": f"{args.vl_backend} server failed to start"}))
            return 3
        from paddleocr import PaddleOCRVL

        pipe = PaddleOCRVL(
            **_vl_pipe_kwargs(args.vl_backend, args.model, port),
        )
        for pdf, key in zip(args.pdfs, keys):
            report = extract_paper(pipe, pdf, key, args.databank_dir, args.dpi)
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
