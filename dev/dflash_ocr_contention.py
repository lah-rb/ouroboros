#!/usr/bin/env python3
"""Does drafting survive when the OCR lane wants the same card?

`dflash_x_swarm.py` measured drafting at +25% with 8 seats, but with the 3060
doing nothing else. The pipeline does not run that way: OCR lives on the 3060
continuously, and the draft has to live there too (same-card placement fails in
llama.cpp's memory fitting). So the two configurations actually available are

  A  3060 dedicated to OCR        text = 8 seats, no draft
  B  3060 shared, draft + OCR     text = 8 seats + dflash

and the question is which gives more TOTAL system throughput, not which gives
the faster text lane. Both sides are therefore measured in both arms: text
aggregate tok/s WHILE OCR is in flight, and the OCR wall clock itself.

The swarm bench predicts B degrades the OCR lane: two tenants on one CUDA
device serialize completely (S = 1.043, aggregate 0.98x — worse than taking
turns). This checks whether any of the +25% survives that, and what it costs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

_PROMPTS = [
    "Explain how a KV cache changes the cost of autoregressive decode.",
    "Describe why batching raises aggregate throughput but lowers per-seat.",
    "Summarise the trade-off speculative decoding makes.",
    "What bounds the number of sequences an accelerator can serve well?",
    "Explain prefill versus decode and why they scale differently.",
    "Describe how quantisation changes the bandwidth picture.",
    "Explain why acceptance rate governs speculative decoding's payoff.",
    "What makes a draft model a good match for its target?",
]
_LLAMA_BIN = "/home/lah-rb/Repos/llama.cpp/build/bin"


def wait_ready(port: int, timeout: float = 900.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=3
            ) as fh:
                if fh.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    return False


def _one(port: int, prompt: str, n_predict: int) -> int:
    body = json.dumps({"prompt": prompt, "n_predict": n_predict,
                       "temperature": 0.0, "top_k": 1, "cache_prompt": False,
                       "ignore_eos": True}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/completion", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as fh:
        d = json.load(fh)
    return int((d.get("timings") or {}).get("predicted_n") or 0)


def text_round(port: int, seats: int, n_predict: int) -> tuple:
    prompts = [_PROMPTS[i % len(_PROMPTS)] for i in range(seats)]
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=seats) as pool:
        toks = sum(pool.map(lambda p: _one(port, p, n_predict), prompts))
    return toks, time.time() - t0


def run_arm(model, draft, pdf, key, seats, n_predict, port, tag) -> dict:
    cmd = [f"{_LLAMA_BIN}/llama-server", "-m", model, "--port", str(port),
           "--host", "127.0.0.1", "-ngl", "99", "-c", str(1024 * seats),
           "--parallel", str(seats), "--no-webui"]
    if draft:
        cmd += ["-md", draft, "-ngld", "99", "--spec-type", "draft-dflash",
                "--spec-draft-n-max", "8", "--spec-draft-device", "CUDA1"]
    tlog = f"/home/lah-rb/tmp/cont_{tag}_text.log"
    with open(tlog, "w") as lf:
        srv = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
    ocr = None
    try:
        if not wait_ready(port):
            return {"error": "text server never answered 200"}
        _one(port, _PROMPTS[0], 16)  # warm

        # Text alone, so the contention delta has its own control inside the
        # same arm rather than across arms.
        toks, wall = text_round(port, seats, n_predict)
        solo_text = round(toks / wall, 1)

        # OCR lane on the 3060, started and left running.
        outdir = f"/home/lah-rb/tmp/cont_{tag}_ocr"
        subprocess.run(["rm", "-rf", outdir], check=False)
        os.makedirs(f"{outdir}/databank", exist_ok=True)
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = "1"
        env["PATH"] = f"{_LLAMA_BIN}:{env.get('PATH','')}"
        env["TMPDIR"] = "/home/lah-rb/tmp"
        olog = open(f"/home/lah-rb/tmp/cont_{tag}_ocr.log", "w")
        t_ocr0 = time.time()
        ocr = subprocess.Popen(
            ["tools/pdf_extract/.venv/bin/python", "tools/pdf_extract/extract_batch.py",
             "--databank-dir", f"{outdir}/databank", "--vl-backend", "llamacpp",
             "--model", "/home/lah-rb/models/PaddleOCR-VL-1.6.Q8_0.gguf",
             "--mmproj", "/home/lah-rb/models/PaddleOCR-VL-1.6-GGUF-mmproj.gguf",
             "--pdfs", pdf, "--keys", key],
            stdout=subprocess.PIPE, stderr=olog, env=env, text=True,
            cwd="/home/lah-rb/Repos/ouroboros")

        # Text rounds WHILE OCR is in flight. Rounds keep running until OCR
        # finishes, and only whole rounds that started before it ended count.
        rounds, ttok, twall = 0, 0, 0.0
        while ocr.poll() is None:
            tk, wl = text_round(port, seats, n_predict)
            ttok += tk
            twall += wl
            rounds += 1
        ocr_out, _ = ocr.communicate()
        ocr_wall = time.time() - t_ocr0
        ocr = None

        rep = {}
        for line in (ocr_out or "").splitlines():
            try:
                rep = json.loads(line)
            except Exception:  # noqa: BLE001
                continue

        text_under_load = round(ttok / twall, 1) if twall else None
        acc = None
        m = re.findall(
            r"draft acceptance = [0-9.]+ \(\s*(\d+) accepted /\s*(\d+) generated\)",
            open(tlog, errors="replace").read())
        if m:
            acc = round(int(m[-1][0]) / int(m[-1][1]), 3) if int(m[-1][1]) else None

        return {
            "arm": tag,
            "draft": bool(draft),
            "text_solo_tok_s": solo_text,
            "text_under_ocr_tok_s": text_under_load,
            "text_cost_pct": (round((text_under_load / solo_text - 1) * 100, 1)
                              if solo_text and text_under_load else None),
            "rounds_during_ocr": rounds,
            "ocr_wall_s": round(ocr_wall, 1),
            "ocr_tool_seconds": rep.get("seconds"),
            "ocr_pages": rep.get("pages"),
            "ocr_numeric": rep.get("numeric_match_rate"),
            "acceptance": acc,
        }
    finally:
        if ocr is not None and ocr.poll() is None:
            ocr.terminate()
        srv.terminate()
        try:
            srv.wait(timeout=30)
        except subprocess.TimeoutExpired:
            srv.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model",
                    default="/home/lah-rb/models/muse-glimmer-30B-kquant-dynamic.gguf")
    ap.add_argument("--draft", default="/home/lah-rb/models/dflash-kquant.gguf")
    ap.add_argument("--pdf",
                    default="/home/lah-rb/corpora/ouroboros-curator-pilot/pdfs/doi_10.1002_jrs.921.pdf")
    ap.add_argument("--seats", type=int, default=8)
    ap.add_argument("--n-predict", type=int, default=128)
    args = ap.parse_args()

    rows = []
    for tag, draft in (("A_ocr_only", None), ("B_draft_plus_ocr", args.draft)):
        r = run_arm(args.model, draft, args.pdf, f"cont_{tag}", args.seats,
                    args.n_predict, 8200 + len(rows), tag)
        rows.append(r)
        print(json.dumps(r), flush=True)
    print("\n" + json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
