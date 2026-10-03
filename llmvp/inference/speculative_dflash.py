"""DFlash speculative decoding for the batched engine (2026-10-02).

WHY. Muse ships a DFlash drafter (a 2.6B block-diffusion head that reads the
target's hidden states at a few layers and proposes a block of tokens in ONE
non-causal pass). Benched on the two-3090 rig: tensor split decodes 59 tok/s
per stream plain and 92-111 with the drafter (1.56-1.80x), pack quality
unchanged (dev/bench_spec_decode.py, dev/spec_dflash_smoke.py). llama.cpp's
own loop lives in common/speculative.cpp, which no binding reaches; everything
it CALLS is bound by the fork (llama_set/get_embeddings_layer_inp,
llama_get_embeddings_nextn, llama_model_target_layer_ids, ctx_other ...), so
the loop is reimplemented here, on the engine's own context.

HOW IT STAYS IN STEP WITH THE TARGET. The drafter keeps a KV cache that must
mirror the target's, row for row: every target position needs the drafter's
encoding of that position's features. Rather than hook every call site, the
drafter ATTACHES to the target LlamaContext instance:

  * decode  -- after every successful target decode (generation steps, prefill
    chunks, head pinning through Llama.eval, vision embedding installs), the
    batch's layer-input features are encoded by the drafter and INJECTED into
    its KV at the same positions and seqs (llama-server's process()).
  * memory_seq_rm / cp / add / div / keep, memory_clear -- mirrored verbatim on
    the drafter's cache, so forks, head splices, purges, window shifts and
    snapshot pins can never drift, including ones added later.

A write the hook cannot see (the mtmd helper decodes straight into the raw
context for non-causal / M-RoPE models) leaves that seq UNCOVERED: draft()
is only offered for a seq whose drafter KV ends exactly where the target's
does (covered()), so such a stream simply decodes plainly. Correctness never
depends on the drafter -- the target verifies every drafted token; a bad
draft only costs speed. Any drafter failure disables speculation for the
process (the target engine is never touched by it).

PLACEMENT TRAP. The drafter borrows the target's output.weight through
ctx_other; under tensor split that tensor lives in the meta backend's buffer,
so the drafter MUST be loaded with the target's placement (split mode, tensor
split, main gpu) -- a drafter on one plain GPU aborts in graph_reserve.
"""

from __future__ import annotations

import ctypes
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

#: ggml_type ids for the drafter's KV cache.
CACHE_TYPES = {"f32": 0, "f16": 1, "q8_0": 8, "q4_0": 2, "bf16": 30}

#: Drafter failures tolerated before speculation is switched off for the process.
MAX_FAILURES = 8

#: The drafter context's n_batch / n_ubatch (see create_ctx).
DRAFT_BATCH = 256
#: Full-size SWA cache for the drafter (see create_ctx); False = windowed.
DRAFT_SWA_FULL = False

_MIRRORED = (
    "memory_seq_rm",
    "memory_seq_cp",
    "memory_seq_add",
    "memory_seq_div",
    "memory_seq_keep",
    "memory_clear",
)


def covered_rows(rows: Sequence[Tuple[int, List[int]]], pos_max: Any) -> List[int]:
    """Indices of the batch rows the drafter can inject: each row must extend
    its seq(s) by exactly one position -- the first row of a seq right after
    the drafter's KV end (pos_max(seq) + 1), each later row right after the
    previous one. A row of an UNCOVERED seq (a write the hook never saw) is
    skipped, and so is every later row of that seq in the batch."""
    keep: List[int] = []
    expect: Dict[int, int] = {}
    broken: set = set()
    for i, (pos, seqs) in enumerate(rows):
        ok = True
        for sq in seqs:
            if sq in broken:
                ok = False
                continue
            want = expect.get(sq)
            if want is None:
                want = int(pos_max(sq)) + 1
            if pos != want:
                ok = False
        if ok:
            keep.append(i)
            for sq in seqs:
                expect[sq] = pos + 1
        else:
            broken.update(seqs)
    return keep


class DFlashDrafter:
    """The drafter model + its context, attached to one target context."""

    def __init__(
        self,
        primary: Any,
        draft_path: str,
        *,
        n_max: int = 3,
        cache_type: str = "q8_0",
    ):
        import llama_cpp
        from llama_cpp import _internals as internals

        self._lc = llama_cpp
        self._internals = internals
        self.primary = primary
        self.draft_path = str(draft_path)
        self.enabled = True
        self.disabled_reason = ""
        self.cache_type = str(cache_type)
        if self.cache_type not in CACHE_TYPES:
            raise ValueError(
                f"speculative_draft_cache_type {cache_type!r} not in {sorted(CACHE_TYPES)}"
            )

        # SAME placement as the target (see the module docstring's trap).
        src = primary.model_params
        mp = llama_cpp.llama_model_default_params()
        mp.n_gpu_layers = src.n_gpu_layers
        mp.split_mode = src.split_mode
        mp.main_gpu = src.main_gpu
        mp.tensor_split = src.tensor_split
        mp.devices = src.devices
        t0 = time.perf_counter()
        self.model = internals.LlamaModel(
            path_model=self.draft_path, params=mp, verbose=False
        )
        m = self.model.model
        n = llama_cpp.llama_model_target_layer_ids_n(m)
        ids = llama_cpp.llama_model_target_layer_ids(m)
        if n <= 0 or not ids:
            raise RuntimeError(f"{draft_path}: not a DFlash drafter (no target layers)")
        self.layers: List[int] = [int(ids[i]) for i in range(n)]
        self.n_embd_tgt = int(llama_cpp.llama_model_n_embd(primary._model.model))
        self.n_embd_dec = int(llama_cpp.llama_model_n_embd(m))
        self.n_embd_enc = len(self.layers) * self.n_embd_tgt
        vocab = llama_cpp.llama_model_get_vocab(m)
        self.mask = int(llama_cpp.llama_vocab_mask(vocab))
        self.n_vocab = int(llama_cpp.llama_vocab_n_tokens(vocab))
        block = 16
        buf = ctypes.create_string_buffer(32)
        if llama_cpp.llama_model_meta_val_str(m, b"dflash.block_size", buf, 32) >= 0:
            try:
                block = int(buf.value.decode())
            except ValueError:
                pass
        # DFlash denoises [id_last, mask * (block-1)] in place: at most block-1.
        self.n_max = max(1, min(int(n_max), block - 1))

        self.ctx: Any = None
        self.target: Any = None
        self._originals: Dict[str, Any] = {}
        self._feat: Optional[np.ndarray] = None
        # stats (health)
        self.h_drafted = 0
        self.h_accepted = 0
        self.h_steps = 0
        self.h_uncovered = 0
        self.h_injected_rows = 0
        self.h_skipped_rows = 0
        self.h_failures = 0
        # Per stream KIND (the request's persona: "default", "vision", ...):
        # [drafted, accepted, verify steps, accepted-length histogram 0..n_max].
        # The pooled rate cannot say whether a vision answer drafts as well
        # as prose (2026-10-03, vision work entering the pipeline).
        self.h_by_kind: Dict[str, list] = {}
        self.create_ctx(primary._ctx)
        logger.info(
            "🚀 DFlash drafter loaded in %.1fs: %s (target layers %s, n_max %d, "
            "block %d, KV %s)",
            time.perf_counter() - t0,
            self.draft_path,
            self.layers,
            self.n_max,
            block,
            self.cache_type,
        )

    # ── context lifecycle ─────────────────────────────────────────

    def create_ctx(self, target_ctx: Any) -> None:
        """Build the drafter context linked to ``target_ctx`` and attach."""
        lc = self._lc
        cp = lc.llama_context_params.from_buffer_copy(self.primary.context_params)
        cp.ctx_other = target_ctx.ctx
        cp.n_rs_seq = 0
        cp.type_k = cp.type_v = CACHE_TYPES[self.cache_type]
        cp.embeddings = False
        # SMALL BATCHES. Copied from the target, the drafter reserved compute
        # and logit buffers for 2,048-row batches (1.6 GB of compute alone),
        # which left 0.9 GB free per 3090 at boot. It never needs that: a
        # draft is <= streams x (n_max + 1) rows, and injection is chunked.
        cp.n_batch = min(int(cp.n_batch), DRAFT_BATCH)
        cp.n_ubatch = min(int(cp.n_ubatch), DRAFT_BATCH)
        # A WINDOWED cache. Every drafter layer is sliding-window (2,048), so
        # it never attends past the window; a full-size cache (the target's
        # swa_full, which LLMVP needs for its forked heads) reserved all
        # 327,680 cells plus a compute scratch scaled to them -- 1.7 GB of KV
        # and 1.5 GB of compute for positions no drafter layer reads.
        cp.swa_full = DRAFT_SWA_FULL
        self.ctx = self._internals.LlamaContext(
            model=self.model, params=cp, verbose=False
        )
        lc.llama_set_embeddings_nextn(self.ctx.ctx, True, True)
        lc.llama_set_causal_attn(self.ctx.ctx, False)
        for lid in self.layers:
            lc.llama_set_embeddings_layer_inp(target_ctx.ctx, lid, True)
        self.n_batch = int(cp.n_batch)
        self.n_ubatch = int(cp.n_ubatch)
        self.n_seq_max = int(cp.n_seq_max)
        self._b_inject = self._internals.LlamaBatch(
            n_tokens=self.n_batch,
            embd=self.n_embd_dec,
            n_seq_max=self.n_seq_max,
            verbose=False,
        )
        self._b_draft = self._internals.LlamaBatch(
            n_tokens=self.n_batch, embd=0, n_seq_max=self.n_seq_max, verbose=False
        )
        self._attach(target_ctx)

    def release_ctx(self) -> None:
        """Detach from the target and free the drafter context. MUST run before
        the target context is closed: the drafter holds a pointer to it."""
        self._detach()
        for obj in (getattr(self, "_b_inject", None), getattr(self, "_b_draft", None)):
            try:
                if obj is not None:
                    obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._b_inject = self._b_draft = None
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:  # noqa: BLE001
                logger.exception("DFlash drafter context close failed")
        self.ctx = None

    def close(self) -> None:
        self.release_ctx()
        try:
            self.model.close()
        except Exception:  # noqa: BLE001
            pass

    def disable(self, why: str) -> None:
        if self.enabled:
            self.enabled = False
            self.disabled_reason = why
            logger.error("🚫 DFlash speculation DISABLED for this process: %s", why)

    def _fail(self, what: str) -> None:
        self.h_failures += 1
        logger.warning("DFlash drafter failure #%d: %s", self.h_failures, what)
        if self.h_failures >= MAX_FAILURES:
            self.disable(f"{self.h_failures} drafter failures (last: {what})")

    # ── the attachment (decode hook + KV mirror) ──────────────────

    def _attach(self, tctx: Any) -> None:
        self._detach()
        self.target = tctx
        drafter = self
        orig_decode = tctx.decode

        def decode(batch: Any, _orig=orig_decode) -> int:
            ret = _orig(batch)
            if ret == 0 and drafter.enabled:
                try:
                    drafter.process(batch)
                except Exception as exc:  # noqa: BLE001 -- never the target's problem
                    drafter._fail(f"process: {exc}")
            return ret

        self._originals["decode"] = orig_decode
        tctx.decode = decode
        for name in _MIRRORED:
            orig = getattr(tctx, name)

            def mirrored(*args: Any, _orig=orig, _name=name) -> Any:
                result = _orig(*args)
                drafter._mirror(_name, args)
                return result

            self._originals[name] = orig
            setattr(tctx, name, mirrored)
        tctx._dflash = self

    def _detach(self) -> None:
        t = self.target
        if t is not None:
            for name in ("decode", *_MIRRORED):
                if name in self._originals:
                    try:
                        delattr(t, name)  # drop the instance shadow
                    except AttributeError:
                        pass
            try:
                del t._dflash
            except AttributeError:
                pass
        self._originals = {}
        self.target = None

    def _mirror(self, name: str, args: Sequence[Any]) -> None:
        if self.ctx is None:
            return
        try:
            getattr(self.ctx, name)(*args)
        except Exception as exc:  # noqa: BLE001
            self._fail(f"mirror {name}{tuple(args)}: {exc}")

    # ── process(): features -> encode -> inject ───────────────────

    def _pos_max(self, seq: int) -> int:
        return int(self._lc.llama_memory_seq_pos_max(self.ctx.get_memory(), int(seq)))

    def covered(self, seq: int, n_past: int) -> bool:
        """Does the drafter's KV for ``seq`` end exactly where the target's does
        (position n_past - 1)? Only then is a draft at n_past sound."""
        if not self.enabled or self.ctx is None:
            return False
        have = self._pos_max(seq)
        ok = have == int(n_past) - 1
        if not ok:
            self.h_uncovered += 1
            if self.h_uncovered <= 5 or self.h_uncovered % 1000 == 0:
                logger.warning(
                    "DFlash: seq %d not covered (drafter KV ends at %d, target at "
                    "%d) — this stream decodes plainly [%d such]",
                    seq,
                    have,
                    int(n_past) - 1,
                    self.h_uncovered,
                )
        return ok

    def process(self, batch: Any) -> None:
        """Inject the features of the target batch just decoded."""
        lc = self._lc
        raw = batch.batch
        n = int(raw.n_tokens)
        if n <= 0:
            return
        keep = covered_rows(
            [
                (
                    int(raw.pos[i]),
                    [int(raw.seq_id[i][k]) for k in range(int(raw.n_seq_id[i]))],
                )
                for i in range(n)
            ],
            self._pos_max,
        )
        if len(keep) != n:
            self.h_skipped_rows += n - len(keep)
            if self.h_skipped_rows == n - len(keep) or self.h_skipped_rows % 10000 < n:
                logger.warning(
                    "DFlash: %d of %d target rows not injected (seq not covered) "
                    "[%d total]",
                    n - len(keep),
                    n,
                    self.h_skipped_rows,
                )
        if not keep:
            return
        if self._feat is None or self._feat.shape[0] < n:
            self._feat = np.empty(
                (max(n, self.n_batch), self.n_embd_enc), dtype=np.float32
            )
        feat = self._feat
        for k, lid in enumerate(self.layers):
            ptr = lc.llama_get_embeddings_layer_inp(self.target.ctx, lid)
            if not ptr:
                raise RuntimeError(f"target layer {lid} input was not extracted")
            src = np.ctypeslib.as_array(ptr, shape=(n, self.n_embd_tgt))
            feat[:n, k * self.n_embd_tgt : (k + 1) * self.n_embd_tgt] = src
        rows = feat[keep] if len(keep) != n else feat[:n]
        rows = np.ascontiguousarray(rows)
        inj = self._b_inject.batch
        for off in range(0, len(keep), self.n_ubatch):
            chunk = rows[off : off + self.n_ubatch]
            enc = lc.llama_batch()
            enc.n_tokens = chunk.shape[0]
            enc.embd = chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            rc = lc.llama_encode(self.ctx.ctx, enc)
            if rc != 0:
                raise RuntimeError(f"llama_encode rc={rc}")
            g = lc.llama_get_embeddings_nextn(self.ctx.ctx)
            if not g:
                raise RuntimeError("drafter encoder produced no output")
            m = chunk.shape[0]
            ctypes.memmove(inj.embd, g, m * self.n_embd_dec * 4)
            for j in range(m):
                i = keep[off + j]
                inj.pos[j] = raw.pos[i]
                ns = int(raw.n_seq_id[i])
                inj.n_seq_id[j] = ns
                for k in range(ns):
                    inj.seq_id[j][k] = raw.seq_id[i][k]
                inj.logits[j] = 0
            inj.n_tokens = m
            rc = lc.llama_decode(self.ctx.ctx, inj)
            if rc != 0:
                raise RuntimeError(f"inject decode rc={rc} ({m} rows)")
            self.h_injected_rows += m

    # ── draft() ───────────────────────────────────────────────────

    def draft(self, items: Sequence[Tuple[int, int, int, int]]) -> Dict[int, List[int]]:
        """Draft for every (seq, n_past, last_token, k): one non-causal pass over
        [last_token, mask * k] per seq. Returns {seq: [k tokens]}; the drafter's
        mask block is removed again (the verify batch injects the real rows)."""
        if not items or not self.enabled or self.ctx is None:
            return {}
        lc = self._lc
        b = self._b_draft
        b.reset()
        spans: List[Tuple[int, int, int]] = []  # (seq, first mask row, k)
        for seq, n_past, last, k in items:
            if b.batch.n_tokens + 1 + k > self.n_batch:
                break
            # EVERY row an output, the anchor included: llama.cpp drops
            # non-output rows before the last layer, which a CAUSAL model never
            # notices -- but this drafter attends across its block
            # non-causally, so dropping the anchor shifted every mask's
            # prediction one position behind (drafts read [t1, t1, t2]
            # instead of [t1, t2, t3]: 20% acceptance instead of 54%, live
            # 2026-10-02). llama.cpp's own loop adds them all with logits on.
            b.add_token(int(last), int(n_past), [int(seq)], True)
            first = int(b.batch.n_tokens)
            for j in range(k):
                b.add_token(self.mask, int(n_past) + 1 + j, [int(seq)], True)
            spans.append((int(seq), first, int(k)))
        rc = lc.llama_decode(self.ctx.ctx, b.batch)
        out: Dict[int, List[int]] = {}
        try:
            if rc != 0:
                self._fail(f"draft decode rc={rc}")
                return {}
            for seq, first, k in spans:
                toks = []
                for j in range(k):
                    ptr = lc.llama_get_logits_ith(self.ctx.ctx, first + j)
                    row = np.ctypeslib.as_array(ptr, shape=(self.n_vocab,))
                    toks.append(int(row.argmax()))
                out[seq] = toks
            return out
        finally:
            for seq, first, k in spans:
                n_past = int(b.batch.pos[first]) - 1
                self.ctx.memory_seq_rm(seq, n_past, -1)

    def record(self, drafted: int, accepted: int, kind: str = "default") -> None:
        self.h_drafted += int(drafted)
        self.h_accepted += int(accepted)
        self.h_steps += 1
        by = self.h_by_kind.get(kind)
        if by is None:
            by = self.h_by_kind[kind] = [0, 0, 0, [0] * (int(self.n_max) + 1)]
        by[0] += int(drafted)
        by[1] += int(accepted)
        by[2] += 1
        if 0 <= int(accepted) < len(by[3]):
            by[3][int(accepted)] += 1
        if self.h_steps % 100 == 0:
            logger.info("🚀 DFlash: %s", self.stats())

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "disabled_reason": self.disabled_reason or None,
            "n_max": self.n_max,
            "drafted": self.h_drafted,
            "accepted": self.h_accepted,
            "accept_rate": (
                round(self.h_accepted / self.h_drafted, 4) if self.h_drafted else None
            ),
            "verify_steps": self.h_steps,
            "uncovered_skips": self.h_uncovered,
            "injected_rows": self.h_injected_rows,
            "skipped_rows": self.h_skipped_rows,
            "failures": self.h_failures,
            "by_kind": {
                kind: {
                    "drafted": d,
                    "accepted": a,
                    "accept_rate": round(a / d, 4) if d else None,
                    "verify_steps": n,
                    # tokens a verify step yields: the accepted drafts plus
                    # the bonus token -- the speed-up before batching costs
                    "tokens_per_step": round((a + n) / n, 3) if n else None,
                    # verify steps that accepted 0, 1, ..., n_max drafts
                    "accepted_hist": list(hist),
                }
                for kind, (d, a, n, hist) in sorted(self.h_by_kind.items())
            },
        }
