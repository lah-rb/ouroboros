#!/usr/bin/env python3
"""Verify the resident-seq cache transition (Phase 0 + 1) on a real model.

Drives SessionManager + the backend directly, running the SAME multi-turn session in
two modes and reporting, per turn:
  - TTFT (time-to-first-token) ≈ prefill cost. Resident should stay FLAT across turns;
    legacy full_replay should CLIMB (it re-prefills the whole history each turn).
  - a memory probe: turn 0 plants a secret; the final turn asks for it — proves the
    resident live-KV correctly retains the conversation.
  - survival: N turns with no crash.

Usage (run from llmvp/):  .venv/bin/python dev/verify_resident_session.py <config> [n_turns]
"""
import asyncio
import sys
import time
from pathlib import Path

CONFIG = sys.argv[1] if len(sys.argv) > 1 else "gpt-oss-120b-a5"
N_TURNS = int(sys.argv[2]) if len(sys.argv) > 2 else 12
SECRET = "BANANA-42"


async def run_session(resident: bool, n_turns: int):
    from core.config import load_config, set_config

    cfg = load_config(Path("configs") / f"{CONFIG}.yaml")
    cfg.model.resident_seq_cache = resident
    if "strip" in sys.argv:
        cfg.model.resident_strip_reasoning = resident
    for a in sys.argv:  # nctx=N forces a small context to trigger windowing
        if a.startswith("nctx="):
            cfg.model.n_ctx = int(a.split("=", 1)[1])
    # Force eager single-instance so the session pins a deterministic context.
    cfg.resources.jit_concurrency_limit = None
    cfg.resources.max_concurrent_requests = 1
    set_config(cfg)

    from preprocessing.static_tokens import manager
    manager.load_static_buffer()

    from inference.backends.factory import create_backend
    from core.session_manager import SessionManager

    backend = create_backend(cfg)
    await backend.initialize()
    active = getattr(backend, "_resident_active", None)
    mode = f"resident={resident} _resident_active={active}"
    print(f"\n=== {mode} | full_replay={getattr(cfg.model,'session_full_replay',True)} ===", flush=True)

    sm = SessionManager(backend)
    info = await sm.start_session(ttl_seconds=600)
    sid = info.session_id

    ttfts, mem_ok = [], None
    for t in range(n_turns):
        if t == 0:
            prompt = f"Remember this code for later: {SECRET}. Acknowledge in one word."
        elif t == n_turns - 1:
            prompt = "What was the code I gave you at the start? Reply with just the code."
        else:
            prompt = f"Turn {t}: reply in one short sentence about the number {t}."
        t0 = time.time()
        first = None
        text = ""
        # Thinking models need room to finish CoT + answer (24 truncated mid-CoT).
        turn_max = 200 if t == n_turns - 1 else 80
        try:
            async for chunk in sm.session_turn(sid, prompt, max_tokens=turn_max, temperature=0.0):
                if first is None:
                    first = time.time() - t0
                text += chunk
        except Exception as e:  # noqa: BLE001
            print(f"  turn {t:>2}: CRASHED: {type(e).__name__}: {str(e)[:120]}", flush=True)
            ttfts.append(None)
            break
        ttfts.append(first)
        if t == n_turns - 1:
            mem_ok = SECRET.replace("-", "") in text.replace("-", "").upper()
        print(f"  turn {t:>2}: TTFT={first:6.2f}s  out={text.strip()[:46]!r}", flush=True)

    await sm.end_session(sid)
    await backend.shutdown()
    valid = [x for x in ttfts if x is not None]
    growth = (valid[-1] / valid[0]) if len(valid) >= 2 and valid[0] else float("nan")
    print(f"  -> TTFT first={valid[0]:.2f}s last={valid[-1]:.2f}s growth={growth:.2f}x "
          f"| memory_recall={'PASS' if mem_ok else 'FAIL'} | survived={len(valid)}/{n_turns} turns",
          flush=True)
    return ttfts, mem_ok


async def main():
    print(f"== resident-seq session verification: {CONFIG}, {N_TURNS} turns ==")
    resident_only = "resident-only" in sys.argv
    # Legacy first (full_replay default), then resident — fresh backend each.
    if not resident_only:
        await run_session(resident=False, n_turns=N_TURNS)
    await run_session(resident=True, n_turns=N_TURNS)
    print("\n(resident TTFT should be FLAT (~1x growth); legacy full_replay should CLIMB.)")


if __name__ == "__main__":
    asyncio.run(main())
