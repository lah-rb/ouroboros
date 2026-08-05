#!/usr/bin/env python3
"""Batched-decode determinism/parity harness (Stage 1 acceptance).

Runs N completions of the SAME prompt at near-greedy temperature and a
fixed seed through the in-process backend, in either decode mode, and
prints the generated texts as JSON. A driver (or a human) compares:

  1. pool(1)  vs batched(W=1)  — token-for-token parity (the sampler +
     step-loop reimplementation produces what Llama.generate produced)
  2. batched(W=N) concurrent identical streams — mutual equality
     (stream isolation: concurrent seqs don't bleed into each other)

Runs one mode per PROCESS (clean global config/backend state):

  cd llmvp
  .venv/bin/python ../dev/batched_parity.py --mode pool    --streams 1
  .venv/bin/python ../dev/batched_parity.py --mode batched --streams 1
  .venv/bin/python ../dev/batched_parity.py --mode batched --streams 3

Uses devstral-2-small-24b (dense, resident-capable, ~12GB) so the
production a5 server is untouched. temperature=1e-6 ≈ greedy: robust to
sub-argmax-gap numeric differences from different prefill chunkings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

_LLMVP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "llmvp"
)

PROMPT = (
    "List the first eight prime numbers, then explain in two sentences why "
    "the sieve of Eratosthenes terminates."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["pool", "batched"], required=True)
    ap.add_argument("--streams", type=int, default=1)
    ap.add_argument("--config", default="devstral-2-small-24b")
    ap.add_argument("--max-tokens", type=int, default=96)
    ap.add_argument("--seed", type=int, default=12345)
    ap.add_argument(
        "--session-turns",
        type=int,
        default=0,
        help="run N-turn pinned sessions instead of stateless completions "
        "(--streams concurrent sessions, turns interleaved)",
    )
    args = ap.parse_args()

    os.chdir(_LLMVP)
    sys.path.insert(0, _LLMVP)

    import yaml

    from core.config import Config, resolve_config_path, set_config

    # resolve_config_path, not CONFIGS_DIR / name: the direct open silently
    # failed for any config reorganised into experiments/ or archive/ (P3,
    # dev/caching/CORPUS.md).
    with open(resolve_config_path(args.config), encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw["resources"]["decode_mode"] = args.mode
    raw["resources"]["max_concurrent_requests"] = (
        args.streams if args.mode == "batched" else 1
    )
    raw["model"]["seed"] = args.seed
    raw["logging"] = {"enabled": False}
    config = Config(**raw)
    set_config(config)

    from core.inference import _get_backend, run_completion

    TURN_PROMPTS = [
        "Name three noble gases.",
        "Which of those three has the lowest atomic number, and what is it?",
        "Write one sentence using that element's name figuratively.",
    ]

    async def run() -> dict:
        t0 = time.monotonic()
        backend = await _get_backend()
        boot_s = time.monotonic() - t0

        async def one(i: int):
            t = time.monotonic()
            outcome = await run_completion(
                PROMPT, max_tokens=args.max_tokens, temperature=1e-6
            )
            return {
                "i": i,
                "text": outcome.text,
                "tokens": outcome.tokens_generated,
                "fresh_prefill": outcome.fresh_prefill_tokens,
                "cached_prefix": outcome.cached_prefix_tokens,
                "wall_s": round(time.monotonic() - t, 2),
            }

        async def one_session(i: int):
            """A pinned N-turn session; a barrier per turn keeps the
            concurrent sessions' turns genuinely interleaved."""
            mgr = _session_managers[i]
            info = await mgr.start_session(ttl_seconds=600)
            transcript = []
            t = time.monotonic()
            for turn in range(args.session_turns):
                prompt = TURN_PROMPTS[turn % len(TURN_PROMPTS)]
                text, n_tok, _meta = await mgr.session_turn_complete(
                    info.session_id,
                    prompt,
                    max_tokens=args.max_tokens,
                    temperature=1e-6,
                )
                transcript.append(
                    {"turn": turn, "prompt": prompt, "text": text, "tokens": n_tok}
                )
                await _turn_barriers[turn].wait()
            await mgr.end_session(info.session_id)
            return {
                "i": i,
                "transcript": transcript,
                "wall_s": round(time.monotonic() - t, 2),
            }

        t1 = time.monotonic()
        if args.session_turns:
            from core.session_manager import SessionManager

            # One manager (shared backend) is the production shape.
            shared_mgr = SessionManager(backend)
            _session_managers = {i: shared_mgr for i in range(args.streams)}
            _turn_barriers = [
                asyncio.Barrier(args.streams) for _ in range(args.session_turns)
            ]
            results = await asyncio.gather(
                *(one_session(i) for i in range(args.streams))
            )
        else:
            results = await asyncio.gather(*(one(i) for i in range(args.streams)))
        gen_wall = time.monotonic() - t1

        health = backend.get_health_status()
        out = {
            "mode": args.mode,
            "streams": args.streams,
            "boot_s": round(boot_s, 1),
            "concurrent_wall_s": round(gen_wall, 2),
            "results": sorted(results, key=lambda r: r["i"]),
            "engine": health.get("batched_engine"),
            "status": health.get("status"),
        }
        await backend.shutdown()
        return out

    print(json.dumps(asyncio.run(run()), indent=1))
    sys.stdout.flush()
    # Skip C++ exit finalizers: ggml-metal's residency-set teardown asserts
    # (rsets count != 0) in atexit on this build — an upstream teardown-order
    # wart, not a runtime failure. The run's work is already printed.
    os._exit(0)


if __name__ == "__main__":
    main()
