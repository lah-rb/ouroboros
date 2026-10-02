#!/usr/bin/env python3
"""DFlash speculative decoding through LLMVP's OWN binding -- smoke test (2026-10-02).

The llama-server bench (dev/bench_spec_decode.py) measured muse + its DFlash
drafter on llama.cpp 7e4c0a9. LLMVP runs the JamePeng llama-cpp-python fork
(0.3.46, vendoring llama.cpp dd1ea5243), whose C++ speculative loop
(common/speculative.cpp) is unreachable from Python. This proves, before any
engine work, that the fork's library + bindings can run one DFlash cycle end
to end, and that it matches the bench:

  load muse (tensor split, as production) + the drafter in a context linked to
  muse's through llama_context_params.ctx_other; turn on extraction of muse's
  layer inputs at the drafter's target layers; prefill a production pack
  prompt, after every target decode running the drafter's ENCODE over those
  features and INJECTING the result into the drafter's KV (process());
  then N tokens greedily, plain and speculative (draft [id_last, mask x n] in
  one non-causal pass, drop the mask block, verify on muse, accept the
  matching prefix, inject the verify rows, roll both caches back past the
  first rejection) -- the same order as llama-server's loop.

Run with LLMVP's venv:
    llmvp/.venv/bin/python dev/spec_dflash_smoke.py [--split tensor|layer|none] [--n 256] [--n-max 3]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import time

import numpy as np

import llama_cpp as L

MUSE = os.path.expanduser("~/models/muse-glimmer-30B-kquant-dynamic.gguf")
DRAFT = os.path.expanduser("~/models/dflash-kquant.gguf")
PROMPTS = os.path.expanduser("~/tmp/spec_bench/prompts.json")
SPLITS = {"none": 0, "layer": 1, "tensor": 3}
N_CTX, N_BATCH, N_UBATCH = 16384, 2048, 512


def model_params(split: str, keep: list):
    mp = L.llama_model_default_params()
    mp.n_gpu_layers = 999
    mp.split_mode = SPLITS[split]
    mp.main_gpu = 1 if split == "none" else 0
    if split != "none":
        ts = (ctypes.c_float * 16)(*([0.5, 0.5] + [0.0] * 14))
        keep.append(ts)
        mp.tensor_split = ts
    return mp


def ctx_params(other=None):
    cp = L.llama_context_default_params()
    cp.n_ctx, cp.n_batch, cp.n_ubatch, cp.n_seq_max = N_CTX, N_BATCH, N_UBATCH, 1
    cp.flash_attn_type = 1
    cp.swa_full = True  # rollback and forks mid-window, as LLMVP runs muse
    if other is not None:
        cp.n_rs_seq = 0
        cp.ctx_other = other
    return cp


class Batch:
    """A llama_batch with numpy views over its arrays."""

    def __init__(self, n: int, embd: int = 0):
        self.b = L.llama_batch_init(n, embd, 1)
        self.n, self.embd = n, embd

    def fill_tokens(self, toks, pos0, logits_all=False, logits_last=False):
        for i, t in enumerate(toks):
            self.b.token[i] = t
            self.b.pos[i] = pos0 + i
            self.b.n_seq_id[i] = 1
            self.b.seq_id[i][0] = 0
            self.b.logits[i] = (
                1 if logits_all or (logits_last and i == len(toks) - 1) else 0
            )
        self.b.n_tokens = len(toks)

    def fill_embd(self, rows: np.ndarray, positions):
        n, d = rows.shape
        dst = np.ctypeslib.as_array(self.b.embd, shape=(self.n * self.embd,))
        dst[: n * d] = rows.reshape(-1)
        for i, p in enumerate(positions):
            self.b.pos[i] = p
            self.b.n_seq_id[i] = 1
            self.b.seq_id[i][0] = 0
            self.b.logits[i] = 0
        self.b.n_tokens = n


class Spec:
    def __init__(self, split: str, n_max: int):
        L.llama_backend_init()
        self.keep: list = []
        t0 = time.time()
        self.m_tgt = L.llama_model_load_from_file(
            MUSE.encode(), model_params(split, self.keep)
        )
        self.c_tgt = L.llama_init_from_model(self.m_tgt, ctx_params())
        # SAME placement as the target: the drafter borrows the target's
        # output.weight through ctx_other, and under tensor split that tensor
        # lives in the meta backend's buffer -- a drafter on one plain GPU
        # aborts ("pre-allocated tensor (output.weight) in a buffer (Meta())
        # that cannot run the operation"). llama-server inherits the split.
        self.m_dft = L.llama_model_load_from_file(
            DRAFT.encode(), model_params(split, self.keep)
        )
        self.c_dft = L.llama_init_from_model(self.m_dft, ctx_params(other=self.c_tgt))
        assert self.c_tgt and self.c_dft, "context creation failed"
        print(f"loaded target + drafter in {time.time() - t0:.0f}s", flush=True)

        self.vocab = L.llama_model_get_vocab(self.m_tgt)
        self.n_vocab = L.llama_vocab_n_tokens(self.vocab)
        n = L.llama_model_target_layer_ids_n(self.m_dft)
        ids = L.llama_model_target_layer_ids(self.m_dft)
        self.layers = [ids[i] for i in range(n)]
        self.n_embd_tgt = L.llama_model_n_embd(self.m_tgt)
        self.n_embd_dec = L.llama_model_n_embd(self.m_dft)
        self.mask = L.llama_vocab_mask(L.llama_model_get_vocab(self.m_dft))
        self.n_max = n_max
        print(f"target layers {self.layers}, n_embd tgt {self.n_embd_tgt} dec {self.n_embd_dec}, "
              f"mask {self.mask}, vocab {self.n_vocab}", flush=True)  # fmt: skip
        for lid in self.layers:
            L.llama_set_embeddings_layer_inp(self.c_tgt, lid, True)
        L.llama_set_embeddings_nextn(self.c_dft, True, True)
        L.llama_set_causal_attn(self.c_dft, False)
        self.bt = Batch(N_BATCH)
        self.bd = Batch(N_BATCH)
        self.bi = Batch(N_BATCH, self.n_embd_dec)

    # ── primitives ────────────────────────────────────────────────

    def tokenize(self, text: str) -> list[int]:
        raw = text.encode("utf-8")
        buf = (L.llama_token * (len(raw) + 16))()
        n = L.llama_tokenize(self.vocab, raw, len(raw), buf, len(buf), True, True)
        assert n > 0, n
        return list(buf[:n])

    def logits(self, ctx, i) -> np.ndarray:
        return np.ctypeslib.as_array(
            L.llama_get_logits_ith(ctx, i), shape=(self.n_vocab,)
        )

    def rm(self, ctx, p0: int) -> None:
        L.llama_memory_seq_rm(L.llama_get_memory(ctx), 0, p0, -1)

    def process(self, n_rows: int, pos0: int) -> None:
        """Target features of the last decode -> drafter encode -> KV inject."""
        feats = np.empty((n_rows, len(self.layers) * self.n_embd_tgt), dtype=np.float32)
        for k, lid in enumerate(self.layers):
            ptr = L.llama_get_embeddings_layer_inp(self.c_tgt, lid)
            assert ptr, f"layer {lid} input not extracted"
            feats[:, k * self.n_embd_tgt : (k + 1) * self.n_embd_tgt] = (
                np.ctypeslib.as_array(ptr, shape=(n_rows, self.n_embd_tgt))
            )
        for off in range(0, n_rows, N_UBATCH):
            chunk = np.ascontiguousarray(feats[off : off + N_UBATCH])
            enc = L.llama_batch()
            enc.n_tokens = chunk.shape[0]
            enc.embd = chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            rc = L.llama_encode(self.c_dft, enc)
            assert rc == 0, f"llama_encode rc={rc}"
            g = np.ctypeslib.as_array(
                L.llama_get_embeddings_nextn(self.c_dft),
                shape=(chunk.shape[0], self.n_embd_dec),
            ).copy()
            self.bi.fill_embd(g, range(pos0 + off, pos0 + off + chunk.shape[0]))
            rc = L.llama_decode(self.c_dft, self.bi.b)
            assert rc == 0, f"inject decode rc={rc}"

    def prefill(self, toks: list[int], spec: bool) -> int:
        self.rm(self.c_tgt, 0)
        self.rm(self.c_dft, 0)
        for off in range(0, len(toks), N_BATCH):
            chunk = toks[off : off + N_BATCH]
            last = off + len(chunk) == len(toks)
            self.bt.fill_tokens(chunk, off, logits_last=last)
            assert L.llama_decode(self.c_tgt, self.bt.b) == 0
            if spec:
                self.process(len(chunk), off)
        return int(np.argmax(self.logits(self.c_tgt, self.bt.b.n_tokens - 1)))

    # ── loops ─────────────────────────────────────────────────────

    def plain(self, toks, n):
        tok = self.prefill(toks, spec=False)
        out, pos, t0 = [tok], len(toks), time.time()
        while len(out) < n:
            self.bt.fill_tokens([tok], pos, logits_all=True)
            assert L.llama_decode(self.c_tgt, self.bt.b) == 0
            tok = int(np.argmax(self.logits(self.c_tgt, 0)))
            out.append(tok)
            pos += 1
        return out[:n], (n - 1) / (time.time() - t0)

    def speculative(self, toks, n):
        tok = self.prefill(toks, spec=True)
        out, n_past, t0 = [tok], len(toks), time.time()
        drafted = accepted = steps = 0
        while len(out) < n:
            # draft: [id_last, mask x n_max] in one non-causal pass
            block = [tok] + [self.mask] * self.n_max
            self.bd.fill_tokens(block, n_past, logits_all=True)
            assert L.llama_decode(self.c_dft, self.bd.b) == 0
            draft = [
                int(np.argmax(self.logits(self.c_dft, i))) for i in range(1, len(block))
            ]
            self.rm(self.c_dft, n_past)  # the mask block is not history
            # verify on the target
            self.bt.fill_tokens([tok] + draft, n_past, logits_all=True)
            assert L.llama_decode(self.c_tgt, self.bt.b) == 0
            k = 0
            while k < len(draft):
                t = int(np.argmax(self.logits(self.c_tgt, k)))
                if t != draft[k]:
                    break
                k += 1
            bonus = int(np.argmax(self.logits(self.c_tgt, k)))
            self.process(len(draft) + 1, n_past)
            self.rm(self.c_tgt, n_past + 1 + k)
            self.rm(self.c_dft, n_past + 1 + k)
            out.extend(draft[:k] + [bonus])
            n_past += 1 + k
            tok = bonus
            drafted += len(draft)
            accepted += k
            steps += 1
        return out[:n], (n - 1) / (time.time() - t0), drafted, accepted, steps


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=tuple(SPLITS), default="tensor")
    ap.add_argument("--n", type=int, default=256)
    ap.add_argument("--n-max", type=int, default=3)
    a = ap.parse_args()
    prompts = json.load(open(PROMPTS))
    s = Spec(a.split, a.n_max)
    results = []
    for pr in [p for p in prompts if p["workload"] in ("pack", "translate")]:
        toks = s.tokenize(pr["prompt"])
        ref, tps_plain = s.plain(toks, a.n)
        got, tps_spec, drafted, acc, steps = s.speculative(toks, a.n)
        same = next((i for i, (x, y) in enumerate(zip(ref, got)) if x != y), len(ref))
        r = {"id": pr["id"], "prompt_tokens": len(toks), "plain_tps": round(tps_plain, 1),
             "spec_tps": round(tps_spec, 1), "speedup": round(tps_spec / tps_plain, 2),
             "accept_rate": round(acc / drafted, 3) if drafted else 0,
             "tokens_per_step": round(len(got) / steps, 2) if steps else 0,
             "identical_prefix_tokens": same, "n": a.n}  # fmt: skip
        results.append(r)
        print(json.dumps(r), flush=True)
    out = os.path.expanduser(f"~/tmp/spec_bench/smoke_{a.split}.json")
    json.dump(results, open(out, "w"), indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
