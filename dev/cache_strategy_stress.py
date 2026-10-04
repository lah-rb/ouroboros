#!/usr/bin/env python3
"""Stress-test KV-cache reuse strategies on a real model — grounding for the cache redesign.

Three tiers of "reuse a prefix's KV without re-prefilling it":
  T3 full_save_state — Llama.save_state()/load_state(): whole-context LlamaState blob. What LLMVP
                       uses today. Suspected fragile on SWA (gpt-oss) and multi-GB-overflow-prone.
  T2 per_seq_state   — llama_state_seq_get_data/set_data on ONE sequence: a small per-sequence blob.
  T1 seq_cp          — llama_memory_seq_cp: fork a resident prefix sequence to another seq_id in the
                       same context (the "resident hot-set" design; no serialization).

For each tier we measure:
  RUN      — did it execute without raising / engine error?
  CORRECT  — does the reused-KV path produce the EXACT greedy continuation a fresh prefill does?
             (temp=0 argmax over N tokens; exact match = the restored/copied KV is faithful.)
  STATE_MB — size of the serialized state blob (T3 whole-context vs T2 per-seq).

Run with --swa-full on/off on SWA models (gpt-oss) and plain on recurrent models (Qwen3-Next).
Usage:
  python dev/cache_strategy_stress.py --model PATH [--swa-full] [--label NAME]
                                      [--n-ctx 4096] [--prefix-tokens 200] [--gen 8]
  Turn-rollback gate (hybrids; run under llmvp/.venv, seat must be free):
  python dev/cache_strategy_stress.py --model PATH --mode rollback \
      --n-ctx 131072 --depths 2000,30000,100000 --gen 16
"""

from __future__ import annotations

import argparse
import ctypes
import sys

import numpy as np

import llama_cpp
from llama_cpp import Llama


def _logits_argmax(llm: Llama) -> int:
    """Greedy next token from the context's current last-position logits."""
    logits = llm._ctx.get_logits_ith(-1)  # POINTER(c_float), len n_vocab
    arr = np.ctypeslib.as_array(logits, shape=(llm._model.n_vocab(),))
    return int(np.argmax(arr))


def _hard_reset(llm: Llama) -> None:
    """Llama.reset() only zeroes the Python n_tokens counter; it leaves the C-level KV cells
    occupied, so a re-prefill at position 0 conflicts ('Invalid input batch'). Also clear KV.
    """
    llm.n_tokens = 0
    llm._ctx.memory_clear(True)


def greedy_after_eval(llm: Llama, n: int) -> list[int]:
    """Assuming the context is positioned after a prefill, emit n greedy tokens."""
    out: list[int] = []
    for _ in range(n):
        tok = _logits_argmax(llm)
        out.append(tok)
        llm.eval([tok])
    return out


def fresh_reference(
    llm: Llama, prefix: list[int], tail: list[int], n: int
) -> list[int]:
    _hard_reset(llm)
    llm.eval(prefix + tail)
    return greedy_after_eval(llm, n)


def _decode_on_seq(llm: Llama, seq_id: int, tokens: list[int], start_pos: int) -> int:
    """Decode `tokens` onto an arbitrary seq_id (the high-level eval only targets seq 0).
    Logits computed on the last token. Returns the next free position."""
    batch = llm._batch
    batch.reset()
    n = len(tokens)
    pos = list(range(start_pos, start_pos + n))
    logits = [False] * (n - 1) + [True]
    batch.add_sequence(list(tokens), pos, [seq_id], logits)
    rc = llm._ctx.decode(batch)
    if rc != 0:
        raise RuntimeError(f"decode on seq {seq_id} returned rc={rc}")
    return start_pos + n


def greedy_on_seq(llm: Llama, seq_id: int, start_pos: int, n: int) -> list[int]:
    """Greedy-continue on a specific seq_id, reading the last-decoded logits each step."""
    out: list[int] = []
    pos = start_pos
    for _ in range(n):
        tok = _logits_argmax(llm)
        out.append(tok)
        pos = _decode_on_seq(llm, seq_id, [tok], pos)
    return out


def _ctx_ptr(llm: Llama):
    return llm._ctx.ctx


def _set_position(llm: Llama, tokens: list[int]) -> None:
    """Sync the high-level Python-side counters to a KV state restored at the C level.
    The low-level state restores KV but not llm.n_tokens / input_ids — fix them so eval() places
    the next tokens at the correct KV position."""
    llm.n_tokens = len(tokens)
    llm.input_ids[: len(tokens)] = np.array(tokens, dtype=np.intc)


# ── T3: whole-context save_state/load_state ────────────────────────────────
def test_full_save_state(llm, prefix, tail, n, ref):
    res = {
        "tier": "T3 full_save_state",
        "run": False,
        "correct": None,
        "state_mb": None,
        "detail": "",
    }
    try:
        _hard_reset(llm)
        llm.eval(prefix)
        st = llm.save_state()  # whole-context blob
        res["state_mb"] = round(st.llama_state_size / (1024 * 1024), 2)
        _hard_reset(llm)
        llm.load_state(st)  # restores KV + n_tokens
        llm.eval(tail)
        got = greedy_after_eval(llm, n)
        res["run"] = True
        res["correct"] = got == ref
        res["detail"] = (
            "match" if got == ref else f"DIVERGED ref={ref[:4]} got={got[:4]}"
        )
    except Exception as e:  # noqa: BLE001
        res["detail"] = f"CRASH: {type(e).__name__}: {str(e)[:160]}"
    return res


# ── T2: per-sequence state get/set on seq 0 ────────────────────────────────
def test_per_seq_state(llm, prefix, tail, n, ref):
    res = {
        "tier": "T2 per_seq_state",
        "run": False,
        "correct": None,
        "state_mb": None,
        "detail": "",
    }
    try:
        ctx = _ctx_ptr(llm)
        _hard_reset(llm)
        llm.eval(prefix)
        size = llama_cpp.llama_state_seq_get_size(ctx, 0)
        buf = (ctypes.c_uint8 * int(size))()
        nbytes = llama_cpp.llama_state_seq_get_data(ctx, buf, size, 0)
        res["state_mb"] = round(int(nbytes) / (1024 * 1024), 2)
        _hard_reset(llm)  # clears seq 0 KV
        nread = llama_cpp.llama_state_seq_set_data(ctx, buf, nbytes, 0)
        if int(nread) == 0:
            raise RuntimeError("state_seq_set_data returned 0 (restore rejected)")
        _set_position(llm, prefix)
        llm.eval(tail)
        got = greedy_after_eval(llm, n)
        res["run"] = True
        res["correct"] = got == ref
        res["detail"] = (
            "match" if got == ref else f"DIVERGED ref={ref[:4]} got={got[:4]}"
        )
    except Exception as e:  # noqa: BLE001
        res["detail"] = f"CRASH: {type(e).__name__}: {str(e)[:160]}"
    return res


# ── T1: resident seq_cp fork (copy seq 0 -> seq 1, verify via restore into seq 0) ──
def test_seq_cp(llm, prefix, tail, n, ref):
    res = {
        "tier": "T1 seq_cp",
        "run": False,
        "correct": None,
        "state_mb": None,
        "detail": "",
    }
    try:
        ctx = _ctx_ptr(llm)
        mem = llama_cpp.llama_get_memory(ctx)
        _hard_reset(llm)
        llm.eval(prefix)
        sz0 = llama_cpp.llama_state_seq_get_size(ctx, 0)
        # fork seq 0 -> seq 1 (the resident-prefix clone)
        llama_cpp.llama_memory_seq_cp(mem, 0, 1, -1, -1)
        sz1 = llama_cpp.llama_state_seq_get_size(ctx, 1)
        res["state_mb"] = round(int(sz1) / (1024 * 1024), 2)
        size_match = int(sz1) == int(sz0) and int(sz1) > 0
        # correctness: pull seq 1's state, restore into a cleared seq 0, decode tail there.
        buf = (ctypes.c_uint8 * int(sz1))()
        nbytes = llama_cpp.llama_state_seq_get_data(ctx, buf, sz1, 1)
        _hard_reset(llm)
        nread = llama_cpp.llama_state_seq_set_data(ctx, buf, nbytes, 0)
        if int(nread) == 0:
            raise RuntimeError("seq1 state restore rejected")
        _set_position(llm, prefix)
        llm.eval(tail)
        got = greedy_after_eval(llm, n)
        res["run"] = True
        res["correct"] = got == ref
        res["detail"] = (
            f"seq0_size={int(sz0)} seq1_size={int(sz1)} size_match={size_match}; "
            + ("match" if got == ref else f"DIVERGED ref={ref[:4]} got={got[:4]}")
        )
    except Exception as e:  # noqa: BLE001
        res["detail"] = f"CRASH: {type(e).__name__}: {str(e)[:160]}"
    return res


# ── Multi-seq concurrency: the resident hot-set (fork prefix -> 2 seqs, decode different tails) ──
def test_multiseq(llm, prefix, tail_a, tail_b, n):
    """Prefill prefix on seq 0, seq_cp -> seq 1, then decode DIFFERENT tails on each seq and
    check both match their own fresh-prefill reference (faithful + no cross-contamination).
    """
    res = {
        "tier": "MULTISEQ seq_cp",
        "run": False,
        "correct": None,
        "state_mb": None,
        "detail": "",
    }
    try:
        ctx = _ctx_ptr(llm)
        mem = llama_cpp.llama_get_memory(ctx)
        ref_a = fresh_reference(llm, prefix, tail_a, n)
        ref_b = fresh_reference(llm, prefix, tail_b, n)

        _hard_reset(llm)
        llm.eval(prefix)  # prefix on seq 0
        llama_cpp.llama_memory_seq_cp(
            mem, 0, 1, -1, -1
        )  # fork -> seq 1 (resident clone)
        # branch B on seq 1 (manual batch), fully, first
        next1 = _decode_on_seq(llm, 1, tail_b, len(prefix))
        got_b = greedy_on_seq(llm, 1, next1, n)
        # branch A on seq 0 (high-level eval continues seq 0; n_tokens still == len(prefix))
        llm.n_tokens = len(prefix)
        llm.eval(tail_a)
        got_a = greedy_after_eval(llm, n)

        ok_a, ok_b = (got_a == ref_a), (got_b == ref_b)
        res["run"] = True
        res["correct"] = ok_a and ok_b
        res["detail"] = (
            f"seq0(A) {'ok' if ok_a else f'BAD got={got_a[:4]} ref={ref_a[:4]}'} | "
            f"seq1(B) {'ok' if ok_b else f'BAD got={got_b[:4]} ref={ref_b[:4]}'} | "
            f"distinct_refs={ref_a != ref_b}"
        )
    except Exception as e:  # noqa: BLE001
        res["detail"] = f"CRASH: {type(e).__name__}: {str(e)[:160]}"
    return res


# ── Depth sweep: how the whole-context save_state blob (T3) grows toward the multi-GB overflow ──
def test_depth_sweep(llm, depths, vocab_filler):
    """At increasing prefill depths, report the T3 whole-context save_state blob size (and whether
    it crashes) vs the T2 per-sequence size — grounds the deep-session overflow trajectory.
    """
    rows = []
    for d in depths:
        row = {"depth": d, "t3_mb": None, "t2_mb": None, "detail": ""}
        try:
            _hard_reset(llm)
            toks = vocab_filler(d)
            llm.eval(toks)
            ctx = _ctx_ptr(llm)
            sz_seq = llama_cpp.llama_state_seq_get_size(ctx, 0)
            row["t2_mb"] = round(int(sz_seq) / (1024 * 1024), 2)
            try:
                st = llm.save_state()
                row["t3_mb"] = round(st.llama_state_size / (1024 * 1024), 2)
                row["detail"] = "ok"
            except Exception as e:  # noqa: BLE001
                row["detail"] = (
                    f"T3 save_state CRASH: {type(e).__name__}: {str(e)[:90]}"
                )
        except Exception as e:  # noqa: BLE001
            row["detail"] = f"prefill failed: {type(e).__name__}: {str(e)[:90]}"
        rows.append(row)
        print(
            f"  depth={d:>6}: T3_whole={row['t3_mb']} MB  T2_perseq={row['t2_mb']} MB  {row['detail']}",
            flush=True,
        )
    return rows


# ── ROLLBACK: per-turn recurrent checkpoint (llmvp inference/turn_checkpoint.py) ──
#
# Gates LLMVP's turn rollback on real hardware before any config relies on it.
# Arms, all on ONE context shaped like production (n_seq_max=1, the binding's
# own checkpoint FIFO off):
#   B  rollback — eval(P); capture; eval(G) [the turn to drop]; restore; eval(T)
#   A  inline   — eval(P); eval(T)   (same batch composition as B → expect BIT-identical)
#   C  fresh    — eval(P + T)        (different batching → near-tie tolerant)
# Controls: a naive tail rm without the restore must be REFUSED on a hybrid; a
# checkpoint must not survive memory_clear; the checkpoint size must not grow
# with depth. Strip arm: a turn [K + think + answer] rolled back and replayed as
# [K + answer] must continue identically to an inline eval of the same stream.
# Comparison is teacher-forced over the reference's greedy tokens, so every
# position is aligned even if an arm would diverge; the verdict FAILS when no
# position was comparable (a gate on zero checkable items passes vacuously).


def _last_logits(llm: Llama) -> np.ndarray:
    ptr = llm._ctx.get_logits_ith(-1)
    return np.array(
        np.ctypeslib.as_array(ptr, shape=(llm._model.n_vocab(),)), dtype=np.float64
    )


def _teacher_forced(llm: Llama, forced: list[int]) -> list[np.ndarray]:
    out = []
    for tok in forced:
        out.append(_last_logits(llm))
        llm.eval([tok])
    return out


def _compare(ref: list[np.ndarray], got: list[np.ndarray], eps: float) -> dict:
    compared = agree = 0
    max_abs = 0.0
    kls = []
    flip_margins = []
    for r, g in zip(ref, got):
        max_abs = max(max_abs, float(np.max(np.abs(r - g))))
        top2 = np.partition(r, -2)[-2:]
        margin = float(top2[1] - top2[0])
        same = int(np.argmax(r)) == int(np.argmax(g))
        if not same:
            flip_margins.append(round(margin, 4))
        if margin >= eps:
            compared += 1
            agree += int(same)
        pr = np.exp(r - r.max())
        pr /= pr.sum()
        pg = np.exp(g - g.max())
        pg /= pg.sum()
        kls.append(float(np.sum(pr * (np.log(pr + 1e-30) - np.log(pg + 1e-30)))))
    return {
        "compared": compared,
        "agree": agree,
        "max_abs": max_abs,
        "mean_kl": float(np.mean(kls)) if kls else float("nan"),
        "flip_margins": flip_margins,
        "ok": compared > 0 and agree == compared,
    }


def test_turn_rollback(
    llm: Llama, fill, depths: list[int], gen: int, eps: float
) -> bool:
    import time
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "llmvp"))
    from inference.turn_checkpoint import (  # noqa: E402
        MODE_PARTIAL,
        RestoreOutcome,
        TurnCheckpointStore,
    )

    tok = lambda b: llm.tokenize(b, add_bos=False)  # noqa: E731
    T = tok(b"\nQ: Name the capital of France, then explain why in one sentence. A:")
    G = tok(b"\nQ: Describe a sunset over the ocean in vivid detail. A: The sky")
    K = tok(b"\nQ: What is 2 + 2? A:")
    THINK = tok(b" Let me think step by step about this simple sum.")
    ANS = tok(b" 4.")
    NEXT = tok(b"\nQ: And what is 3 + 3? A:")
    all_ok = True
    sizes = set()
    for depth in depths:
        P = fill(depth)
        if len(P) + len(G) + len(T) + gen + 64 >= llm.n_ctx():
            print(f"  depth {depth}: skipped (n_ctx {llm.n_ctx()})", flush=True)
            continue
        print(f"\n-- depth {depth} (P={len(P)} tok) --", flush=True)

        # A: inline reference; its greedy tokens are the teacher sequence.
        _hard_reset(llm)
        llm.eval(P)
        llm.eval(T)
        forced = []
        a_logits = []
        for _ in range(gen):
            lg = _last_logits(llm)
            a_logits.append(lg)
            t = int(np.argmax(lg))
            forced.append(t)
            llm.eval([t])

        # A': the SAME inline path again — the determinism control. If the model
        # is not bit-reproducible at this depth (MoE routing / sparse-attention
        # top-k on Metal), B cannot be held to bit identity with A either.
        _hard_reset(llm)
        llm.eval(P)
        llm.eval(T)
        a2_logits = _teacher_forced(llm, forced)

        # B: rollback.
        _hard_reset(llm)
        store = TurnCheckpointStore(MODE_PARTIAL)
        llm.eval(P)
        ck = store.capture(llm)
        if ck is None:
            print("  capture FAILED", flush=True)
            all_ok = False
            continue
        sizes.add(ck.nbytes)
        llm.eval(G)
        # Negative control first — the rm alone must be refused while G is in.
        naive = llm._ctx.memory_seq_rm(0, len(P), -1)
        t0 = time.perf_counter()
        outcome = store.restore(llm, len(P))
        restore_ms = (time.perf_counter() - t0) * 1000
        print(
            f"  checkpoint {ck.nbytes / 2**20:.1f} MiB  save {ck.save_ms:.1f} ms  "
            f"restore {restore_ms:.1f} ms  outcome={outcome.value}  "
            f"naive_rm_refused={naive is False}",
            flush=True,
        )
        if outcome is not RestoreOutcome.OK:
            all_ok = False
            continue
        llm.eval(T)
        b_logits = _teacher_forced(llm, forced)

        # C: fresh single prefill.
        _hard_reset(llm)
        llm.eval(P + T)
        c_logits = _teacher_forced(llm, forced)

        ab = _compare(a_logits, b_logits, eps=0.0)
        aa = _compare(a_logits, a2_logits, eps=0.0)
        bc = _compare(c_logits, b_logits, eps=eps)
        bit_identical = ab["max_abs"] == 0.0
        reproducible = aa["max_abs"] == 0.0
        print(
            f"  A vs A' (determinism control): max|Δ|={aa['max_abs']:.3g} "
            f"reproducible={reproducible}",
            flush=True,
        )
        print(
            f"  B vs A (same batching): max|Δ|={ab['max_abs']:.3g} "
            f"bit_identical={bit_identical} top1 {ab['agree']}/{ab['compared']}",
            flush=True,
        )
        if reproducible and not bit_identical:
            # The model reproduces itself exactly, so any B/A difference IS
            # the rollback's doing.
            print("  rollback is NOT exact on a reproducible model", flush=True)
            all_ok = False
        # On a model that does not reproduce itself, exactness is not
        # observable; hold the rollback to the same near-tie criterion as the
        # fresh prefill, and show how big the model's own noise is.
        tie_eps = 0.0 if reproducible else eps
        if not reproducible:
            ab = _compare(a_logits, b_logits, eps=eps)
            print(
                f"  (not reproducible → near-tie tolerant: A/A' top1 "
                f"{aa['agree']}/{aa['compared']} flips at margins {aa['flip_margins']}; "
                f"B/A top1 {ab['agree']}/{ab['compared']} flips at {ab['flip_margins']})",
                flush=True,
            )
        print(
            f"  B vs C (fresh prefill): max|Δ|={bc['max_abs']:.3g} "
            f"mean_KL={bc['mean_kl']:.3g} top1 {bc['agree']}/{bc['compared']} "
            f"(margin ≥ {eps})",
            flush=True,
        )
        all_ok &= bool(ab["ok"] and bc["ok"])

        # Stale control: a cleared context must not be rolled back.
        llm.eval(G)
        _hard_reset(llm)
        stale = store.restore(llm, len(P))
        print(f"  stale control: {stale.value} (want refused)", flush=True)
        all_ok &= stale is RestoreOutcome.REFUSED_UNTOUCHED

        # Strip arm: [K + THINK + ANS] rolled back and replayed as [K + ANS].
        _hard_reset(llm)
        store = TurnCheckpointStore(MODE_PARTIAL)
        llm.eval(P)
        store.capture(llm)
        llm.eval(K + THINK + ANS)
        if store.restore(llm, len(P)) is not RestoreOutcome.OK:
            print("  strip arm: restore FAILED", flush=True)
            all_ok = False
            continue
        llm.eval(K + ANS)
        llm.eval(NEXT)
        s_logits = _teacher_forced(llm, forced)
        _hard_reset(llm)
        llm.eval(P)
        llm.eval(K + ANS)
        llm.eval(NEXT)
        r_logits = _teacher_forced(llm, forced)
        st = _compare(r_logits, s_logits, eps=tie_eps)
        print(
            f"  strip arm vs inline stripped stream: max|Δ|={st['max_abs']:.3g} "
            f"top1 {st['agree']}/{st['compared']} (margin ≥ {tie_eps}; flips at "
            f"{st['flip_margins']})",
            flush=True,
        )
        all_ok &= bool(st["ok"])

    if len(sizes) > 1:
        print(f"  checkpoint size VARIED with depth: {sorted(sizes)}", flush=True)
        all_ok = False
    print(f"\n== ROLLBACK VERDICT: {'PASS' if all_ok else 'FAIL'} ==", flush=True)
    return all_ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--label", default="")
    ap.add_argument("--n-ctx", type=int, default=4096)
    ap.add_argument("--prefix-tokens", type=int, default=200)
    ap.add_argument("--gen", type=int, default=8)
    ap.add_argument("--n-gpu-layers", type=int, default=-1)
    ap.add_argument(
        "--mode",
        default="tiers",
        choices=["tiers", "multiseq", "depth", "all", "rollback"],
    )
    ap.add_argument("--depths", default="1000,4000,16000,32000")
    ap.add_argument(
        "--eps",
        type=float,
        default=0.05,
        help="rollback mode: top-1/top-2 logit margin below which a position "
        "is a near-tie and not compared against the fresh prefill",
    )
    args = ap.parse_args()

    label = args.label or args.model.split("/")[-1]
    print(
        f"== cache-strategy stress: {label} | swa_full={args.swa_full} | n_ctx={args.n_ctx} ==",
        flush=True,
    )
    print("loading model… (large GGUF, may take minutes)", flush=True)
    rollback = args.mode == "rollback"
    llm = Llama(
        model_path=args.model,
        n_ctx=args.n_ctx,
        n_gpu_layers=args.n_gpu_layers,
        # T1 needs seq 0 + seq 1; rollback mirrors production (one seq, and the
        # binding's own checkpoint FIFO off — llmvp _FORK_CTX_CHECKPOINTS).
        n_seq_max=1 if rollback else 2,
        swa_full=bool(args.swa_full),
        kv_unified=True,  # single unified KV (Metal); pairs with swa_full
        logits_all=False,
        verbose=False,
        **({"ctx_checkpoints": 0} if rollback else {}),
    )
    print(
        f"loaded. n_swa={llm._model.n_swa()} is_recurrent={llm._model.is_recurrent()} "
        f"is_hybrid={llm._model.is_hybrid()} memory_can_shift={llm._ctx.memory_can_shift()}",
        flush=True,
    )

    def fill(n_tokens: int) -> list[int]:
        words = "You are a meticulous operator. " + " ".join(
            f"item{i}" for i in range(n_tokens)
        )
        return llm.tokenize(words.encode(), add_bos=True)[:n_tokens]

    if args.mode in ("tiers", "all"):
        prefix = fill(args.prefix_tokens)
        tail = llm.tokenize(
            b"\nNow answer in one short sentence: what is the capital of France?",
            add_bos=False,
        )
        ref = fresh_reference(llm, prefix, tail, args.gen)
        print(f"reference greedy[{args.gen}] = {ref}", flush=True)
        results = [
            test_full_save_state(llm, prefix, tail, args.gen, ref),
            test_per_seq_state(llm, prefix, tail, args.gen, ref),
            test_seq_cp(llm, prefix, tail, args.gen, ref),
        ]
        print("\n== TIERS (shallow save/restore correctness) ==", flush=True)
        print(
            f"{'tier':<20} {'run':<5} {'correct':<8} {'state_MB':<9} detail", flush=True
        )
        for r in results:
            print(
                f"{r['tier']:<20} {str(r['run']):<5} {str(r['correct']):<8} "
                f"{str(r['state_mb']):<9} {r['detail']}",
                flush=True,
            )

    if args.mode in ("multiseq", "all"):
        # Coherent prefix so distinct tails yield DISTINCT answers (so contamination is detectable).
        pre = (
            b"You are a precise geography assistant. Reply with only the city name. "
            b"Context: the quarterly review is complete and all systems are nominal."
        )
        prefix = llm.tokenize(pre, add_bos=True)
        tail_a = llm.tokenize(b"\nQ: What is the capital of France? A:", add_bos=False)
        tail_b = llm.tokenize(b"\nQ: What is the capital of Japan? A:", add_bos=False)
        print(
            "\n== MULTISEQ (resident hot-set: fork prefix to 2 seqs, decode different tails) ==",
            flush=True,
        )
        r = test_multiseq(llm, prefix, tail_a, tail_b, args.gen)
        print(
            f"{r['tier']:<20} run={r['run']} correct={r['correct']}  {r['detail']}",
            flush=True,
        )

    if rollback:
        depths = [int(x) for x in args.depths.split(",")]
        print("\n== ROLLBACK (per-turn recurrent checkpoint) ==", flush=True)
        return 0 if test_turn_rollback(llm, fill, depths, args.gen, args.eps) else 1

    if args.mode in ("depth", "all"):
        depths = [int(x) for x in args.depths.split(",")]
        print(
            "\n== DEPTH SWEEP (T3 whole-context blob growth → multi-GB overflow trajectory) ==",
            flush=True,
        )
        test_depth_sweep(llm, depths, fill)

    return 0


if __name__ == "__main__":
    sys.exit(main())
