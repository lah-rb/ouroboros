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
     prose-only rescored them 0.91-1.00). "Prose" also excludes MARGIN
     LINE NUMBERS and PUBLISHER FURNITURE (see _prose_text) — both were
     counting numbers a faithful extraction is right to drop.
  5. Degenerate-decode detection (_max_repeat_words). The rates above
     are RECALL, so a decode that falls into a loop keeps every number
     and scores clean while the document is ruined. Measured: the
     longest repeat across every paper judged fit to train was 19
     words; the two degenerate ones scored 675 and 1598.

The earlier calibration note here claimed faithful extractions measure
numeric 0.89-0.95 / span 0.83-0.88. That band was an ARTIFACT of the two
oracle bugs now fixed in _prose_text; a blind audit of 48 extractions
found the numeric rate correlates with judged quality at r = +0.02, so
it ranks nothing and the thresholds are a floor, not a quality bar.
Full record: dev/EXTRACTION_GATE_CALIBRATION_2026-08-14.md

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
from typing import Optional

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
# A drawing covering at least this fraction of the page is a figure, not a
# rule or an underline. Deliberately generous: over-excluding prose would
# understate recall and fail faithful papers, so the bar is set where a plot
# frame clears it and a table rule does not.
_FIG_REGION_MIN_AREA_FRAC = 0.01
# Scripts that do not delimit words with whitespace. Stripped from the truth
# before SPAN sampling only — see _verify_page. Numerics are untouched.
_CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿가-힯＀-￯]+")
_PROSE_DIGIT_FRAC = 0.5

# Margin-furniture detection — see _gutter_spans.
_PURE_DIGIT = re.compile(r"\d{1,4}")
_MARGIN_PAD = 6.0  # pt of slack, so a hanging indent is not read as a margin
_MIN_PROSE_SPANS = 5  # below this there is no column to measure against

# PUBLISHER FURNITURE. Every pattern must be one a body sentence cannot
# plausibly contain, because this drops the whole LINE. Deliberately narrower
# than the pattern that first exposed the problem: that probe also matched a
# bare "licence"/"email", which a Methods section legitimately uses. Each entry
# below is anchored to boilerplate structure — a URL form, a labelled field, a
# copyright year — rather than to a word.
_FURNITURE = re.compile(
    r"this content was downloaded"
    r"|downloaded from .{0,40}ip address"
    r"|\bdoi\s*:\s*10\.|\bdoi\.org/|dx\.doi\.org"
    r"|creativecommons\.org|creative commons attribution"
    r"|\bissn\b|\be-?issn\b"
    r"|all rights reserved"
    r"|see front matter"
    r"|©\s*\d{4}|\(c\)\s*(?:19|20)\d{2}"
    r"|\b(?:tel|fax)\.?\s*:\s*[+\d]"
    r"|published by (?:elsevier|springer|wiley|iop|the royal society)",
    re.I,
)

# Above this, a document is a book and gets a human decision instead of an OCR
# pass — see extract_paper. Above every paper that has succeeded on this corpus
# (max 177 pages), below the 560-page volume that cannot fit a dispatch budget.
_MAX_EXTRACT_PAGES = int(os.environ.get("OUROBOROS_MAX_EXTRACT_PAGES", "200"))

# Degenerate-decode detection — see _max_repeat_words.
_REPEAT_MAX_PERIOD = 24  # longest phrase treated as a loop unit

# PaddleOCR's internal table-cell tokens. They mark cell boundaries inside the
# model's table representation and must never reach output; when they do, a
# table was emitted as raw token soup instead of markup. Measured on the audit
# sample: 3 of 48 papers leak, ALL THREE tier C, none in A/B — perfect
# precision, low recall. BARE, not angle-wrapped: the first version of this
# pattern looked for <lcel> and found nothing while `lcel` sat in the text.
_TABLE_TOKEN_LEAK = re.compile(r"\b(?:lcel|fcel|ecel|ucel)\b", re.I)

# TABLE SIZE IS A RISK PREDICTOR, and the only one that survived measurement.
# Table damage — rows silently dropped, values bound to the wrong column — is
# the largest defect class in the blind audit (8 of 25 tier-C papers) and the
# rate metrics cannot see any of it: every number is still present. Nor can it
# be detected after the fact. Two candidate detectors were built and measured
# against the audit tiers, and both were discarded:
#
#   find_tables() row/cell comparison — ran BACKWARDS (damaged papers scored
#   0.625 row-keep vs 0.214 for everything else) because PyMuPDF's table
#   detection is unreliable on this corpus.
#   absence of <th> — not a defect at all: 93% of tables in the corpus carry
#   no header markup, so it measures the output format, not damage.
#
# What DOES separate is how big the biggest table is: median 22 rows in papers
# whose dominant defect is table structure, against 9 everywhere else and 9 in
# the papers judged fit to train. Recorded so a consumer can scope around it —
# the PROSE of these papers is repeatedly faithful even where the tables are
# wrecked, so dropping the paper would be the wrong trade.
_TABLE_ROW = re.compile(r"<tr\b", re.I)
_TABLE_BLOCK = re.compile(r"<table.*?</table>", re.S | re.I)


def _largest_table_rows(md: str) -> int:
    return max(
        (len(_TABLE_ROW.findall(t)) for t in _TABLE_BLOCK.findall(md)),
        default=0,
    )


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
_VL_BACKENDS = ("llmvp", "llamacpp", "mlx")
_DEFAULT_VL_BACKEND = os.environ.get("OUROBOROS_VL_BACKEND", "llmvp")
_LLAMA_SERVER = os.environ.get("OUROBOROS_LLAMA_SERVER", "llama-server")

# The fleet server: paddle held hot as a Phase 2b secondary rather than
# spawned per batch. No weights load, no teardown, and the OCR stage becomes
# visible to LLMVP's model management instead of being a private subprocess.
# The port is the SERVER's, so nothing here picks a free one.
_LLMVP_URL = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008")
_LLMVP_MODEL = os.environ.get("OUROBOROS_LLMVP_VL_MODEL", "paddle-ocr-vl")

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
    if backend == "llmvp":
        raise ValueError(
            "the llmvp backend uses the RUNNING fleet server — nothing to spawn"
        )
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
    so the name IS the model) and left unset for a spawned llama.cpp server,
    whose model is fixed at spawn and discovered from /v1/models.

    `llmvp` is the FLEET server: no subprocess, no spawn, paddle already hot
    beside the primary as a Phase 2b secondary. The name MUST be sent there —
    LLMVP serves several models from one port and its routing is strict, so
    an unnamed request would be answered by the primary rather than by
    paddle. That is also the reason its /v1/models lists the hot entries.
    """
    kwargs: dict = {
        "vl_rec_backend": "mlx-vlm-server" if backend == "mlx" else "llama-cpp-server",
        "vl_rec_server_url": f"http://127.0.0.1:{port}/",
    }
    if backend == "mlx":
        kwargs["vl_rec_api_model_name"] = model
    elif backend == "llmvp":
        # THE /v1 IS LOAD-BEARING. paddlex builds an AsyncOpenAI client from
        # this URL, and the OpenAI SDK appends "/chat/completions" to it —
        # so a bare host posts to /chat/completions. llama-server answers
        # there AND at /v1/chat/completions, which is why the spawned backend
        # gets away with a root URL; LLMVP mounts its shim only under /v1 and
        # 404s. Cost one live run to find.
        kwargs["vl_rec_server_url"] = f"{_LLMVP_URL.rstrip('/')}/v1/"
        kwargs["vl_rec_api_model_name"] = model or _LLMVP_MODEL
    if concurrency:
        kwargs["vl_rec_max_concurrency"] = concurrency
    return kwargs


def _llmvp_models(port: int, timeout: float = 3.0) -> Optional[set]:
    """Model ids the fleet server will serve, or None if it is not answering."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/models", timeout=timeout
        ) as r:
            return {m.get("id") for m in json.load(r).get("data", [])}
    except Exception:  # noqa: BLE001 — down, starting, or not listening
        return None


def _llmvp_load(port: int, model: str, timeout: float = 300.0) -> tuple[bool, str]:
    """Ask LLMVP to make ``model`` hot. Returns (ok, detail).

    THIS IS ORCHESTRATION, NOT A SIDE EFFECT OF INFERENCE, and the distinction
    is LLMVP's own: a completion never loads weights, because a multi-minute
    load hiding inside a request is how a timeout becomes a mystery. loadModel
    is the explicit door, so the batch knocks on it once at startup and
    reports what it hears — including a governor refusal, which is a sizing
    decision the operator needs to read rather than a crash.
    """
    q = {
        "query": "mutation($n:String!){ loadModel(name:$n){ ok state "
        "footprintGb detail } }",
        "variables": {"n": model},
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/graphql",
        data=json.dumps(q).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.load(r)
    except Exception as exc:  # noqa: BLE001 — report, the caller decides
        return False, f"{type(exc).__name__}: {exc}"
    if body.get("errors"):
        return False, json.dumps(body["errors"])[:300]
    res = (body.get("data") or {}).get("loadModel") or {}
    return bool(res.get("ok")), res.get("detail") or ""


def _ensure_llmvp_model(port: int, model: str) -> tuple[bool, str]:
    """Server up AND ``model`` servable, loading it if it is merely cold.

    Readiness and routability are one question here: LLMVP serves several
    models from one port with STRICT routing, so a server that is up but has
    not loaded the OCR model would refuse every crop. Resolving it before any
    page is rendered turns a per-crop failure into one clear startup answer.
    """
    names = _llmvp_models(port)
    if names is None:
        return False, "not reachable"
    if model in names:
        return True, "already hot"
    ok, detail = _llmvp_load(port, model)
    if not ok:
        return False, f"loadModel refused: {detail}"
    names = _llmvp_models(port)
    if names is None or model not in names:
        return False, "loadModel reported ok but the model is not listed"
    return True, "loaded"


def _wait_health(port: int, timeout: float = 120.0) -> bool:
    """Readiness for a SPAWNED server (llama-server / mlx_vlm.server).

    Not usable for the fleet server: /health is a llama-server route and
    LLMVP answers 404 there (it is GraphQL-first). See _ensure_llmvp_model.
    """
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


def _figure_regions(page) -> list:
    """Rectangles on this page that are figures — vector art or raster.

    A drawing this large is a plot frame, an axis or a shaded series, not a
    rule or an underline. Small strokes are ignored precisely because a table
    rule or a text underline would otherwise swallow the prose beside it.
    """
    rects = []
    try:
        page_area = abs(page.rect.width * page.rect.height) or 1.0
        for d in page.get_drawings():
            r = d.get("rect")
            if r is None:
                continue
            area = abs(r.width * r.height)
            if area / page_area >= _FIG_REGION_MIN_AREA_FRAC:
                rects.append(r)
        for img in page.get_images(full=True):
            try:
                rects.extend(page.get_image_rects(img[0]))
            except Exception:  # noqa: BLE001 — an unreachable xref is not fatal
                continue
    except Exception:  # noqa: BLE001 — verification must not break extraction
        return []
    return rects


def _gutter_spans(page) -> set:
    """Anchors of pure-digit spans lying OUTSIDE the page's prose column.

    A manuscript's line-number column is such a span. PyMuPDF's "blocks"
    output merges it into the prose line beside it, so block-level reading
    cannot separate them and the number enters the truth as if it were
    content. Detection is geometric, never sequential: a pure-digit span
    outside the horizontal extent of the prose is furniture — a line number
    or a folio — and never a measurement.
    """
    prose_x0, prose_x1, digits = [], [], []
    try:
        for blk in page.get_text("dict")["blocks"]:
            for ln in blk.get("lines", []):
                for sp in ln["spans"]:
                    t = sp["text"].strip()
                    if not t:
                        continue
                    if _PURE_DIGIT.fullmatch(t):
                        digits.append(sp["bbox"])
                    else:
                        prose_x0.append(sp["bbox"][0])
                        prose_x1.append(sp["bbox"][2])
    except Exception:  # noqa: BLE001 — verification must not break extraction
        return set()
    # Too little prose to establish a column: refuse to guess. Dropping digits
    # on a table-only page would delete the data we are trying to verify.
    if not digits or len(prose_x0) < _MIN_PROSE_SPANS:
        return set()
    left, right = min(prose_x0), max(prose_x1)
    return {
        (round(b[0], 1), round(b[1], 1))
        for b in digits
        if b[2] < left - _MARGIN_PAD or b[0] > right + _MARGIN_PAD
    }


def _prose_text(page) -> str:
    """Text-layer prose for verification — vector-figure text excluded.

    Publishers that draw figures as vector graphics (Nature's whole
    family, some RSC/Elsevier) emit axis ticks and data labels into the
    text layer. The VLM renders those figures as images, so against the
    raw text layer every axis number counts as a miss.

    TWO FILTERS, because one was not enough. The original rule — short AND
    digit-dominated — catches axis ticks and misses everything else a figure
    contains: legend entries, panel captions, inset annotations, sample codes.
    On figure-dense papers that residue is large enough to sink a faithful
    extraction. Measured 2026-08-12 on a live corpus: papers that FAILED the
    quality gate averaged 38.3 figures against 23.2 for those that passed,
    with numeric recall correlating negatively with figure count (r = -0.47).
    The three worst carried 118, 112 and 48 figures.

    So blocks are also dropped SPATIALLY — if a text block sits inside a
    figure region, it is figure text whatever it looks like. That is the
    property that actually distinguishes it, rather than a proxy for it.
    A page with no detected figures behaves exactly as before.

    TWO MORE, added 2026-08-14 after a blind audit found the numeric rate
    UNCORRELATED (r = +0.02) with judged document quality. Both were counting
    numbers the extractor is RIGHT to drop, so a faithful extraction was
    charged for its own correctness:

    - MARGIN LINE NUMBERS. Assembled from SPANS rather than blocks so a
      line-number can be removed without taking the prose line PyMuPDF merged
      it into. On a line-numbered manuscript this was ~40 phantom misses per
      page; 12 of 12 affected papers in the audit sample were failing the gate,
      median numeric 0.528 -> 0.977.
    - PUBLISHER FURNITURE. The access stamp a publisher injects at download
      time, the DOI/ISSN, the copyright footer. A fixed cost against a small
      denominator, so it sinks SHORT faithful papers hardest: one tier-A
      conference paper lost 47 of its 47 numbers to an IOP download stamp.

    The figure-region test stays at BLOCK granularity on purpose. Moving it to
    spans would change a second thing at once, and the measurement that
    justified this could no longer attribute its own delta.
    """
    regions = _figure_regions(page)
    gutters = _gutter_spans(page)
    parts = []
    try:
        blocks = page.get_text("dict")["blocks"]
    except Exception:  # noqa: BLE001 — fall back to the block reader
        blocks = []
    for blk in blocks:
        bbox = blk.get("bbox")
        if bbox is None or "lines" not in blk:
            continue
        if regions:
            cx, cy = (bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0
            if any(r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1 for r in regions):
                continue
        chunks = []
        for ln in blk["lines"]:
            for sp in ln["spans"]:
                if (round(sp["bbox"][0], 1), round(sp["bbox"][1], 1)) in gutters:
                    continue
                chunks.append(sp["text"])
            chunks.append("\n")
        text = "".join(chunks)
        if not text.strip():
            continue
        stripped = re.sub(r"[\s,.\-–—°%()×±]+", "", text)
        if (
            len(text.strip()) < _PROSE_MIN_BLOCK
            and stripped
            and sum(c.isdigit() for c in stripped) / len(stripped) > _PROSE_DIGIT_FRAC
        ):
            continue
        # Furniture is dropped LINE-wise, not block-wise: a running footer is
        # often glued to the last line of real prose.
        text = "\n".join(ln for ln in text.split("\n") if not _FURNITURE.search(ln))
        if text.strip():
            parts.append(text)
    return "\n".join(parts)


# Script census over the produced markdown. The span metric is an
# English-prose instrument (CJK is stripped before sampling above), so the
# VERDICT layer needs to know when a low span score means "non-Latin paper"
# rather than "unfaithful extraction" — numerics are the language-invariant
# anchor either way. Mirrored in agent/actions/extraction_actions.py
# (separate venvs — keep in sync).
_SCRIPT_RANGES = (
    ("latin", ((0x0041, 0x024F),)),
    ("cyrillic", ((0x0400, 0x04FF),)),
    ("greek", ((0x0370, 0x03FF),)),
    # Han + kana + CJK punctuation/fullwidth, matching _CJK_RE's spirit.
    ("cjk", ((0x3000, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF))),
    ("hangul", ((0xAC00, 0xD7AF), (0x1100, 0x11FF))),
)


def _script_profile(text: str) -> dict:
    """Letter-class fractions of ``text`` (digits/punct/space excluded).

    Returns {"latin": f, "cyrillic": f, ..., "nonlatin": f} rounded to 3
    places; all zeros for text with no classified letters.
    """
    counts = {name: 0 for name, _ in _SCRIPT_RANGES}
    total = 0
    for ch in text:
        cp = ord(ch)
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= cp <= hi for lo, hi in ranges):
                counts[name] += 1
                total += 1
                break
    if not total:
        return {**{k: 0.0 for k in counts}, "nonlatin": 0.0}
    out = {k: round(v / total, 3) for k, v in counts.items()}
    out["nonlatin"] = round(1.0 - counts["latin"] / total, 3)
    return out


def _max_repeat_words(md: str, max_period: int = _REPEAT_MAX_PERIOD) -> int:
    """Words spanned by the longest back-to-back repeated block.

    The gate's rate metrics are RECALL — "does this number appear anywhere on
    the page" — so they are blind to the VLM's worst failure mode: a decode
    that falls into a loop and emits the same clause hundreds of times. Every
    number is still present, so the rates stay clean while the document is
    ruined. Measured on the audit sample: the longest repeat across all 19
    papers judged fit to train was 19 words; the two degenerate documents
    scored 675 and 1598.

    For a period p, a run of L consecutive positions with w[i] == w[i+p] means
    a p-word block repeats; the repeated region spans L + p words. L >= p is
    required so one coincidental match cannot register as a loop.
    """
    words = re.findall(r"\S+", md)
    best = 0
    for p in range(1, max_period + 1):
        if len(words) <= p:
            break
        run = 0
        for i in range(len(words) - p):
            if words[i] == words[i + p]:
                run += 1
                if run >= p:
                    best = max(best, run + p)
            else:
                run = 0
    return best


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

    # SPAN IS A WHITESPACE METRIC. On a script that does not delimit words it
    # measures nothing: a 5-"word" n-gram becomes one huge character run that
    # never matches, and a faithful page scores near zero.
    #
    # CJK runs are dropped from the truth before sampling rather than the whole
    # page being skipped, because the papers that hit this are BILINGUAL — the
    # one that exposed it is a Chinese journal printing an English title,
    # abstract and captions alongside the Chinese. Skipping whole pages threw
    # away checkable English prose and still left mixed pages scoring badly
    # (measured: span 0.29 -> 0.44, still under the gate). Stripping keeps
    # every English span checkable and removes only what cannot be scored.
    #
    # Numerics are untouched — they are language-invariant, which is the whole
    # reason the numeric check is the primary anchor.
    truth_spanable = _CJK_RE.sub(" ", truth_n)

    words = truth_spanable.split()
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


def _collect_figures(
    src_dir: str, dest_dir: str, start: int = 0
) -> tuple[int, int, dict]:
    """Dedup + filter figure crops from src into dest as fig_NN.png.

    ``start`` offsets the numbering (book-segment mode appends to a dir that
    already holds earlier segments' figures; full runs keep 0 so a retry
    overwrites rather than duplicates).

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
            name = f"fig_{start + kept:02d}.png"
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


def extract_paper(
    pipe,
    pdf_path: str,
    key: str,
    databank_dir: str,
    dpi: int,
    temperature: float = 0.8,
    top_p: float = 0.95,
    page_range: tuple | None = None,
) -> dict:
    """``page_range=(a, b)`` extracts pages [a, b) only — the BOOK SEGMENT
    mode. An explicit range is operator intent, so the oversize referral is
    bypassed; the markdown lands in a part file (markdown/<key>.part_AAAA.md)
    for the drain to assemble once every segment is done, and figure
    numbering continues from what is already on disk so segments never
    clobber earlier crops."""
    t0 = time.time()
    report = {
        "paper_key": key,
        "md_path": "",
        "pages": 0,
        "total_pages": 0,
        "page_range": list(page_range) if page_range else None,
        "verified_pages": 0,
        "unverified_pages": 0,
        "numeric_match_rate": 0.0,
        "span_pass_rate": 0.0,
        "script_profile": {},
        "max_repeat_words": 0,
        "oversize": False,
        "table_token_leak": 0,
        "largest_table_rows": 0,
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
        report["total_pages"] = len(doc)
        if page_range:
            lo = max(0, int(page_range[0]))
            hi = min(len(doc), int(page_range[1]))
            page_indices = list(range(lo, hi))
        else:
            page_indices = list(range(len(doc)))
        report["pages"] = len(page_indices)
        # OVERSIZE: MEASURED, NOT ATTEMPTED. A dispatch shares one timeout
        # across its whole batch, so a book does not merely fail — it burns
        # the budget its companions needed and takes them down with it. One
        # corpus asset is a 560-page USGS volume; at ~7 s/page it cannot fit,
        # and 30 minutes were spent discovering that. The threshold sits above
        # every paper that has ever succeeded here (largest: 177 pages) and
        # well below a book, so this refuses volumes without touching long
        # review articles. Not a failure — a referral: these are substantial
        # documents that deserve a decision before any GPU is spent on them.
        if page_range is None and len(doc) > _MAX_EXTRACT_PAGES:
            report["oversize"] = True
            report["error"] = ""
            report["seconds"] = round(time.time() - t0, 1)
            doc.close()
            return report
        page_mds: list[str] = []
        num_hit = num_total = span_hit = span_total = 0

        with tempfile.TemporaryDirectory(prefix="pdfx_") as tmp:
            for i in page_indices:
                page = doc[i]
                png = os.path.join(tmp, f"p{i}.png")
                page.get_pixmap(dpi=dpi).save(png)
                truth = _prose_text(page)

                parts = []
                out_dir = os.path.join(tmp, f"out{i}")
                # Explicit, every call: the client otherwise pins temperature
                # to 0 (greedy) for llama-cpp-server backends, and greedy
                # loops deterministically on some pages. See --vl-temperature.
                for res in pipe.predict(png, temperature=temperature, top_p=top_p):
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

            fig_start = (
                sum(1 for f in os.listdir(fig_dir) if f.startswith("fig_"))
                if page_range and os.path.isdir(fig_dir)
                else 0
            )
            kept, droppedn, renames = _collect_figures(tmp, fig_dir, start=fig_start)
            report["figures_kept"] = kept
            report["figures_dropped"] = droppedn

        doc.close()
        joined = "\n\n---\n\n".join(page_mds)
        # Rewrite image refs the pipeline emitted to our relative layout.
        for old, new in renames.items():
            joined = joined.replace(old, f"../figures/{key}/{new}")
        # The pipeline emits refs as imgs/<basename>; the basename rewrite
        # above left a stale "imgs/" prefix, and "imgs/../figures/…"
        # collapses to markdown/figures/… — one directory too shallow. Live:
        # every figure link in the corpus resolved to a nonexistent path
        # (invisible to the substring-anchored consumers, broken for any
        # path-resolving reader — caught in the 2026-08-16 triage review).
        joined = joined.replace("imgs/../figures/", "../figures/")
        # Refs to figures _collect_figures DROPPED (dedup/junk filter) have
        # no rename entry and would dangle at a file that exists nowhere.
        # Replace the tag with an inert marker so the drop is visible in
        # the document instead of masquerading as a broken image.
        joined = re.sub(
            r'<img\s[^>]*src="imgs/[^"]+"[^>]*/?>',
            "*[figure removed by extraction filter]*",
            joined,
        )
        md_name = f"{key}.part_{page_range[0]:04d}.md" if page_range else f"{key}.md"
        md_path = os.path.join(md_dir, md_name)
        with open(md_path, "w") as f:
            f.write(joined)
        report["md_path"] = os.path.relpath(md_path, databank_dir)
        report["numeric_match_rate"] = num_hit / num_total if num_total else 1.0
        report["span_pass_rate"] = span_hit / span_total if span_total else 1.0
        report["script_profile"] = _script_profile(joined)
        # Measured on the joined document: a loop can straddle a page boundary,
        # and the rates above cannot see one at all.
        report["max_repeat_words"] = _max_repeat_words(joined)
        # RECORDED, NOT GATED. The signal is real (3 for 3 on the audit
        # sample, no false positives) but only one paper carried enough of
        # them to justify a cut, and fitting a threshold to one example is how
        # a checker gets shipped that measures nothing. Surfaced for triage
        # until there is enough of it to calibrate against.
        report["table_token_leak"] = len(_TABLE_TOKEN_LEAK.findall(joined))
        report["largest_table_rows"] = _largest_table_rows(joined)
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
    ap.add_argument(
        "--page-range",
        default=None,
        help="A:B — extract pages [A, B) only (book-segment mode; single key)",
    )
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
    # SAMPLING IS PINNED BY THE CLIENT, NOT THE SERVER, so this is the ONLY
    # effective control: paddlex sends an explicit `temperature: 0` to every
    # llama-cpp-server backend when none is given (predictor.py:503), which
    # overrides any server-side generation default. Greedy is the lab default
    # for PaddleOCR-VL and it ORBITS on loop-prone pages — one paper repeated
    # the word "both" 3,003 times, byte-identically, on two machines and two
    # serving paths. The lab's own docs offer `--do-sample true
    # --temperature 0.8` as the stochastic alternative; these defaults are
    # that. NOTE the historical corpus (through 2026-08-15) was extracted at
    # the client-pinned 0 — every quality figure predating these flags is a
    # greedy figure.
    ap.add_argument(
        "--vl-temperature",
        type=float,
        default=0.8,
        help="VL sampling temperature passed per predict() (0 = greedy, "
        "which deterministically loops on some pages)",
    )
    ap.add_argument(
        "--vl-top-p",
        type=float,
        default=0.95,
        help="VL nucleus sampling threshold passed per predict()",
    )
    args = ap.parse_args()

    # Resolve weights together, so a half-specified pair cannot silently mix
    # an explicit model with a default projector from the other quant.
    # The llmvp backend loads NO local weights — the fleet server already
    # holds them. `model` there is a registry NAME, not a path, so resolving
    # a GGUF for it would be meaningless and the mmproj is the server's.
    if args.vl_backend == "llmvp":
        args.model = args.model or _LLMVP_MODEL
        args.mmproj = ""
    elif not args.model:
        args.model, default_mmproj = _default_vl_model(args.vl_backend)
        if not args.mmproj:
            args.mmproj = default_mmproj
    elif args.vl_backend == "llamacpp" and not args.mmproj:
        args.mmproj = _DEFAULT_MMPROJ

    keys = args.keys or [os.path.splitext(os.path.basename(p))[0] for p in args.pdfs]
    if len(keys) != len(args.pdfs):
        print(json.dumps({"error": "keys/pdfs length mismatch"}))
        return 2

    # The llmvp backend attaches to the RUNNING fleet server: no spawn, no
    # teardown, and the batch fails fast if it is not up rather than silently
    # falling back to a subprocess (which would load a second copy of paddle
    # alongside the hot one).
    server = None
    if args.vl_backend == "llmvp":
        port = int(_LLMVP_URL.rsplit(":", 1)[-1])
        ok, why = _ensure_llmvp_model(port, args.model)
        if not ok:
            print(
                json.dumps(
                    {
                        "error": f"LLMVP at {_LLMVP_URL} cannot serve "
                        f"{args.model!r}: {why}. Start the server, or use "
                        f"--vl-backend llamacpp to spawn a private one."
                    }
                )
            )
            return 3
    else:
        port = _free_port()
        try:
            server = _spawn_vl_server(
                args.vl_backend, args.model, args.mmproj, port, args.vl_parallel
            )
        except (ValueError, OSError) as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
            return 2
    try:
        if server is not None and not _wait_health(port):
            print(json.dumps({"error": f"{args.vl_backend} server failed to start"}))
            return 3
        from paddleocr import PaddleOCRVL

        pipe = PaddleOCRVL(
            **_vl_pipe_kwargs(
                args.vl_backend,
                args.model,
                port,
                # The fleet server's parallelism is its OWN vision_pool_size,
                # not a flag here — but the CLIENT still has to fan out to use
                # it, so the crop concurrency is passed through.
                concurrency=args.vl_parallel if args.vl_backend == "llmvp" else 0,
            ),
        )
        for pdf, key in zip(args.pdfs, keys):
            pr = None
            if args.page_range:
                a, _, b = args.page_range.partition(":")
                pr = (int(a), int(b))
            report = extract_paper(
                pipe,
                pdf,
                key,
                args.databank_dir,
                args.dpi,
                temperature=args.vl_temperature,
                top_p=args.vl_top_p,
                page_range=pr,
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
    finally:
        # Guard the whole TEARDOWN, not the return. A `return` inside finally
        # swallows any in-flight exception and overrides the exit code; and
        # the llmvp backend owns no subprocess to reap.
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
