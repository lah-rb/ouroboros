#!/usr/bin/env python3
"""JIT identity check for the resident-seq cache.

In JIT mode, shared instances are spawned on-demand and each must get the SEQ_STATIC
pin at warm-up. This forces a scale-up (hold the primary, acquire a second) so we have
the primary + a JIT-spawned instance, then runs the SAME temp=0 stateless completion on
both — they must be byte-identical (the fork is consistent across JIT-spawned contexts).
Also runs the legacy (resident off) path for the static-fork == load_state bit-identity.

Usage (from llmvp/):  .venv/bin/python dev/verify_resident_jit.py <config> ["prompt"]
"""
import asyncio
import sys
from pathlib import Path

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "devstral-2-small-24b"
PROMPT = sys.argv[2] if len(sys.argv) > 2 else "List three prime numbers under 20."


async def run(resident: bool):
    from core.config import load_config, set_config

    cfg = load_config(Path("configs") / f"{CONFIG}.yaml")
    cfg.model.resident_seq_cache = resident
    cfg.resources.jit_concurrency_limit = 2      # JIT mode, up to 2 instances
    cfg.resources.max_concurrent_requests = 2
    set_config(cfg)

    from preprocessing.static_tokens import manager
    manager.load_static_buffer()

    from inference.backends.factory import create_backend
    from core.session_manager import SessionManager

    backend = create_backend(cfg)
    await backend.initialize()
    active = getattr(backend, "_resident_active", None)
    print(f"\n=== resident={resident} _resident_active={active} (JIT limit=2) ===", flush=True)

    sm = SessionManager(backend)

    async def first_turn(sid):
        return "".join([c async for c in sm.session_turn(sid, PROMPT, 32, 0.0)]).strip()

    # Two sessions: the 2nd start_session acquire finds the queue empty and forces
    # a JIT batch scale-up — spawning + warming a new instance (SEQ_STATIC pin) that
    # session 2 pins. Both run the SAME framed turn → outputs must be identical.
    info1 = await sm.start_session(600)
    info2 = await sm.start_session(600)
    spawned = len(backend._all_instances)
    out_a = await first_turn(info1.session_id)
    out_b = await first_turn(info2.session_id)
    await sm.end_session(info1.session_id)
    await sm.end_session(info2.session_id)
    await backend.shutdown()

    print(f"  instances spawned={spawned}", flush=True)
    print(f"  primary     out={out_a[:60]!r}", flush=True)
    print(f"  jit-spawned out={out_b[:60]!r}", flush=True)
    print(f"  -> primary==jit_spawned: {out_a == out_b}", flush=True)
    return out_a, out_b


async def main():
    print(f"== resident JIT identity: {CONFIG} | prompt={PROMPT!r} ==")
    leg_a, leg_b = await run(resident=False)
    res_a, res_b = await run(resident=True)
    print("\n== VERDICT ==")
    print(f"  legacy   primary==spawned : {leg_a == leg_b}")
    print(f"  resident primary==spawned : {res_a == res_b}")
    print(f"  resident == legacy (fork==load_state): {res_a == leg_a}")


if __name__ == "__main__":
    asyncio.run(main())
