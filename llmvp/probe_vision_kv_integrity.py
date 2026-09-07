#!/usr/bin/env python3
"""P0b — the batched-vision go/no-go: does a media chunk decoded onto seq B
leave seq A byte-identical, using the SPLIT mtmd API outside the handler?

QUESTION. The batched-vision plan installs an image onto a working seat's
seq via mtmd_encode_chunk -> mtmd_get_output_embd -> a copied buffer ->
mtmd_helper_decode_image_chunk(seq_id=B) while other seqs hold live KV.
The handler hardcodes seq 0 and clears the whole context; the C API takes
an explicit seq_id — but nothing has ever proven that an image decode on
one seq leaves a neighbour's KV untouched, or that the split pipeline
produces a working multimodal prefix at all outside the handler.

PRE-REGISTERED PREDICTIONS (write verdicts next to these, don't edit):
  P1: seq A's temp-0 continuation is byte-identical before/after a full
      install on seq B (arm 2) and with interleaved install (arm 3).
  P2: new_n_past == old_n_past + chunk_n_tokens for muse (no M-RoPE
      position/cell divergence; if false, the plan's R3 cell-debt
      machinery is load-bearing).
  P3: the image-chunk decode (the atomic control-op stall) takes < 1.5 s
      for a typical corpus figure at n_batch 512.
  P4: seq B, continued past the install, produces a sane figure
      description (proves render->tokenize->encode->decode->generate).

ARMS.
  1  golden: greedy 128-tok continuation on seq A, seq B empty.
  2  full install on seq B (text1 + image chunk), then regenerate A.
  3  interleaved: A decode steps alternating with B install pieces
     (mimics control-op timing between engine steps), regenerate A.
  B  continue seq B through text2 and greedy-describe the figure.

RUN (GPU must be free — carve a mission window):
  cd llmvp && .venv/bin/python probe_vision_kv_integrity.py
Knobs: PROBE_MODEL, PROBE_MMPROJ, PROBE_IMAGE, PROBE_N_CTX, PROBE_GEN.
Output: probe_out/vision_kv_integrity.jsonl + summary. Exit 0 = go.
"""

from __future__ import annotations

import ctypes
import json
import os
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
N_CTX = int(os.environ.get("PROBE_N_CTX", "16384"))
N_GEN = int(os.environ.get("PROBE_GEN", "128"))
N_BATCH = 512

SEQ_A, SEQ_B = 0, 1

PROMPT_A = os.environ.get("PROBE_PROMPT_A", "The mineral quartz is composed of")
# Hand-built muse-marker prompt for the probe; P1 does the faithful render.
# PROBE_PROMPT_B re-targets the probe at another family's template (the
# 2026-08-29 paddle run passes the PaddleOCR pool template verbatim).
PROMPT_B = os.environ.get(
    "PROBE_PROMPT_B",
    "<|start|>user<|message|>Look carefully at this figure. <__media__> "
    "Describe every panel, axis and label.<|eot|>"
    "<|start|>assistant to=user<|message|>",
)
# PROBE_JOINT_STEP=1 adds arm 4: ONE llama_decode carrying a token row for
# BOTH seqs after the install — the batched engine's _step shape. This is
# the exact call probe_paddle_batched_vs_pool.py reported rejected
# ("Invalid input batch") when its own bookkeeping advanced positions by
# token count; with position authority from new_n_past the M-RoPE batch
# validation (llama-batch.cpp: batch pos strictly above the seq's KV
# pos_max) should pass. Off by default so the recorded muse verdicts stay
# what they were.
JOINT_STEP = os.environ.get("PROBE_JOINT_STEP", "") == "1"


def _log(msg):
    print(msg, flush=True)


def main() -> int:
    from llama_cpp import Llama, internals
    from llama_cpp import mtmd_cpp as M

    t0 = time.time()
    _log(f"loading {os.path.basename(MODEL)} …")
    llama = Llama(
        model_path=MODEL,
        n_ctx=512,
        n_gpu_layers=-1,
        n_batch=N_BATCH,
        verbose=False,
    )
    params = type(llama.context_params).from_buffer_copy(llama.context_params)
    params.n_ctx = N_CTX
    params.n_seq_max = 2
    ctx = internals.LlamaContext(model=llama._model, params=params)
    batch = internals.LlamaBatch(n_tokens=N_BATCH, embd=0, n_seq_max=2)
    n_vocab = llama.n_vocab()
    _log(
        f"model + probe ctx ready in {time.time()-t0:.1f}s "
        f"(n_ctx={N_CTX}, n_seq_max=2)"
    )

    def decode_tokens(seq, tokens, start_pos, want_logits_last=True):
        """Decode `tokens` onto `seq` in N_BATCH chunks; return last logits row."""
        pos = start_pos
        i = 0
        last = None
        while i < len(tokens):
            take = tokens[i : i + N_BATCH]
            batch.reset()
            for j, tok in enumerate(take):
                is_last = want_logits_last and (i + j == len(tokens) - 1)
                batch.add_token(tok, pos, [seq], is_last)
                pos += 1
            rc = ctx.decode(batch)
            if rc != 0:
                raise RuntimeError(f"decode rc={rc} at pos {pos}")
            i += len(take)
            if want_logits_last and i >= len(tokens):
                last = np.ctypeslib.as_array(
                    ctx.get_logits_ith(len(take) - 1), shape=(n_vocab,)
                ).copy()
        return pos, last

    def greedy(seq, start_pos, first_logits, n):
        toks, pos, logits = [], start_pos, first_logits
        for _ in range(n):
            tok = int(np.argmax(logits))
            toks.append(tok)
            batch.reset()
            batch.add_token(tok, pos, [seq], True)
            rc = ctx.decode(batch)
            if rc != 0:
                raise RuntimeError(f"decode rc={rc} in greedy at pos {pos}")
            pos += 1
            logits = np.ctypeslib.as_array(
                ctx.get_logits_ith(0), shape=(n_vocab,)
            ).copy()
        return toks, pos

    tok_a = llama.tokenize(PROMPT_A.encode(), add_bos=True, special=False)

    def run_a():
        ctx.memory_seq_rm(SEQ_A, 0, -1)
        pos, logits = decode_tokens(SEQ_A, tok_a, 0)
        toks, _ = greedy(SEQ_A, pos, logits, N_GEN)
        return toks

    # ── arm 1: golden ────────────────────────────────────────────────
    golden = run_a()
    _log(f"arm1 golden: {N_GEN} toks, tail={golden[-6:]}")

    # ── mtmd setup ───────────────────────────────────────────────────
    mp = M.mtmd_context_params_default()
    mp.use_gpu = True
    mp.print_timings = False
    mp.warmup = True
    mctx = M.mtmd_init_from_file(MMPROJ.encode(), llama._model.model, mp)
    assert mctx, "mtmd_init_from_file failed"
    n_embd_inp = llama._model.n_embd_inp()

    img = open(IMAGE, "rb").read()
    buf = (ctypes.c_uint8 * len(img)).from_buffer_copy(img)
    wrapper = M.mtmd_helper_bitmap_init_from_buf(mctx, buf, len(img), False)
    assert wrapper.bitmap, "bitmap init failed"

    def tokenize_b():
        chunks = M.mtmd_input_chunks_init()
        it = M.mtmd_input_text()
        enc = PROMPT_B.encode()
        it.text = ctypes.c_char_p(enc)
        it.text_len = len(enc)
        it.add_special = True  # seq B starts empty — per-seq semantics
        it.parse_special = True
        arr = (M.mtmd_bitmap_p_ctypes * 1)(wrapper.bitmap)
        rc = M.mtmd_tokenize(mctx, chunks, ctypes.byref(it), arr, 1)
        assert rc == 0, f"mtmd_tokenize rc={rc}"
        return chunks

    def chunk_list(chunks):
        out = []
        for i in range(int(M.mtmd_input_chunks_size(chunks))):
            ch = M.mtmd_input_chunks_get(chunks, i)
            out.append((int(M.mtmd_input_chunk_get_type(ch)), ch))
        return out

    def text_tokens(ch):
        n = ctypes.c_size_t(0)
        p = M.mtmd_input_chunk_get_tokens_text(ch, ctypes.byref(n))
        return [int(p[k]) for k in range(int(n.value))]

    def install_b(interleave_a=None):
        """Install text1+image on seq B. Returns (stats, pos_after, last_logits)."""
        ctx.memory_seq_rm(SEQ_B, 0, -1)
        chunks = chunk_list(tokenize_b())
        stats = {
            "text1": 0,
            "img_tokens": 0,
            "img_decode_s": None,
            "encode_s": None,
            "old_n_past": None,
            "new_n_past": None,
        }
        pos = 0
        last = None
        text_segments_after_image = []
        seen_image = False
        for ctype_, ch in chunks:
            if ctype_ == 0:  # text
                toks = text_tokens(ch)
                if not seen_image:
                    stats["text1"] += len(toks)
                    pos, last = decode_tokens(SEQ_B, toks, pos)
                else:
                    text_segments_after_image.append(toks)
                if interleave_a:
                    interleave_a()
            elif ctype_ == 1:  # image
                seen_image = True
                nt = int(M.mtmd_input_chunk_get_n_tokens(ch))
                stats["img_tokens"] = nt
                te = time.time()
                rc = M.mtmd_encode_chunk(mctx, ch)
                assert rc == 0, f"encode rc={rc}"
                src = M.mtmd_get_output_embd(mctx)
                embd = np.ctypeslib.as_array(
                    src, shape=(nt * n_embd_inp,)
                ).copy()  # COPY out of mtmd scratch
                stats["encode_s"] = round(time.time() - te, 3)
                if interleave_a:
                    interleave_a()
                stats["old_n_past"] = pos
                newp = ctypes.c_int32(0)
                td = time.time()
                rc = M.mtmd_helper_decode_image_chunk(
                    mctx,
                    ctx.ctx,
                    ch,
                    embd.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                    pos,
                    SEQ_B,
                    N_BATCH,
                    ctypes.byref(newp),
                    ctypes.cast(None, M.mtmd_helper_post_decode_callback),
                    None,
                )
                stats["img_decode_s"] = round(time.time() - td, 3)
                assert rc == 0, f"decode_image_chunk rc={rc}"
                stats["new_n_past"] = int(newp.value)
                pos = int(newp.value)
                if interleave_a:
                    interleave_a()
        return stats, pos, last, text_segments_after_image

    # ── arm 2: full install, then regenerate A ───────────────────────
    stats2, pos_b, _, tail_segs = install_b()
    a2 = run_a()
    same2 = a2 == golden
    _log(f"arm2: install {stats2} | A identical: {same2}")

    # ── arm 3: interleaved install ───────────────────────────────────
    # Rebuild A's prefix, then alternate A greedy steps with install pieces.
    ctx.memory_seq_rm(SEQ_A, 0, -1)
    a_pos, a_logits = decode_tokens(SEQ_A, tok_a, 0)
    a3_toks = []
    a_state = {"pos": a_pos, "logits": a_logits}

    def step_a():
        tok = int(np.argmax(a_state["logits"]))
        a3_toks.append(tok)
        batch.reset()
        batch.add_token(tok, a_state["pos"], [SEQ_A], True)
        assert ctx.decode(batch) == 0
        a_state["pos"] += 1
        a_state["logits"] = np.ctypeslib.as_array(
            ctx.get_logits_ith(0), shape=(n_vocab,)
        ).copy()

    stats3, pos_b3, _, tail_segs3 = install_b(interleave_a=step_a)
    while len(a3_toks) < N_GEN:
        step_a()
    same3 = a3_toks == golden
    _log(f"arm3: interleaved install | A identical: {same3}")

    # ── seq B describe (uses the arm-3 install, still resident) ──────
    tail = [t for seg in tail_segs3 for t in seg]
    if tail:
        pos_b3, b_logits = decode_tokens(SEQ_B, tail, pos_b3)
    else:
        batch.reset()
        # no text2 (template ended at generation head inside text1) —
        # refresh logits by re-decoding nothing is impossible; use last
        # install logits via a 1-token nudge is not clean; instead require
        # text2 in the prompt shape above (it ends with the gen head, so
        # tail is expected non-empty only when the marker is mid-prompt).
        b_logits = None
    desc = ""
    if b_logits is not None:
        b_toks, pos_b3 = greedy(SEQ_B, pos_b3, b_logits, 200)
        desc = llama.detokenize(b_toks).decode("utf-8", errors="replace")
    _log("\n=== seq B description (greedy 200) ===\n" + desc[:800])

    # ── arm 4 (opt-in): the batched engine's step shape ──────────────
    # One decode call, one token row per LIVE seq, after the install.
    joint_ok = None
    if JOINT_STEP and b_logits is not None:
        a4_pos = a_state["pos"]
        a4_logits = a_state["logits"]
        b4_pos = pos_b3
        b4_logits = np.ctypeslib.as_array(
            ctx.get_logits_ith(0), shape=(n_vocab,)
        ).copy()
        joint_ok = True
        for _ in range(4):
            batch.reset()
            batch.add_token(int(np.argmax(a4_logits)), a4_pos, [SEQ_A], True)
            batch.add_token(int(np.argmax(b4_logits)), b4_pos, [SEQ_B], True)
            rc = ctx.decode(batch)
            if rc != 0:
                joint_ok = False
                _log(
                    f"arm4: JOINT multi-seq decode rejected rc={rc} "
                    f"(A pos {a4_pos}, B pos {b4_pos})"
                )
                break
            a4_pos += 1
            b4_pos += 1
            a4_logits = np.ctypeslib.as_array(
                ctx.get_logits_ith(0), shape=(n_vocab,)
            ).copy()
            b4_logits = np.ctypeslib.as_array(
                ctx.get_logits_ith(1), shape=(n_vocab,)
            ).copy()
        if joint_ok:
            _log(
                "arm4: 4 joint multi-seq steps decoded clean (rc=0) — the "
                "batched _step shape is LEGAL after this install"
            )

    row = {
        "golden_tail": golden[-8:],
        "arm2_identical": same2,
        "arm3_identical": same3,
        "install_arm2": stats2,
        "install_arm3": stats3,
        "desc_head": desc[:200],
        "n_embd_inp": n_embd_inp,
        "pos_delta_eq_tokens": (
            stats2["new_n_past"] == stats2["old_n_past"] + stats2["img_tokens"]
        ),
        "joint_step_ok": joint_ok,
        "model": os.path.basename(MODEL),
    }
    os.makedirs("probe_out", exist_ok=True)
    with open("probe_out/vision_kv_integrity.jsonl", "a") as fh:
        fh.write(json.dumps(row) + "\n")

    ok = same2 and same3 and bool(desc.strip())
    if joint_ok is False:
        ok = False
    _log("\n=== VERDICT ===")
    if joint_ok is not None:
        _log(f"  P5 joint multi-seq step legal  : {joint_ok}")
    _log(f"  P1 neighbour KV byte-identical : {same2 and same3}")
    _log(
        f"  P2 pos_delta == img_tokens     : {row['pos_delta_eq_tokens']} "
        f"(old={stats2['old_n_past']} +{stats2['img_tokens']} -> "
        f"new={stats2['new_n_past']})"
    )
    _log(
        f"  P3 image decode stall          : {stats2['img_decode_s']}s "
        f"(encode {stats2['encode_s']}s)"
    )
    _log(f"  P4 description non-empty       : {bool(desc.strip())}")
    _log(f"  GO / NO-GO                     : {'GO' if ok else 'NO-GO'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
