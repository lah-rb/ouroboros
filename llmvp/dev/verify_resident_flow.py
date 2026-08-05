#!/usr/bin/env python3
"""Verify the resident flow-prefix hot-set (Phase 2) through the backend.

Runs the SAME stateless flow request three ways and checks bit-identity (temp=0):
  - ref   : no flow cache (fresh prefill of [flow head + dynamic] on the global static)
  - build : first request with flow_key F → BUILD (pins [global+flow head] on a flow seq)
  - hit   : second request with flow_key F → HIT (seq_cp the pinned flow seq onto seq 0)
All three must be identical → the resident flow fork == a fresh prefill.

Usage (from llmvp/):  .venv/bin/python dev/verify_resident_flow.py <config>
"""

import asyncio
import sys
from pathlib import Path

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "gemma-4-31b"


async def main():
    from core.config import load_config, set_config

    cfg = load_config(Path("configs") / f"{CONFIG}.yaml")
    cfg.model.resident_seq_cache = True
    cfg.model.flow_kv_cache = True
    cfg.resources.jit_concurrency_limit = None
    cfg.resources.max_concurrent_requests = 1
    set_config(cfg)

    from preprocessing.static_tokens import manager, get_static_tokens

    manager.load_static_buffer()

    from inference.backends.factory import create_backend

    backend = create_backend(cfg)
    await backend.initialize()
    print(
        f"_resident_active={getattr(backend,'_resident_active',None)} "
        f"_flow_resident={getattr(backend,'_flow_resident',None)} "
        f"n_seq_max(req)={2 + backend._flow_hot_set}",
        flush=True,
    )
    if not getattr(backend, "_resident_active", False):
        print(
            "resident not active (can_shift=False) — resident flow N/A for this model"
        )
        await backend.shutdown()
        return

    # Continuation prompts (no turn framing) so the model emits a MULTI-token run —
    # any KV divergence in the flow fork vs a fresh prefill would surface in the
    # sequence, not just the first token.
    static = list(get_static_tokens())
    flow_head = list(
        backend.tokenize(
            "\n\nReference notes on number theory that the assistant may consult below."
        )
    )
    dynamic = list(
        backend.tokenize(
            "\n\nThe first twelve prime numbers in ascending order are: 2, 3, 5,"
        )
    )
    prompt = static + flow_head + dynamic
    flow_prefix_len = len(static) + len(flow_head)
    print(
        f"static={len(static)} flow_head={len(flow_head)} flow_prefix_len={flow_prefix_len} "
        f"total={len(prompt)}",
        flush=True,
    )

    async def complete(flow_key):
        inst = await backend.acquire_instance()
        kw = (
            {"flow_key": flow_key, "flow_prefix_len": flow_prefix_len}
            if flow_key
            else {}
        )
        out = "".join(backend.generate_stream_sync(inst, prompt, 24, 0.0, **kw)).strip()
        await backend.release_instance(inst)
        return out

    out_ref = await complete(None)
    out_build = await complete("flowA")
    out_hit = await complete("flowA")
    await backend.shutdown()

    print(f"  ref  ={out_ref[:60]!r}", flush=True)
    print(f"  build={out_build[:60]!r}", flush=True)
    print(f"  hit  ={out_hit[:60]!r}", flush=True)
    print(
        f"  => build==ref: {out_build == out_ref} | hit==build: {out_hit == out_build} "
        f"| hit==ref: {out_hit == out_ref}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
