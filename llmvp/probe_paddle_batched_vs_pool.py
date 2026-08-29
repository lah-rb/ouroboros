#!/usr/bin/env python3
"""Head-to-head at SMALL model scale: batched single context vs a
multi-context pool, on paddle-ocr-vl (0.5B Q8).

QUESTION. Every Ouroboros serving experiment to date (30B muse,
gpt-oss-120b) found N streams batched into ONE context beat N private
contexts on aggregate throughput and context availability. Paddle's
production serving has always been multi-context (contexts are cheap at
this scale). Does the batched advantage hold at 0.5B, or does it
require A3B-and-larger models?

PRE-REGISTERED PREDICTION (operator, 2026-08-29): batching should still
win, but the margin may need higher N to appear at this scale.

FINDING #1 (established while building this probe): paddle is M-RoPE,
and the fork's LlamaBatch carries ONE position per token — the
4-section M-RoPE position layout is not expressible, so llama_decode
rejects any hand-rolled multi-seq batch that follows an mtmd image
install ("Invalid input batch"). Production works only because the
handler drives the instance's own single-seq bookkeeping. CONSEQUENCE:
batched-engine serving for paddle would need fork-level M-RoPE batch
support before images could enter shared-context streams at all.

THE MEASURABLE HALF. Image ENCODE work is identical in both
architectures (serialized through one mtmd context in both our
designs), so the architectural question lives in DECODE multiplexing +
context economics. Arms use an identical synthetic prefill (~1,280
tokens — the KV footprint of a typical OCR page) + 700-token greedy
generation:

  pool    — N threads, each its OWN LlamaContext (n_seq_max=1,
            n_ctx=4096) + own batch: production's shape.
  batched — ONE context (kv_unified, n_seq_max=N, n_ctx=4096*N), one
            interleaved decode loop, one row per live stream per step.

Cells run as subprocesses so an OOM abort loses one cell (a pool-arm
OOM is a context-availability finding, not an error).

RUN (3060, production paddle home, quiet machine):
  CUDA_VISIBLE_DEVICES=1 .venv/bin/python probe_paddle_batched_vs_pool.py
Output: probe_out/paddle_batched_vs_pool.jsonl + summary lines.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

MODEL = "/home/lah-rb/models/PaddleOCR-VL-1.6.Q8_0.gguf"
SEED_TEXT = (
    b"Raman spectra of quartz exhibit a dominant band at 464 cm-1 arising "
    b"from symmetric Si-O-Si bending, with weaker features at 128, 206, "
    b"355, 394, 696, 796, 808, 1063 and 1160 cm-1. "
)
PREFILL_TOKENS = 1280
CAP = 700
PER_STREAM_CTX = 4096
N_BATCH = 512
OUT = "probe_out/paddle_batched_vs_pool.jsonl"


def _gpu_mb():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.split("\n")[0].strip())
    except Exception:
        return -1


def _prefill_ids(llama):
    toks = llama.tokenize(SEED_TEXT, True, False)
    out = list(toks)
    while len(out) < PREFILL_TOKENS:
        out.extend(toks[1:])
    return out[:PREFILL_TOKENS]


def run_cell(arm: str, n: int) -> dict:
    import numpy as np
    from llama_cpp import Llama, internals

    llama = Llama(model_path=MODEL, n_ctx=512, n_gpu_layers=-1,
                  n_batch=N_BATCH, verbose=False)
    model = llama._model
    n_vocab = llama.n_vocab()
    is_eog = model.token_is_eog
    ids = _prefill_ids(llama)
    res = {"arm": arm, "n": n}
    base_mb = _gpu_mb()

    def prefill(ctx, batch, seq):
        pos = 0
        i = 0
        last = None
        while i < len(ids):
            take = ids[i:i + N_BATCH]
            batch.reset()
            for j, t in enumerate(take):
                is_last = i + j == len(ids) - 1
                batch.add_token(t, pos, [seq], is_last)
                pos += 1
            assert ctx.decode(batch) == 0
            if i + len(take) >= len(ids):
                last = np.ctypeslib.as_array(
                    ctx.get_logits_ith(len(take) - 1),
                    shape=(n_vocab,)).copy()
            i += len(take)
        return pos, last

    if arm == "pool":
        gen = [0] * n
        errs = []
        peak = {"mb": base_mb}
        lock = threading.Lock()

        def worker(i):
            try:
                params = type(llama.context_params).from_buffer_copy(
                    llama.context_params)
                params.n_ctx = PER_STREAM_CTX
                params.n_seq_max = 1
                ctx = internals.LlamaContext(model=model, params=params)
                batch = internals.LlamaBatch(
                    n_tokens=N_BATCH, embd=0, n_seq_max=1)
                pos, logits = prefill(ctx, batch, 0)
                with lock:
                    peak["mb"] = max(peak["mb"], _gpu_mb())
                for _ in range(CAP):
                    tok = int(np.argmax(logits))
                    if is_eog(tok):
                        break
                    gen[i] += 1
                    batch.reset()
                    batch.add_token(tok, pos, [0], True)
                    assert ctx.decode(batch) == 0
                    pos += 1
                    logits = np.ctypeslib.as_array(
                        ctx.get_logits_ith(0), shape=(n_vocab,)).copy()
            except Exception as e:  # noqa: BLE001
                errs.append(f"s{i}: {type(e).__name__} {e}")

        t0 = time.time()
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.time() - t0
        res.update(wall_s=round(wall, 2), gen_tokens=sum(gen), errors=errs,
                   peak_mb=peak["mb"], base_mb=base_mb)

    else:  # batched
        params = type(llama.context_params).from_buffer_copy(
            llama.context_params)
        params.n_ctx = PER_STREAM_CTX * n
        params.n_seq_max = n
        params.kv_unified = True
        ctx = internals.LlamaContext(model=model, params=params)
        batch = internals.LlamaBatch(
            n_tokens=max(N_BATCH, n), embd=0, n_seq_max=n)
        t0 = time.time()
        pos = [0] * n
        logits = [None] * n
        for s in range(n):
            pos[s], logits[s] = prefill(ctx, batch, s)
        prefill_s = time.time() - t0
        peak_mb = _gpu_mb()
        live = list(range(n))
        gen = [0] * n
        while live:
            batch.reset()
            rows = []
            for s in live:
                tok = int(np.argmax(logits[s]))
                if is_eog(tok) or gen[s] >= CAP:
                    continue
                gen[s] += 1
                batch.add_token(tok, pos[s], [s], True)
                pos[s] += 1
                rows.append(s)
            if not rows:
                break
            assert ctx.decode(batch) == 0
            for r, s in enumerate(rows):
                logits[s] = np.ctypeslib.as_array(
                    ctx.get_logits_ith(r), shape=(n_vocab,)).copy()
            live = [s for s in live if gen[s] < CAP and s in rows]
        wall = time.time() - t0
        res.update(wall_s=round(wall, 2), prefill_s=round(prefill_s, 2),
                   gen_tokens=sum(gen), errors=[], peak_mb=peak_mb,
                   base_mb=base_mb)

    res["agg_tps"] = round(res["gen_tokens"] / max(res["wall_s"], 1e-9), 1)
    return res


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--cell":
        r = run_cell(sys.argv[2], int(sys.argv[3]))
        print("CELL " + json.dumps(r))
        return 0

    os.makedirs("probe_out", exist_ok=True)
    env = dict(os.environ)
    env.setdefault("CUDA_VISIBLE_DEVICES", "1")
    cells = [("batched", n) for n in (1, 2, 4, 8, 16, 24)] + [
        ("pool", n) for n in (1, 2, 4, 8, 16)]
    for arm, n in cells:
        p = subprocess.run(
            [sys.executable, __file__, "--cell", arm, str(n)],
            capture_output=True, text=True, timeout=1800, env=env)
        line = next((ln for ln in p.stdout.splitlines()
                     if ln.startswith("CELL ")), None)
        if line:
            r = json.loads(line[5:])
        else:
            r = {"arm": arm, "n": n, "crashed": True, "rc": p.returncode,
                 "tail": (p.stderr or p.stdout)[-300:]}
        with open(OUT, "a") as fh:
            fh.write(json.dumps(r) + "\n")
        print(f"{arm:8s} N={n:<3} " + (
            f"wall {r['wall_s']:>7.1f}s  agg {r['agg_tps']:>7.1f} tok/s  "
            f"peak {r.get('peak_mb', -1)} MB  errs={len(r.get('errors') or [])}"
            if not r.get("crashed") else f"CRASHED rc={r['rc']}"), flush=True)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
