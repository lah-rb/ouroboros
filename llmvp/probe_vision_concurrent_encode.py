#!/usr/bin/env python3
"""P0a — can a CLIP/mtmd image ENCODE run on a worker thread beside live
multi-seq text decode without corrupting it or taxing it?

QUESTION. The batched-vision plan runs mtmd_encode_chunk OFF the decode
thread (only the short embedding-decode goes through a control op). The
mtmd context is documented not-thread-safe against ITSELF, but nothing
says whether an encode (touching only the clip/mtmd context) can overlap
llama_decode on the text context safely.

PRE-REGISTERED PREDICTIONS (write verdicts next to these, don't edit):
  P1: text tokens are byte-identical with and without a concurrent
      encode loop (no corruption).
  P2: text decode tps degrades < 10% vs the model-free control arm
      (encode work lands on the GPU beside decode; some contention is
      expected, corruption is not).

ARMS.
  1  control: two seqs greedy-decode N tokens interleaved (the batched-
     decode shape), record tokens + wall.
  2  same + a worker thread looping mtmd_encode_chunk on a fixture
     figure under a lock.
  3  model-free control: same + a worker thread looping numpy matmuls
     (isolates GIL/CPU effects from mtmd effects).

RUN (GPU free — carve a mission window):
  cd llmvp && .venv/bin/python probe_vision_concurrent_encode.py
Knobs: PROBE_MODEL, PROBE_MMPROJ, PROBE_IMAGE, PROBE_GEN.
Output: probe_out/vision_concurrent_encode.jsonl. Exit 0 = pass.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time

import numpy as np

MODEL = os.environ.get(
    "PROBE_MODEL", "/home/lah-rb/models/muse-glimmer-30B-kquant-dynamic.gguf"
)
MMPROJ = os.environ.get("PROBE_MMPROJ", "/home/lah-rb/models/mmproj-kquant.gguf")
IMAGE = os.environ.get(
    "PROBE_IMAGE",
    "/home/lah-rb/corpora/ouroboros-spectra/databank/figures/"
    "title_détecteur_multibandes_libs_à_base_de_réseaux_horlographiques_épais"
    "__conception_o/fig_33.png",
)
N_GEN = int(os.environ.get("PROBE_GEN", "192"))
N_BATCH = 512


def main() -> int:
    from llama_cpp import Llama, internals
    from llama_cpp import mtmd_cpp as M

    llama = Llama(
        model_path=MODEL,
        n_ctx=512,
        n_gpu_layers=-1,
        n_batch=N_BATCH,
        verbose=False,
    )
    params = type(llama.context_params).from_buffer_copy(llama.context_params)
    params.n_ctx = 8192
    params.n_seq_max = 2
    ctx = internals.LlamaContext(model=llama._model, params=params)
    batch = internals.LlamaBatch(n_tokens=N_BATCH, embd=0, n_seq_max=2)
    n_vocab = llama.n_vocab()

    prompts = [
        llama.tokenize(b"The mineral quartz is composed of", True, False),
        llama.tokenize(b"Raman spectroscopy measures", True, False),
    ]

    def run_pair():
        """Interleaved two-seq greedy decode, one joint batch per step —
        the batched engine's decode shape."""
        for s in (0, 1):
            ctx.memory_seq_rm(s, 0, -1)
        pos = [0, 0]
        logits = [None, None]
        for s in (0, 1):
            batch.reset()
            for j, tok in enumerate(prompts[s]):
                batch.add_token(tok, j, [s], j == len(prompts[s]) - 1)
            assert ctx.decode(batch) == 0
            pos[s] = len(prompts[s])
            logits[s] = np.ctypeslib.as_array(
                ctx.get_logits_ith(len(prompts[s]) - 1), shape=(n_vocab,)
            ).copy()
        out = [[], []]
        t0 = time.time()
        for _ in range(N_GEN):
            batch.reset()
            nxt = []
            for s in (0, 1):
                tok = int(np.argmax(logits[s]))
                out[s].append(tok)
                batch.add_token(tok, pos[s], [s], True)
                pos[s] += 1
                nxt.append(tok)
            assert ctx.decode(batch) == 0
            for row, s in enumerate((0, 1)):
                logits[s] = np.ctypeslib.as_array(
                    ctx.get_logits_ith(row), shape=(n_vocab,)
                ).copy()
        wall = time.time() - t0
        return out, wall, (2 * N_GEN) / wall

    # mtmd setup for arm 2
    dev = os.environ.get("PROBE_PROJECTOR_DEVICE", "CUDA1")
    if dev:
        os.environ["MTMD_BACKEND_DEVICE"] = dev  # projector off the decode card
    mp = M.mtmd_context_params_default()
    mp.n_threads = int(os.environ.get("PROBE_MTMD_THREADS", "2"))
    mp.use_gpu = True
    mp.print_timings = False
    mp.warmup = True
    mctx = M.mtmd_init_from_file(MMPROJ.encode(), llama._model.model, mp)
    assert mctx, "mtmd_init_from_file failed"
    img = open(IMAGE, "rb").read()
    buf = (ctypes.c_uint8 * len(img)).from_buffer_copy(img)
    wrapper = M.mtmd_helper_bitmap_init_from_buf(mctx, buf, len(img), False)
    assert wrapper.bitmap
    chunks = M.mtmd_input_chunks_init()
    it = M.mtmd_input_text()
    enc = b"look: <__media__> done"
    it.text = ctypes.c_char_p(enc)
    it.text_len = len(enc)
    it.add_special = False
    it.parse_special = True
    arr = (M.mtmd_bitmap_p_ctypes * 1)(wrapper.bitmap)
    assert M.mtmd_tokenize(mctx, chunks, ctypes.byref(it), arr, 1) == 0
    img_chunk = None
    for i in range(int(M.mtmd_input_chunks_size(chunks))):
        ch = M.mtmd_input_chunks_get(chunks, i)
        if int(M.mtmd_input_chunk_get_type(ch)) == 1:
            img_chunk = ch
    assert img_chunk is not None

    enc_lock = threading.Lock()
    stop = threading.Event()
    counters = {"encodes": 0, "matmuls": 0}

    def encode_loop():
        while not stop.is_set():
            with enc_lock:
                rc = M.mtmd_encode_chunk(mctx, img_chunk)
                assert rc == 0
                counters["encodes"] += 1

    def matmul_loop():
        a = np.random.default_rng(0).standard_normal((512, 512))
        while not stop.is_set():
            a = a @ a * 1e-3
            counters["matmuls"] += 1

    results = {}
    for arm, worker in (
        ("control", None),
        ("encode", encode_loop),
        ("matmul", matmul_loop),
    ):
        stop.clear()
        th = None
        if worker:
            th = threading.Thread(target=worker, daemon=True)
            th.start()
        toks, wall, tps = run_pair()
        if th:
            stop.set()
            th.join(timeout=30)
        results[arm] = {"tokens": toks, "wall_s": round(wall, 2), "tps": round(tps, 1)}
        print(
            f"arm {arm:8s}: {wall:6.2f}s  {tps:6.1f} tok/s  "
            f"(encodes={counters['encodes']})",
            flush=True,
        )

    identical = results["encode"]["tokens"] == results["control"]["tokens"]
    tax = 1 - results["encode"]["tps"] / max(results["matmul"]["tps"], 1e-9)
    row = {
        "identical": identical,
        "tax_vs_matmul": round(tax, 3),
        "encodes_done": counters["encodes"],
        **{
            k: {kk: vv for kk, vv in v.items() if kk != "tokens"}
            for k, v in results.items()
        },
    }
    os.makedirs("probe_out", exist_ok=True)
    with open("probe_out/vision_concurrent_encode.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")

    print("\n=== VERDICT ===")
    print(f"  P1 tokens byte-identical under concurrent encode: {identical}")
    print(f"  P2 decode tax vs model-free control: {tax*100:.1f}% (<10% passes)")
    ok = identical and tax < 0.10
    print(f"  {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
