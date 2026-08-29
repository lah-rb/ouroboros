#!/usr/bin/env python3
"""Does batched multi-stream scaling hold at OCR REQUEST SHAPE?

WHY THIS CELL WAS MISSING. The 2026-08-29 head-to-head
(probe_paddle_batched_vs_pool.py) proved batched single-context beats a
multi-context pool at 0.5B — but at a figtext-like shape (1,280-token
prefill + 700-token generation). The OCR Phase 0 verdict then declared the
batched path dead using a serving-path stage split. The recheck of that
split found the fit-set contaminated (pooled across heterogeneous
segments: intercept 250 ms predicting 308 ms where the clean cell observes
148 ms) and the decode share evaluated at the wrong median (fit-set 16-24
tok vs production 43 tok). Corrected, decode is ~35-43% of a production
crop request — the MARGINAL band of the pre-registered rules, whose
condition ("pool also plateaus") P0b satisfied. So the architectural
question is live again, and it needs the one cell nobody ran: the two
architectures at the REAL request shape.

SHAPE. One OCR crop request ~= 150-token prefill + ~40-token generation +
a per-request KV reset (production clears the context between crops).
Requests repeat per stream, so per-request overhead — reset, re-prefill,
scheduling — is IN the measurement instead of amortized away by a single
700-token generation.

ARMS.
  pool    — N threads, each its OWN context (n_ctx 4096, n_seq_max 1):
            production's architecture. Each request: seq_rm, prefill,
            40 greedy tokens.
  batched — ONE context (kv_unified, n_seq_max=N), one interleaved decode
            loop; a stream finishing its request does seq_rm + re-prefill
            inline (which stalls the shared loop for that one call — this
            UNDERSTATES batched vs a real continuous-batching engine that
            merges prefill chunks into decode steps; the bias runs against
            the doctrine, so a positive result is robust).

Greedy, EOG ignored: fixed 40-token requests so every cell does identical
work. Streams share the seed text; identical token streams are fine for
throughput.

METRIC. Aggregate REQUESTS/S (not tok/s — requests are what OCR pays
for), plus per-request wall. The pool N=1 cell doubles as the ENGINE-side
per-request floor at this shape: serving wall (~148-190 ms measured conc-1
in production) minus this number is what the serving software layer adds
per request, and the width-flat sweep already proved that layer does not
parallelize.

PRE-REGISTERED PREDICTIONS (Fable, 2026-08-29, before any cell ran):
  1. batched aggregate >= 2x pool aggregate by N=8 at this shape — the
     standing "batched beats pools" result holds once the serving floor
     is out of the frame.
  2. pool plateaus <= 1.4x its own N=1, mirroring production width-flat.
  3. engine-side per-request (pool N=1) ~= 90-100 ms, i.e. roughly HALF
     of the observed ~190 ms serving wall — the other half is serving
     software, and it is the single biggest lever in the whole lane.

RUN (3060, quiet machine, server DOWN):
  CUDA_VISIBLE_DEVICES=1 llmvp/.venv/bin/python llmvp/probe_ocr_shape_scaling.py
Output: probe_out/ocr_shape_scaling.jsonl + summary table.
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
PREFILL_TOKENS = 150
CAP = 40
REQUESTS_PER_STREAM = 12
PER_STREAM_CTX = 4096
N_BATCH = 512
OUT = "probe_out/ocr_shape_scaling.jsonl"


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
    ids = _prefill_ids(llama)
    res = {"arm": arm, "n": n}

    def prefill(ctx, batch, seq):
        """One-shot 150-token prefill (fits a single n_batch)."""
        batch.reset()
        for j, t in enumerate(ids):
            batch.add_token(t, j, [seq], j == len(ids) - 1)
        assert ctx.decode(batch) == 0
        return len(ids), np.ctypeslib.as_array(
            ctx.get_logits_ith(len(ids) - 1), shape=(n_vocab,)).copy()

    req_walls: list[float] = []
    walls_lock = threading.Lock()

    if arm == "pool":
        errs: list[str] = []

        def worker(i):
            try:
                params = type(llama.context_params).from_buffer_copy(
                    llama.context_params)
                params.n_ctx = PER_STREAM_CTX
                params.n_seq_max = 1
                ctx = internals.LlamaContext(model=model, params=params)
                batch = internals.LlamaBatch(
                    n_tokens=N_BATCH, embd=0, n_seq_max=1)
                for _ in range(REQUESTS_PER_STREAM):
                    r0 = time.time()
                    ctx.memory_seq_rm(0, 0, -1)
                    pos, logits = prefill(ctx, batch, 0)
                    for _ in range(CAP):
                        tok = int(np.argmax(logits))
                        batch.reset()
                        batch.add_token(tok, pos, [0], True)
                        assert ctx.decode(batch) == 0
                        pos += 1
                        logits = np.ctypeslib.as_array(
                            ctx.get_logits_ith(0), shape=(n_vocab,)).copy()
                    with walls_lock:
                        req_walls.append((time.time() - r0) * 1000.0)
            except Exception as e:  # noqa: BLE001
                errs.append(f"s{i}: {type(e).__name__} {e}")

        t0 = time.time()
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = time.time() - t0
        res.update(errors=errs)

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
        logits: list = [None] * n
        gen = [0] * n
        done_reqs = [0] * n
        req_t0 = [t0] * n
        for s in range(n):
            pos[s], logits[s] = prefill(ctx, batch, s)

        while True:
            live = [s for s in range(n) if done_reqs[s] < REQUESTS_PER_STREAM]
            if not live:
                break
            # streams that just completed a request: bank it, reset, re-prefill
            for s in live:
                if gen[s] >= CAP:
                    req_walls.append((time.time() - req_t0[s]) * 1000.0)
                    done_reqs[s] += 1
                    gen[s] = 0
                    if done_reqs[s] >= REQUESTS_PER_STREAM:
                        continue
                    req_t0[s] = time.time()
                    ctx.memory_seq_rm(s, 0, -1)
                    pos[s], logits[s] = prefill(ctx, batch, s)
            rows = []
            batch.reset()
            for s in range(n):
                if done_reqs[s] >= REQUESTS_PER_STREAM or gen[s] >= CAP:
                    continue
                tok = int(np.argmax(logits[s]))
                gen[s] += 1
                batch.add_token(tok, pos[s], [s], True)
                pos[s] += 1
                rows.append(s)
            if not rows:
                continue
            assert ctx.decode(batch) == 0
            for r, s in enumerate(rows):
                logits[s] = np.ctypeslib.as_array(
                    ctx.get_logits_ith(r), shape=(n_vocab,)).copy()
        wall = time.time() - t0
        res.update(errors=[])

    total_reqs = len(req_walls)
    req_walls.sort()
    res.update(
        wall_s=round(wall, 2),
        requests=total_reqs,
        req_per_s=round(total_reqs / max(wall, 1e-9), 2),
        req_ms_p50=round(req_walls[total_reqs // 2], 1) if total_reqs else -1,
        req_ms_p90=round(req_walls[9 * total_reqs // 10], 1) if total_reqs else -1,
    )
    return res


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--cell":
        r = run_cell(sys.argv[2], int(sys.argv[3]))
        print("CELL " + json.dumps(r))
        return 0

    os.makedirs("probe_out", exist_ok=True)
    env = dict(os.environ)
    env.setdefault("CUDA_VISIBLE_DEVICES", "1")
    cells = [("batched", n) for n in (1, 2, 4, 8, 16)] + [
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
            f"wall {r['wall_s']:>6.1f}s  {r['req_per_s']:>6.2f} req/s  "
            f"req p50 {r['req_ms_p50']:>7.1f} ms  p90 {r['req_ms_p90']:>7.1f}"
            f"  errs={len(r.get('errors') or [])}"
            if not r.get("crashed") else f"CRASHED rc={r['rc']}"), flush=True)
    print("wrote", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
