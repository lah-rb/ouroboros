#!/usr/bin/env python3
"""GGUF headers: KV geometry, hot-path bytes/token, and the chat template.

    python3 dev/gguf_geometry.py <file.gguf> [--template] [--vocab PATTERN]

WHY THIS LIVES IN dev/ RATHER THAN A SCRATCHPAD. Every config in this repo is
sized from these numbers, and getting them wrong has cost real time: gemma-4's
formula over-predicted KV ~2x (hence kv_bytes_per_token_measured), laguna's
"file size predicts speed" intuition was backwards, and the Hy3 config was
written claiming a load was impossible when it was not. Reading the headers
first is the cheap half of every model onboarding, and rewriting the reader each
time is how it stops happening.

Units note, learned the hard way: _kv_preflight compares in DECIMAL GB. Sizing a
config from the binary figure lands ~7% short and the load is refused.
"""

from __future__ import annotations

import argparse
import glob
import os
import struct
import sys
from collections import defaultdict

GGML = {
    0: ("F32", 1, 4),
    1: ("F16", 1, 2),
    2: ("Q4_0", 32, 18),
    3: ("Q4_1", 32, 20),
    6: ("Q5_0", 32, 22),
    7: ("Q5_1", 32, 24),
    8: ("Q8_0", 32, 34),
    9: ("Q8_1", 32, 36),
    10: ("Q2_K", 256, 84),
    11: ("Q3_K", 256, 110),
    12: ("Q4_K", 256, 144),
    13: ("Q5_K", 256, 176),
    14: ("Q6_K", 256, 210),
    15: ("Q8_K", 256, 292),
    16: ("IQ2_XXS", 256, 66),
    17: ("IQ2_XS", 256, 74),
    18: ("IQ3_XXS", 256, 98),
    19: ("IQ1_S", 256, 50),
    20: ("IQ4_NL", 32, 18),
    21: ("IQ3_S", 256, 110),
    22: ("IQ2_S", 256, 82),
    23: ("IQ4_XS", 256, 136),
    24: ("I8", 1, 1),
    25: ("I16", 1, 2),
    26: ("I32", 1, 4),
    27: ("I64", 1, 8),
    28: ("F64", 1, 8),
    29: ("IQ1_M", 256, 56),
    30: ("BF16", 1, 2),
}


class R:
    def __init__(self, f):
        self.f = f

    def u32(self):
        return struct.unpack("<I", self.f.read(4))[0]

    def u64(self):
        return struct.unpack("<Q", self.f.read(8))[0]

    def s(self):
        return self.f.read(self.u64()).decode("utf8", "replace")

    def val(self, t):
        f = self.f
        if t == 0:
            return struct.unpack("<B", f.read(1))[0]
        if t == 1:
            return struct.unpack("<b", f.read(1))[0]
        if t == 2:
            return struct.unpack("<H", f.read(2))[0]
        if t == 3:
            return struct.unpack("<h", f.read(2))[0]
        if t == 4:
            return self.u32()
        if t == 5:
            return struct.unpack("<i", f.read(4))[0]
        if t == 6:
            return struct.unpack("<f", f.read(4))[0]
        if t == 7:
            return struct.unpack("<?", f.read(1))[0]
        if t == 8:
            return self.s()
        if t == 9:
            et = self.u32()
            n = self.u64()
            return [self.val(et) for _ in range(n)]
        if t == 10:
            return self.u64()
        if t == 11:
            return struct.unpack("<q", f.read(8))[0]
        if t == 12:
            return struct.unpack("<d", f.read(8))[0]
        raise ValueError(f"unknown gguf value type {t}")


def read(path: str):
    with open(path, "rb") as f:
        r = R(f)
        assert f.read(4) == b"GGUF", "not a GGUF"
        r.u32()
        n_tensors, n_kv = r.u64(), r.u64()
        kv = {}
        for _ in range(n_kv):
            k = r.s()
            kv[k] = r.val(r.u32())
        tensors = []
        for _ in range(n_tensors):
            name = r.s()
            dims = [r.u64() for _ in range(r.u32())]
            ttype = r.u32()
            r.u64()
            n = 1
            for d in dims:
                n *= d
            tensors.append((name, ttype, n))
    return kv, tensors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--template", action="store_true", help="print the chat template")
    ap.add_argument("--vocab", help="search the vocab for a substring")
    args = ap.parse_args()

    kv, tensors = read(args.path)
    arch = kv.get("general.architecture", "?")

    def g(k, d=None):
        return kv.get(f"{arch}.{k}", d)

    layers = g("block_count")
    n_head = g("attention.head_count")
    n_kv_head = g("attention.head_count_kv", n_head)
    embd = g("embedding_length")
    head_dim = g("attention.key_length") or (
        embd // n_head if isinstance(n_head, int) and n_head else 0
    )
    if isinstance(n_kv_head, list):
        kvh = max(n_kv_head)
        kvnote = f"  (per-layer, max {kvh}; interleaved-SWA — expect the formula to OVER-predict)"
    else:
        kvh = n_kv_head
        kvnote = ""

    # All shards, not just the one named — the single most common sizing error.
    d = os.path.dirname(args.path)
    shards = sorted(glob.glob(os.path.join(d, "*.gguf")))
    shards = [s for s in shards if "mmproj" not in os.path.basename(s).lower()]
    wb = sum(os.path.getsize(s) for s in shards)

    print(f"\n{os.path.basename(args.path)}")
    print(
        f"  arch={arch}  layers={layers}  n_head={n_head if not isinstance(n_head, list) else 'per-layer'}"
        f"  n_kv_head={kvh}{kvnote}"
    )
    print(
        f"  embd={embd}  head_dim={head_dim}  experts={g('expert_count', 0)} (used {g('expert_used_count', 0)})"
    )
    print(f"  trained ctx={g('context_length')}  rope_freq_base={g('rope.freq_base')}")
    for k in sorted(kv):
        if "rope.scaling" in k:
            print(f"    {k} = {kv[k]}")
    print(
        f"  bos={kv.get('tokenizer.ggml.bos_token_id')} eos={kv.get('tokenizer.ggml.eos_token_id')} "
        f"add_bos={kv.get('tokenizer.ggml.add_bos_token')}"
    )

    per_tok = 2 * layers * kvh * head_dim * 2
    total_b = sum((n // GGML[t][1]) * GGML[t][2] for _, t, n in tensors if t in GGML)
    # Tensors are only in THIS shard; scale by the on-disk total for the real figure.
    w_gb = wb / 1e9
    print(
        f"\n  weights: {w_gb:.1f} GB decimal ({wb/1024**3:.1f} GiB, {len(shards)} shard(s))"
    )
    print(
        f"  KV/token: {per_tok/1024:.0f} KiB   -> DECIMAL GB below, matching _kv_preflight"
    )
    print(f"  {'n_ctx':>9} {'KV GB':>8} {'total GB':>10}")
    for n_ctx in (8192, 16384, 32768, 65536, 131072, 262144):
        kvg = per_tok * n_ctx / 1e9
        print(f"  {n_ctx:>9} {kvg:>8.1f} {w_gb + kvg:>10.1f}")

    hot = cold = 0.0
    by = defaultdict(float)
    for name, t, n in tensors:
        if t not in GGML:
            continue
        b = (n // GGML[t][1]) * GGML[t][2]
        is_exp = "_exps" in name or ".experts." in name
        by[(GGML[t][0], "expert" if is_exp else "dense")] += b
        if is_exp:
            cold += b
        else:
            hot += b
    ne, nu = g("expert_count", 0) or 0, g("expert_used_count", 0) or 0
    frac = (nu / ne) if ne else 1.0
    print(
        f"\n  dense+attn {hot/1024**3:6.2f} GiB · experts {cold/1024**3:6.2f} GiB"
        f" · read/token {(hot + cold*frac)/1024**3:.2f} GiB"
        f" · avg {total_b*8/max(1,sum(n for _,_,n in tensors)):.3f} bpw  (this shard)"
    )
    print(
        "  NOTE: read/token has NOT predicted decode speed on this hardware "
        "(laguna: 36 GiB file spread, same tok/s). Measure."
    )

    if args.vocab:
        toks = kv.get("tokenizer.ggml.tokens", [])
        types = kv.get("tokenizer.ggml.token_type", [])
        hits = [(i, t) for i, t in enumerate(toks) if args.vocab in t]
        print(f"\n  vocab {len(toks):,} · '{args.vocab}': {len(hits)} match")
        for i, t in hits[:12]:
            print(f"    {i:>7}  type={types[i] if types else '?'}  {t!r}")

    if args.template:
        print("\n=== CHAT TEMPLATE ===")
        print(kv.get("tokenizer.chat_template", "(none)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
