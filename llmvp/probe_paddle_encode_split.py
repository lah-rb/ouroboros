#!/usr/bin/env python3
"""Which vision stage carries paddle's constant per-request cost?

CONTEXT. The serving floor is ~89-91 ms per crop request (blank 32x32
included), flat in image area below crop sizes, flat in n_ctx, and the
recheck disproved the causal-attn suspect (paddle is use_non_causal=False,
so the mtmd-helper bracket is a no-op). Remaining candidates: a
constant-cost projector forward at the minimum patch grid, the image-embd
decode, or Python serving code. This probe times the C stages in
ISOLATION — no server, no handler — so whatever it cannot account for is
Python's.

STAGES per image: bitmap init (PNG decode) / mtmd_tokenize / encode
(clip+projector forward) / image-embd decode into a fresh context /
40-token text-only generation baseline for the same context (decode
reference at this shape).

PRE-REGISTERED PREDICTION (2026-08-29): encode of a blank 32x32 is
NOT ~2 ms — it pads to a minimum patch grid and costs tens of ms, and
this constant is the biggest single piece of the serving floor. If
instead encode is ~2 ms, the floor is Python serving code and py-spy
inherits the question.

RUN (3060, quiet):
  CUDA_VISIBLE_DEVICES=1 llmvp/.venv/bin/python llmvp/probe_paddle_encode_split.py
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import statistics
import time

MODEL = "/home/lah-rb/models/PaddleOCR-VL-1.6.Q8_0.gguf"
MMPROJ = "/home/lah-rb/models/PaddleOCR-VL-1.6-GGUF-mmproj.gguf"
SCRATCH = (
    "/tmp/claude-1000/-home-lah-rb-Repos-ouroboros/"
    "89c0814e-2bf4-43de-a225-ceaa353d642d/scratchpad/ocr_probe"
)
OUT = "probe_out/paddle_encode_split.jsonl"
REPEATS = 10


def main() -> int:
    import numpy as np
    from PIL import Image
    from llama_cpp import Llama, internals
    from llama_cpp import mtmd_cpp as M

    os.makedirs("probe_out", exist_ok=True)

    llama = Llama(model_path=MODEL, n_ctx=8192, n_gpu_layers=-1,
                  n_batch=512, verbose=False)
    model = llama._model
    n_vocab = llama.n_vocab()
    n_embd_inp = int(M.llama_model_n_embd_inp(model.model)) if hasattr(
        M, "llama_model_n_embd_inp") else 0

    params = M.mtmd_context_params_default()
    params.use_gpu = True
    params.print_timings = False
    params.n_threads = 4
    params.warmup = True
    mctx = M.mtmd_init_from_file(MMPROJ.encode(), model.model, params)
    assert mctx, "mtmd init failed"
    marker = M.mtmd_default_marker().decode()
    print(f"mrope={bool(M.mtmd_decode_use_mrope(mctx))}")

    # images: blank32, real crop, real page
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (255, 255, 255)).save(buf, format="PNG")
    man = json.load(open(os.path.join(SCRATCH, "manifest.json")))
    page_p = man["pages"][0]["path"]
    im = Image.open(page_p)
    line = im.crop((int(im.width * 0.1), int(im.height * 0.35),
                    int(im.width * 0.1) + 880, int(im.height * 0.35) + 32))
    lb = io.BytesIO()
    line.save(lb, format="PNG")
    arms = [
        ("blank32", buf.getvalue(), 32 * 32),
        ("crop", lb.getvalue(), 880 * 32),
        ("page", open(page_p, "rb").read(), im.width * im.height),
    ]

    prompt = f"{marker}Transcribe all text in this image as plain markdown."
    sink = open(OUT, "w")
    print(f"\n{'arm':8s} {'ntok':>5s} | {'bitmap':>7s} {'token.':>7s} "
          f"{'encode':>7s} {'embd-dec':>8s} | {'sum':>7s}  (median ms)")
    print("-" * 66)

    for name, data, area in arms:
        t_bm, t_tok, t_enc, t_dec = [], [], [], []
        ntok = 0
        for _ in range(REPEATS):
            # fresh context per repeat so embd decode always lands at pos 0
            cparams = type(llama.context_params).from_buffer_copy(
                llama.context_params)
            cparams.n_ctx = 8192
            cparams.n_seq_max = 1
            ctx = internals.LlamaContext(model=model, params=cparams)

            t0 = time.time()
            cbuf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            w = M.mtmd_helper_bitmap_init_from_buf(mctx, cbuf, len(data), False)
            assert w.bitmap
            t1 = time.time()

            chunks = M.mtmd_input_chunks_init()
            it = M.mtmd_input_text()
            enc = prompt.encode()
            it.text = ctypes.c_char_p(enc)
            it.text_len = len(enc)
            it.add_special = True
            it.parse_special = True
            arr = (M.mtmd_bitmap_p_ctypes * 1)(w.bitmap)
            rc = M.mtmd_tokenize(mctx, chunks, ctypes.byref(it), arr, 1)
            assert rc == 0, f"tokenize rc={rc}"
            t2 = time.time()

            img_chunk = None
            n = int(M.mtmd_input_chunks_size(chunks))
            for i in range(n):
                ch = M.mtmd_input_chunks_get(chunks, i)
                if int(M.mtmd_input_chunk_get_type(ch)) == 1:
                    img_chunk = ch
                    ntok = int(M.mtmd_input_chunk_get_n_tokens(ch))
            assert img_chunk is not None

            rc = M.mtmd_encode_chunk(mctx, img_chunk)
            assert rc == 0, f"encode rc={rc}"
            embd = M.mtmd_get_output_embd(mctx)
            t3 = time.time()

            new_past = ctypes.c_int32(0)
            rc = M.mtmd_helper_decode_image_chunk(
                mctx, ctx.ctx, img_chunk, embd, 0, 0, 512,
                ctypes.byref(new_past),
                ctypes.cast(None, M.mtmd_helper_post_decode_callback), None)
            assert rc == 0, f"embd decode rc={rc}"
            t4 = time.time()

            t_bm.append((t1 - t0) * 1e3)
            t_tok.append((t2 - t1) * 1e3)
            t_enc.append((t3 - t2) * 1e3)
            t_dec.append((t4 - t3) * 1e3)
            M.mtmd_input_chunks_free(chunks)
            M.mtmd_bitmap_free(w.bitmap)
            ctx.close()

        med = [statistics.median(x) for x in (t_bm, t_tok, t_enc, t_dec)]
        row = {"arm": name, "area": area, "img_tokens": ntok,
               "bitmap_ms": round(med[0], 2), "tokenize_ms": round(med[1], 2),
               "encode_ms": round(med[2], 2), "embd_decode_ms": round(med[3], 2)}
        sink.write(json.dumps(row) + "\n")
        print(f"{name:8s} {ntok:>5d} | {med[0]:>7.2f} {med[1]:>7.2f} "
              f"{med[2]:>7.2f} {med[3]:>8.2f} | {sum(med):>7.2f}")

    # text-only 40-token generation reference in a fresh context
    cparams = type(llama.context_params).from_buffer_copy(llama.context_params)
    cparams.n_ctx = 8192
    cparams.n_seq_max = 1
    ctx = internals.LlamaContext(model=model, params=cparams)
    batch = internals.LlamaBatch(n_tokens=512, embd=0, n_seq_max=1)
    ids = llama.tokenize(b"Transcribe the following. " * 12, True, False)[:150]
    walls = []
    for _ in range(6):
        ctx.memory_seq_rm(0, 0, -1)
        t0 = time.time()
        batch.reset()
        for j, t in enumerate(ids):
            batch.add_token(t, j, [0], j == len(ids) - 1)
        assert ctx.decode(batch) == 0
        pos = len(ids)
        logits = np.ctypeslib.as_array(
            ctx.get_logits_ith(len(ids) - 1), shape=(n_vocab,)).copy()
        for _ in range(40):
            tok = int(np.argmax(logits))
            batch.reset()
            batch.add_token(tok, pos, [0], True)
            assert ctx.decode(batch) == 0
            pos += 1
            logits = np.ctypeslib.as_array(
                ctx.get_logits_ith(0), shape=(n_vocab,)).copy()
        walls.append((time.time() - t0) * 1e3)
    print(f"\ntext-only 150+40 reference: {statistics.median(walls):.1f} ms")
    sink.write(json.dumps(
        {"arm": "text_ref", "wall_ms": round(statistics.median(walls), 1)}) + "\n")
    sink.close()
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
