#!/usr/bin/env python3
"""One-shot audio → transcript for agent missions and the (future) scraper
audio ingest lane. Crash-isolated sibling of fig_review/pdf_extract_one:
one invocation = one OS process; models load from the HF cache.

Engines:
  parakeet (default) — mlx-community/parakeet-tdt-0.6b-v2 (English,
      SOTA-class WER, ~60x realtime on Apple Silicon, native chunking).
      NOTE: canary-qwen-2.5b was the requested primary but is impractical on
      macOS (NeMo speechlm2-only, no ports, no timestamps, 40s clip ceiling);
      parakeet is the evidence-backed peer.
  whisper — mlx-community/whisper-large-v3-turbo (multilingual). Also the
      AUTOMATIC FALLBACK if parakeet fails (non-English audio, odd formats).

Transcription only — no diarization by design; the reading model infers
speakers from context.

Usage:
  .venv/bin/python audio_transcribe.py --audio talk.mp3 [--out talk.transcript.txt]
      [--engine parakeet|whisper] [--timestamps] [--chunk-s 120]

stdout = transcript (or a one-line JSON report with --out); errors to stderr,
non-zero exit. Requires ffmpeg on PATH.

Module-level imports are stdlib-only ON PURPOSE (house convention) so tests
can import helpers without the mlx stack.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

PARAKEET_MODEL = "mlx-community/parakeet-tdt-0.6b-v2"
WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"


def fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def render_segments(segments: list, timestamps: bool) -> str:
    """segments: [(start_s, text)]. Plain joined text, or [m:ss]-prefixed lines."""
    if timestamps:
        return "\n".join(f"[{fmt_ts(s)}] {t.strip()}" for s, t in segments if t.strip())
    return " ".join(t.strip() for _, t in segments if t.strip())


def run_parakeet(audio: str, chunk_s: float) -> list:
    from parakeet_mlx import from_pretrained

    model = from_pretrained(PARAKEET_MODEL)
    result = model.transcribe(audio, chunk_duration=chunk_s, overlap_duration=15.0)
    # result.sentences: objects with .start/.end/.text (parakeet-mlx AlignedSentence)
    sents = getattr(result, "sentences", None) or []
    if sents:
        return [(float(s.start), str(s.text)) for s in sents]
    return [(0.0, str(getattr(result, "text", "") or ""))]


def run_whisper(audio: str) -> list:
    import mlx_whisper

    out = mlx_whisper.transcribe(audio, path_or_hf_repo=WHISPER_MODEL)
    segs = out.get("segments") or []
    if segs:
        return [(float(s["start"]), str(s["text"])) for s in segs]
    return [(0.0, str(out.get("text", "") or ""))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", default="", help="write here (default: stdout)")
    ap.add_argument("--engine", choices=("parakeet", "whisper"), default="parakeet")
    ap.add_argument("--timestamps", action="store_true",
                    help="prefix segments with [m:ss] (recommended for long audio)")
    ap.add_argument("--chunk-s", type=float, default=120.0,
                    help="parakeet chunk duration for long audio")
    args = ap.parse_args()

    if not os.path.isfile(args.audio):
        print(f"audio_transcribe: no such file: {args.audio}", file=sys.stderr)
        return 2
    if shutil.which("ffmpeg") is None:
        print("audio_transcribe: ffmpeg not on PATH (brew install ffmpeg)", file=sys.stderr)
        return 3

    t0 = time.time()
    engine = args.engine
    try:
        segments = run_parakeet(args.audio, args.chunk_s) if engine == "parakeet" \
            else run_whisper(args.audio)
    except Exception as e:  # noqa: BLE001 — engine fallback, then CLI boundary
        if engine == "parakeet":
            print(f"audio_transcribe: parakeet failed ({e}) — falling back to whisper",
                  file=sys.stderr)
            engine = "whisper"
            try:
                segments = run_whisper(args.audio)
            except Exception as e2:  # noqa: BLE001
                print(f"audio_transcribe: whisper fallback failed: {e2}", file=sys.stderr)
                return 1
        else:
            print(f"audio_transcribe: {e}", file=sys.stderr)
            return 1

    text = render_segments(segments, args.timestamps)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
        print(json.dumps({
            "out": args.out, "engine": engine, "chars": len(text),
            "segments": len(segments), "seconds": round(time.time() - t0, 1),
        }))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
