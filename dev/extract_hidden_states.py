#!/usr/bin/env python3
"""Hidden-state probe, stage 1 — backfill gpt-oss hidden-state vectors for the
trusted training turns (dev/train_dataset_trusted_v1.jsonl).

For each turn context: prefill-only through gpt-oss-120b F16 (no decode), extract
the last-layer hidden state as TWO variants — the final-token vector ("query
embedding", ThinkSwitcher-style) and the mean over the final batch chunk of up to
n_batch tokens ("tail-mean"; = full-prompt mean for the median ~1k-token context).

Deploy-time rationale: at routing time the turn context is already prefilled in the
session KV, so this vector is ~free in production; only the TRAINING corpus needs
this one-off backfill pass. Run with the LLMVP server STOPPED (two 120Bs don't fit).

Resume-safe: skips uids already present in the output. -> dev/hidden_states_v1.npz
"""
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, "llmvp")  # use the pinned fork's llama_cpp via llmvp's venv
import llama_cpp
from llama_cpp import Llama

MODEL = "/Users/lah-rb/.lmstudio/models/unsloth/gpt-oss-120b-GGUF/gpt-oss-120b-F16.gguf"
N_CTX = 16384          # max trusted context ~10k tokens -> no truncation in practice
N_BATCH = 2048
OUT = "dev/hidden_states_v1.npz"
SAVE_EVERY = 100


def load_done():
    if not os.path.exists(OUT):
        return {}, []
    z = np.load(OUT, allow_pickle=True)
    uids = list(z["uids"])
    rows = [
        {"uid": u, "last": z["last_tok"][i], "mean": z["tail_mean"][i],
         "n_tok": int(z["n_tokens"][i])}
        for i, u in enumerate(uids)
    ]
    return {r["uid"] for r in rows}, rows


def save(rows):
    np.savez_compressed(
        OUT,
        uids=np.array([r["uid"] for r in rows]),
        last_tok=np.stack([r["last"] for r in rows]),
        tail_mean=np.stack([r["mean"] for r in rows]),
        n_tokens=np.array([r["n_tok"] for r in rows]),
    )


def main():
    data = [json.loads(l) for l in open("dev/train_dataset_trusted_v1.jsonl")]
    done, rows = load_done()
    todo = [r for r in data if r["uid"] not in done]
    print(f"{len(data)} turns, {len(done)} already extracted, {len(todo)} to do", flush=True)
    if not todo:
        return

    llm = Llama(
        model_path=MODEL, n_ctx=N_CTX, n_gpu_layers=-1, flash_attn_type=-1,
        swa_full=True, kv_unified=True, n_threads=15, n_batch=N_BATCH,
        verbose=False,
    )
    ctx = llm._ctx.ctx
    llama_cpp.llama_set_embeddings(ctx, True)
    n_embd = llama_cpp.llama_model_n_embd(llm._model.model)
    mem = llama_cpp.llama_get_memory(ctx) if hasattr(llama_cpp, "llama_get_memory") else None
    batch = llama_cpp.llama_batch_init(N_BATCH, 0, 1)
    print(f"model loaded; n_embd={n_embd}", flush=True)

    def clear_kv():
        if mem is not None:
            llama_cpp.llama_memory_clear(mem, True)
        else:  # older API fallback
            llama_cpp.llama_kv_cache_clear(ctx)

    def extract(text):
        toks = llm.tokenize(text.encode("utf-8", errors="replace"), add_bos=True, special=False)
        if len(toks) > N_CTX - 8:
            toks = toks[-(N_CTX - 8):]  # keep the TAIL (recent history + menu)
        clear_kv()
        pos = 0
        while pos < len(toks):
            chunk = toks[pos:pos + N_BATCH]
            final = pos + len(chunk) >= len(toks)
            batch.n_tokens = len(chunk)
            for j, t in enumerate(chunk):
                batch.token[j] = t
                batch.pos[j] = pos + j
                batch.n_seq_id[j] = 1
                batch.seq_id[j][0] = 0
                batch.logits[j] = 1 if final else 0  # embeddings for every final-chunk token
            if llama_cpp.llama_decode(ctx, batch) != 0:
                raise RuntimeError("llama_decode failed")
            pos += len(chunk)
        n_out = len(chunk)
        vecs = np.empty((n_out, n_embd), dtype=np.float32)
        for i in range(n_out):
            ptr = llama_cpp.llama_get_embeddings_ith(ctx, i)
            vecs[i] = np.ctypeslib.as_array(ptr, shape=(n_embd,))
        return vecs[-1].copy(), vecs.mean(0), len(toks)

    t0 = time.time()
    tok_total = 0
    for k, r in enumerate(todo):
        last, mean, n_tok = extract(r["text"])
        rows.append({"uid": r["uid"], "last": last, "mean": mean, "n_tok": n_tok})
        tok_total += n_tok
        if k == 2:  # sanity gate after 3 extractions
            V = np.stack([x["last"] for x in rows[-3:]])
            norms = np.linalg.norm(V, axis=1)
            cos01 = float(V[0] @ V[1] / (norms[0] * norms[1] + 1e-9))
            print(f"SANITY: norms={norms.round(2).tolist()} cos(v0,v1)={cos01:.3f}", flush=True)
            if norms.min() < 1e-3 or cos01 > 0.9999:
                raise RuntimeError("degenerate vectors — aborting before the long run")
        if (k + 1) % SAVE_EVERY == 0 or k + 1 == len(todo):
            save(rows)
            rate = tok_total / max(1e-9, time.time() - t0)
            eta = sum(len(x["text"]) for x in todo[k + 1:]) / 3.6 / max(1.0, rate) / 60
            print(f"{k+1}/{len(todo)} saved | {rate:.0f} tok/s | ~{eta:.0f} min left", flush=True)
    print(f"DONE: {len(rows)} vectors -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
