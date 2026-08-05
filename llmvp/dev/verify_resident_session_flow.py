#!/usr/bin/env python3
"""Verify the resident SESSION flow-fork (model.resident_session_flow_fork).

A memoryful session started with a flow_key + static_prefix forks the pinned
[global static + flow head] onto its live seq at turn 0, so only the first user
message's tail prefills. This drives three single-instance sessions (temp=0):

  A (flow) : start(flow_key=F, static_prefix=PREAMBLE), turn 0 = P     → BUILD
  B (flow) : same, on the SAME instance after A ends                   → HIT
  C (none) : start(), turn 0 = PREAMBLE + P (preamble inline)          → reference

Checks:
  - turn-0 output A == B == C  (the fork == a fresh inline prefill, byte-identical)
  - turn-0 TTFT(B) < TTFT(A)   (HIT skips the preamble eval the BUILD paid)
  - static_base(A,B) = flow_prefix_len > global static; static_base(C) = global static

Usage (from llmvp/):  .venv/bin/python dev/verify_resident_session_flow.py <config>
"""

import asyncio
import sys
import time
from pathlib import Path

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "gemma-4-31b"

# A substantial invariant preamble so BUILD (evals it) vs HIT (forks it) differ.
PREAMBLE = (
    "You are ARC-7, a meticulous operations assistant. Operating rules: "
    + " ".join(
        f"Rule {i}: always think step by step, cite the relevant rule, and keep "
        f"answers terse and verifiable."
        for i in range(1, 40)
    )
    + " End of standing instructions. Begin the task.\n"
)
PROMPT = "List three prime numbers under 20."


async def turn0(sm, flow_key, static_prefix, prompt):
    info = await sm.start_session(
        ttl_seconds=600, flow_key=flow_key, static_prefix=static_prefix
    )
    t0 = time.time()
    first = None
    text = ""
    async for chunk in sm.session_turn(
        info.session_id, prompt, max_tokens=24, temperature=0.0
    ):
        if first is None:
            first = time.time() - t0
        text += chunk
    base = sm._sessions[info.session_id].static_base
    await sm.end_session(info.session_id)
    return text.strip(), first, base


async def main():
    from core.config import load_config, set_config

    cfg = load_config(Path("configs") / f"{CONFIG}.yaml")
    cfg.model.resident_seq_cache = True
    cfg.model.resident_session_flow_fork = True
    cfg.resources.jit_concurrency_limit = None  # eager, single instance
    cfg.resources.max_concurrent_requests = 1
    set_config(cfg)

    from preprocessing.static_tokens import manager

    manager.load_static_buffer()

    from inference.backends.factory import create_backend
    from core.session_manager import SessionManager

    backend = create_backend(cfg)
    await backend.initialize()
    print(
        f"_resident_active={getattr(backend,'_resident_active',None)} "
        f"_session_flow_fork={getattr(backend,'_session_flow_fork',None)} "
        f"_flow_band={getattr(backend,'_flow_band',None)} "
        f"global_static={getattr(backend,'_resident_static_len',None)}",
        flush=True,
    )
    if not getattr(backend, "_resident_active", False):
        print("resident not active (can_shift=False) — session flow-fork N/A")
        await backend.shutdown()
        return

    sm = SessionManager(backend)
    out_a, ttft_a, base_a = await turn0(sm, "F", PREAMBLE, PROMPT)  # BUILD
    out_b, ttft_b, base_b = await turn0(sm, "F", PREAMBLE, PROMPT)  # HIT
    out_c, ttft_c, base_c = await turn0(sm, None, None, PREAMBLE + PROMPT)  # reference
    await backend.shutdown()

    print(f"  A BUILD: ttft={ttft_a:.3f}s base={base_a} out={out_a[:48]!r}", flush=True)
    print(f"  B HIT  : ttft={ttft_b:.3f}s base={base_b} out={out_b[:48]!r}", flush=True)
    print(f"  C ref  : ttft={ttft_c:.3f}s base={base_c} out={out_c[:48]!r}", flush=True)
    identical = out_a == out_b == out_c
    print(
        f"  => out A==B==C: {identical} | TTFT(B)<TTFT(A): {ttft_b < ttft_a} "
        f"({ttft_b:.3f} vs {ttft_a:.3f}) | base A==B>{base_c}: "
        f"{base_a == base_b and base_a > base_c}",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
