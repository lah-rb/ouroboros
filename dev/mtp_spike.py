#!/usr/bin/env python3
"""MTP draft-verify spike — does Qwopus's nextn layer pay on Metal?

The feasibility probe (2026-07-24) proved the machinery live: the vendored
fork exposes LLAMA_CONTEXT_TYPE_MTP, an MTP-typed context creates over the
shared weights, decodes, and its draft agreed with the main model on a
one-token test. This spike builds the actual loop and measures the only
numbers that matter:

  - draft ACCEPTANCE RATE (accepted drafts / drafted tokens)
  - end-to-end decode tok/s: speculative vs plain baseline, K in {2,3,4}

Greedy sampling throughout so acceptance is well-defined (draft token ==
main argmax). Loop per iteration:
  1. draft K tokens autoregressively with the MTP context (1 nextn layer);
  2. verify all K in ONE main-context batch (logits on every row);
  3. accept the longest matching prefix L, take the main model's row-L
     argmax as the correction token;
  4. roll BOTH contexts' KV back past the divergence (memory_seq_rm) and
     decode the correction into each.

v2 (post-crash): qwen3.6's GDN-HYBRID recurrent layers cannot rewind —
mid-sequence memory_seq_rm is invalid on this arch (llama_decode -1; the
same property that excludes hybrids from the batched engine). The loop
therefore uses STATE CHECKPOINTING instead of KV rollback: snapshot the
main sequence state (llama_state_seq_*) before each verify round;
full-accept rounds continue free, mismatch rounds restore the checkpoint
and decode accepted+correction in one clean batch. The draft context is
never rolled back — the nextn layer is 1/65th of the model, so a full
history re-feed on mismatch is nearly free. Checkpoint overhead is
measured and reported (it is part of the verdict).

Run standalone with the LLMVP server DOWN (22GB weights + two contexts).
Usage: llmvp/.venv/bin/python dev/mtp_spike.py [gen_tokens]
"""

from __future__ import annotations

import sys
import time

import numpy as np

QWOPUS = (
    "/Users/lah-rb/.lmstudio/models/Jackrong/Qwopus3.6-27B-Coder-MTP-GGUF/"
    "Qwopus3.6-27B-Coder-MTP-Q6_K.gguf"
)
GEN_TOKENS = int(sys.argv[1]) if len(sys.argv) > 1 else 256
PROMPTS = [
    b"def binary_search(arr, target):\n",
    b"# A Python class implementing a doubly linked list with insert, delete\nclass DoublyLinkedList:\n",
    b"The three most important considerations when designing a REST API are",
]


def load():
    from llama_cpp import Llama
    import llama_cpp._internals as internals
    import llama_cpp.llama_cpp as C

    t0 = time.time()
    m = Llama(model_path=QWOPUS, n_ctx=8192, n_gpu_layers=-1, verbose=False)
    params = C.llama_context_default_params()
    params.n_ctx = 8192
    params.n_batch = 512
    params.ctx_type = 1  # LLAMA_CONTEXT_TYPE_MTP
    mtp = internals.LlamaContext(model=m._model, params=params, verbose=False)
    print(f"loaded + MTP ctx in {time.time() - t0:.0f}s", flush=True)
    return m, mtp, internals


class Ctx:
    """Tiny driver around one llama context: decode tokens, keep position."""

    def __init__(self, ctx, n_vocab: int, internals):
        self.ctx = ctx
        self.n_vocab = n_vocab
        self.batch = internals.LlamaBatch(
            n_tokens=512, embd=0, n_seq_max=1, verbose=False
        )
        self.pos = 0

    def feed(self, tokens: list[int], want_logits: str = "last"):
        """Decode tokens at the current position. want_logits: 'last'|'all'.
        Returns argmax per logits row (list) for 'all', or [argmax] for
        'last'."""
        self.batch.reset()
        for i, t in enumerate(tokens):
            want = want_logits == "all" or i == len(tokens) - 1
            self.batch.add_token(t, self.pos + i, [0], want)
        ret = self.ctx.decode(self.batch)
        if ret != 0:
            raise RuntimeError(f"decode returned {ret} at pos {self.pos}")
        outs = []
        rows = range(len(tokens)) if want_logits == "all" else [len(tokens) - 1]
        for i in rows:
            lp = self.ctx.get_logits_ith(i)
            arr = np.ctypeslib.as_array(lp, shape=(self.n_vocab,))
            outs.append(int(arr.argmax()))
        self.pos += len(tokens)
        return outs

    def rollback_to(self, pos: int):
        self.ctx.memory_seq_rm(0, pos, -1)
        self.pos = pos

    # ── recurrent-safe state checkpointing (GDN hybrids can't rewind) ──

    def checkpoint(self):
        import ctypes

        import llama_cpp.llama_cpp as C

        size = C.llama_state_seq_get_size(self.ctx.ctx, 0)
        if size <= 0:
            raise RuntimeError(
                "llama_state_seq_get_size returned 0 — "
                "per-seq state unsupported on this arch"
            )
        buf = (ctypes.c_uint8 * size)()
        written = C.llama_state_seq_get_data(self.ctx.ctx, buf, size, 0)
        if written <= 0:
            raise RuntimeError("llama_state_seq_get_data failed")
        return (buf, written, self.pos)

    def restore(self, ck) -> None:
        import llama_cpp.llama_cpp as C

        buf, size, pos = ck
        self.ctx.memory_seq_rm(0, 0, -1)  # whole-seq clear IS hybrid-legal
        ok = C.llama_state_seq_set_data(self.ctx.ctx, buf, size, 0)
        if ok <= 0:
            raise RuntimeError("llama_state_seq_set_data failed")
        self.pos = pos

    def clear(self) -> None:
        self.ctx.memory_seq_rm(0, 0, -1)
        self.pos = 0


def baseline(m, internals, prompt_toks: list[int], n: int) -> tuple[float, list[int]]:
    main = Ctx(m._ctx, m.n_vocab(), internals)
    main.rollback_to(0)
    nxt = main.feed(prompt_toks)[0]
    out = [nxt]
    t0 = time.time()
    while len(out) < n:
        nxt = main.feed([out[-1]])[0]
        out.append(nxt)
    dt = time.time() - t0
    return (n - 1) / dt, out


def speculative(
    m, mtp_ctx, internals, prompt_toks: list[int], n: int, k: int
) -> tuple[float, float, float, list[int]]:
    main = Ctx(m._ctx, m.n_vocab(), internals)
    draft = Ctx(mtp_ctx, m.n_vocab(), internals)
    main.clear()
    draft.clear()
    first = main.feed(prompt_toks)[0]
    draft.feed(prompt_toks)
    out = [first]
    drafted = accepted = 0
    ck_s = 0.0
    t0 = time.time()
    while len(out) < n:
        # 1. draft K autoregressively with the nextn layer
        drafts = []
        cur = out[-1]
        for _ in range(k):
            cur = draft.feed([cur])[0]
            drafts.append(cur)
        drafted += k
        # 2. checkpoint main (hybrids can't rewind), then batch-verify:
        #    row i's argmax is the main model's prediction FOR drafts[i]
        tc = time.time()
        ck = main.checkpoint()
        ck_s += time.time() - tc
        verify_in = [out[-1]] + drafts[:-1]
        preds = main.feed(verify_in, want_logits="all")
        # 3. longest matching prefix
        L = 0
        while L < k and preds[L] == drafts[L]:
            L += 1
        if L == k:
            out.extend(drafts)
            accepted += k
            # both ctxs hold the drafts legitimately — free continue
            continue
        correction = preds[L]
        out.extend(drafts[:L] + [correction])
        accepted += L
        # 4. mismatch: restore main to pre-verify, decode accepted prefix
        #    + correction in ONE batch; draft ctx re-feeds full history
        #    (1/65th-model cost — no rewind needed).
        tc = time.time()
        main.restore(ck)
        ck_s += time.time() - tc
        main.feed([verify_in[0]] + drafts[:L] + [correction], want_logits="last")
        hist = prompt_toks + out[:-1]
        draft.clear()
        draft.feed(hist)
    dt = time.time() - t0
    return (len(out) - 1) / dt, accepted / max(drafted, 1), ck_s, out


def main() -> None:
    m, mtp_ctx, internals = load()
    print(f"n_layer_nextn={m.n_layer_nextn()}  gen={GEN_TOKENS} tok/prompt")
    for pi, prompt in enumerate(PROMPTS):
        toks = m.tokenize(prompt, add_bos=True)
        base_tps, base_out = baseline(m, internals, toks, GEN_TOKENS)
        print(f"\nprompt {pi}: baseline {base_tps:.1f} tok/s")
        for k in (2, 3, 4):
            tps, acc, ck_s, spec_out = speculative(
                m, mtp_ctx, internals, toks, GEN_TOKENS, k
            )
            match = spec_out[: len(base_out)] == base_out
            print(
                f"  K={k}: {tps:.1f} tok/s ({tps / base_tps - 1:+.0%} vs base), "
                f"acceptance {acc:.0%}, checkpoint_overhead={ck_s:.1f}s, "
                f"output-match={match}",
                flush=True,
            )
    print("\ndone")


if __name__ == "__main__":
    main()
