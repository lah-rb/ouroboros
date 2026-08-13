#!/usr/bin/env python3
"""Does the OCR subprocess leave GPU headroom for the resident model?

THE QUESTION. The 2026-08-13 overnight scrape spent 540.6 min of wall clock
as 384.6 min of paddle OCR (`command_run`) + 142.1 min of muse inference,
with **0.0 min of overlap** — the agent's cycle loop runs one flow, one step
at a time, so the OCR subprocess and the resident model never work at once.
A perfect overlap floors the run at max(384.6, 142.1) = 384.6 min, i.e. 156
min (28.9%) is on the table. Whether it is REALLY on the table depends
entirely on whether one GPU can do both at once.

PRIOR EVIDENCE, BOTH DIRECTIONS (dev/MULTI_MODEL_PLAN.md):
  * P0.b, two models IN ONE PROCESS: concurrent wall == solo SUM on 5/6
    questions — Metal serialized the kernels. Co-residency bought nothing
    and cost a greedy divergence. Phase 2b parked.
  * probe_cross_process_decode, 2026-07-17, two models in TWO PROCESSES:
    byte-identical output, concurrent wall == the SLOWER LEG ALONE. Ideal.

The OCR path is already the second regime — `_spawn_vl_server` runs
llama-server as its own process. But that probe paired gpt-oss with a 32B
dense model, both decoding. This pairs a 0.9B VL model firing FOUR
concurrent region crops (`--vl-parallel 4`, the measured saturation point)
with a 30B dense model decoding at 19 tok/s. Prefill-heavy vs
bandwidth-bound, and saturation at 4 slots is a reason to doubt free
headroom. Extrapolating from the 2026-07-17 pair would be exactly the kind
of assumption this codebase has been burned by.

PROTOCOL (P0.b's shape, adapted). Three arms, fixed work in each leg:
  A   OCR alone            — N pdfs through extract_batch, production flags
  B   inference alone      — M sequential completions on the live server
  C   both, gathered       — the same A work and the same B work at once

  ratio = t_C / max(t_A, t_B).   1.0 = free overlap; (t_A+t_B)/max = serial.

Fidelity is checked in both legs, because a throughput win that quietly
degrades either side is not a win: OCR compares extracted chars and the
verification rates per paper, inference compares text at temperature 0.

The probe writes OCR output to a scratch databank so the corpus is never
touched. Usage:

    .venv/bin/python dev/gpu_contention_probe.py --pdfs 2 --calls 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS = Path.home() / "corpora" / "ouroboros-spectra"
LLMVP_URL = "http://127.0.0.1:8008/graphql"

COMPLETION_QUERY = """
query Completion($request: CompletionRequest!) {
    completion(request: $request) {
        text
        tokensGenerated
        promptTokens
        prefillMs
        decodeMs
    }
}
"""

# Shaped like the production `tag_papers` call: a paragraph of paper
# metadata in, a structured judgement out. The exact content does not
# matter; the token profile does (~1.3k generated in production).
PROMPTS = [
    "A paper reports laser-induced breakdown spectroscopy of basaltic "
    "glass at 1064 nm, calibrating Mg/Si against certified reference "
    "materials. Decide whether it belongs in a corpus about remote "
    "mineral identification, and justify the decision in three sentences.",
    "A paper measures thermal emission spectra of carbonate-bearing dust "
    "between 400 and 1400 cm^-1 and fits a two-component mixing model. "
    "Decide whether it belongs in a corpus about remote mineral "
    "identification, and justify the decision in three sentences.",
    "A paper describes a convolutional network classifying hyperspectral "
    "imagery of agricultural land cover at 30 m resolution. Decide "
    "whether it belongs in a corpus about remote mineral identification, "
    "and justify the decision in three sentences.",
    "A paper derives optical constants for olivine from reflectance "
    "measurements on pressed pellets across 0.3-25 microns. Decide "
    "whether it belongs in a corpus about remote mineral identification, "
    "and justify the decision in three sentences.",
]


def pick_pdfs(n: int) -> list[Path]:
    """Median-sized PDFs — not the smallest (unrepresentative) nor the
    largest (a single outlier would dominate the leg)."""
    pdfs = sorted(
        (CORPUS / "pdfs").glob("*.pdf"), key=lambda p: p.stat().st_size, reverse=True
    )
    if not pdfs:
        sys.exit(f"no PDFs under {CORPUS / 'pdfs'}")
    mid = len(pdfs) // 2
    return pdfs[mid : mid + n]


async def ocr_leg(pdfs: list[Path], databank: Path, tag: str) -> dict:
    """One extract_batch dispatch — exactly what the agent action runs."""
    databank.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(REPO / "tools" / "pdf_extract" / ".venv" / "bin" / "python"),
        str(REPO / "tools" / "pdf_extract" / "extract_batch.py"),
        "--pdfs",
        *[str(p) for p in pdfs],
        "--keys",
        *[p.stem for p in pdfs],
        "--databank-dir",
        str(databank),
        "--vl-backend",
        "llamacpp",
    ]
    t0 = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    wall = time.monotonic() - t0

    reports = []
    for line in (out or b"").decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(r, dict) and r.get("paper_key"):
            reports.append(r)
    papers = []
    for r in reports:
        md = databank / (r.get("md_path") or "")
        papers.append(
            {
                "key": r.get("paper_key"),
                "pages": r.get("pages"),
                "chars": (
                    len(md.read_text(encoding="utf-8", errors="replace"))
                    if r.get("md_path") and md.is_file()
                    else 0
                ),
                "numeric": r.get("numeric_match_rate"),
                "span": r.get("span_pass_rate"),
                "seconds": r.get("seconds"),
                "error": r.get("error"),
            }
        )
    return {
        "arm": tag,
        "wall_s": round(wall, 1),
        "rc": proc.returncode,
        # Sum of per-paper `seconds` vs the leg wall exposes the
        # spawn+teardown overhead a persistent server would delete.
        "papers_s": round(sum(float(p["seconds"] or 0) for p in papers), 1),
        "papers": papers,
    }


async def infer_leg(calls: int, max_tokens: int, tag: str) -> dict:
    """M SEQUENTIAL completions — production shape (one per cycle), not a
    fan-out. Fanning out here would measure the batched engine instead of
    the contention this probe is about."""
    import aiohttp

    rows = []
    t0 = time.monotonic()
    async with aiohttp.ClientSession() as sess:
        for i in range(calls):
            req = {
                "prompt": PROMPTS[i % len(PROMPTS)],
                "maxTokens": max_tokens,
                "temperature": 0.0,
            }
            c0 = time.monotonic()
            async with sess.post(
                LLMVP_URL,
                json={"query": COMPLETION_QUERY, "variables": {"request": req}},
                timeout=aiohttp.ClientTimeout(total=600),
            ) as resp:
                body = await resp.json()
            c = ((body.get("data") or {}).get("completion")) or {}
            rows.append(
                {
                    "i": i,
                    "wall_s": round(time.monotonic() - c0, 2),
                    "gen": c.get("tokensGenerated"),
                    "prefill_ms": c.get("prefillMs"),
                    "decode_ms": c.get("decodeMs"),
                    "text": (c.get("text") or ""),
                    "errors": body.get("errors"),
                }
            )
    wall = time.monotonic() - t0
    gen = sum(int(r["gen"] or 0) for r in rows)
    dec_ms = sum(float(r["decode_ms"] or 0) for r in rows)
    return {
        "arm": tag,
        "wall_s": round(wall, 1),
        "calls": calls,
        "generated": gen,
        "decode_tps": round(gen / (dec_ms / 1000), 2) if dec_ms else None,
        "rows": rows,
    }


def wired_mb() -> int:
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if "wired down" in line:
                pages = int(line.split(":")[1].strip().rstrip("."))
                return pages * 16384 // (1024 * 1024)
    except Exception:  # noqa: BLE001 — a telemetry read never fails a probe
        pass
    return -1


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", type=int, default=2, help="PDFs per OCR leg")
    ap.add_argument("--calls", type=int, default=8, help="completions per inf leg")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--out", default="dev/gpu_contention_results.json")
    args = ap.parse_args()

    pdfs = pick_pdfs(args.pdfs)
    scratch = (
        Path(os.environ.get("TMPDIR", "/tmp")) / "ouroboros_contention_probe"
    )  # noqa: S108 — scratch only
    print(f"PDFs : {[p.name for p in pdfs]}")
    print(f"calls: {args.calls} x {args.max_tokens} tok")
    print(f"wired at start: {wired_mb()} MB\n")

    results: dict = {"pdfs": [p.name for p in pdfs], "calls": args.calls}

    # ── A: OCR alone ────────────────────────────────────────────────
    shutil.rmtree(scratch / "A", ignore_errors=True)
    print("arm A — OCR alone ...", flush=True)
    results["A"] = await ocr_leg(pdfs, scratch / "A", "A_ocr_solo")
    print(f"  {results['A']['wall_s']}s  rc={results['A']['rc']}\n", flush=True)

    # ── B: inference alone ──────────────────────────────────────────
    print("arm B — inference alone ...", flush=True)
    results["B"] = await infer_leg(args.calls, args.max_tokens, "B_inf_solo")
    print(
        f"  {results['B']['wall_s']}s  {results['B']['generated']} tok "
        f"@ {results['B']['decode_tps']} tok/s\n",
        flush=True,
    )

    # ── C: both at once ─────────────────────────────────────────────
    shutil.rmtree(scratch / "C", ignore_errors=True)
    print("arm C — both concurrently ...", flush=True)
    c0 = time.monotonic()
    ocr_c, inf_c = await asyncio.gather(
        ocr_leg(pdfs, scratch / "C", "C_ocr"),
        infer_leg(args.calls, args.max_tokens, "C_inf"),
    )
    results["C"] = {
        "wall_s": round(time.monotonic() - c0, 1),
        "ocr": ocr_c,
        "inf": inf_c,
        "wired_after_mb": wired_mb(),
    }

    # ── verdict ─────────────────────────────────────────────────────
    t_a, t_b, t_c = (
        results["A"]["wall_s"],
        results["B"]["wall_s"],
        results["C"]["wall_s"],
    )
    ideal, serial = max(t_a, t_b), t_a + t_b
    # 0.0 = as good as one leg alone; 1.0 = fully serialized.
    pos = (t_c - ideal) / (serial - ideal) if serial > ideal else float("nan")
    results["verdict"] = {
        "t_ocr_solo_s": t_a,
        "t_inf_solo_s": t_b,
        "t_concurrent_s": t_c,
        "ideal_s": ideal,
        "serialized_s": serial,
        "serialization": round(pos, 3),
        "recovered_s": round(serial - t_c, 1),
        "recovered_pct": round((serial - t_c) / serial * 100, 1),
        "ocr_slowdown": round(ocr_c["wall_s"] / t_a, 3) if t_a else None,
        "inf_slowdown": round(inf_c["wall_s"] / t_b, 3) if t_b else None,
        "decode_tps_solo": results["B"]["decode_tps"],
        "decode_tps_concurrent": inf_c["decode_tps"],
    }

    # Fidelity: identical work must produce identical output.
    same_text = [
        (a.get("text") or "") == (b.get("text") or "")
        for a, b in zip(results["B"]["rows"], inf_c["rows"])
    ]
    results["verdict"]["inf_text_identical"] = f"{sum(same_text)}/{len(same_text)}"
    ocr_pairs = list(zip(results["A"]["papers"], ocr_c["papers"]))
    results["verdict"][
        "ocr_chars_identical"
    ] = f"{sum(1 for a, b in ocr_pairs if a['chars'] == b['chars'])}/{len(ocr_pairs)}"
    results["verdict"]["ocr_rates"] = [
        {
            "key": a["key"],
            "solo": [a["numeric"], a["span"]],
            "concurrent": [b["numeric"], b["span"]],
        }
        for a, b in ocr_pairs
    ]

    Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results["verdict"], indent=2))
    print(f"\nfull results -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
