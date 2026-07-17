#!/usr/bin/env python3
"""P0.a — mid-process backend teardown/reinit probe (MULTI_MODEL_PLAN.md).

Question: does `LlamaCppBackend.shutdown()` actually release Metal wired
memory mid-process, and does a subsequent init of a DIFFERENT model in the
same process come up clean? This is the assumption Phase 1 `swapModel`
stands on; if it fails, the fallback is swap-by-respawn.

Sequence (three swap cycles in one process):
    1. Olmo-32B    cold load  -> decode -> teardown   (baseline swap)
    2. Devstral-24B cold load -> decode -> teardown   (different model)
    3. Olmo-32B    warm load  -> decode -> teardown   (page-cache reload,
       i.e. the swap-BACK latency a boss-turn cadence would pay)

Wired memory is sampled via vm_stat around every phase. Verdict criteria:
    - each teardown returns wired to within ~2 GB of the pre-load level
    - every reload decodes coherent text with zero errors
    - cycle-3 load time << cycle-1 load time (page cache held the GGUF)

Run from llmvp/ under its venv, with the production server IDLE:
    .venv/bin/python dev/probe_p0a_swap_teardown.py
"""

import asyncio
import gc
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from core.config import Config, set_config  # noqa: E402
from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402

BASE = Path(__file__).resolve().parents[1]

OLMO = BASE / "configs" / "olmo-3.1-32b-think.yaml"
DEVSTRAL = BASE / "configs" / "devstral-2-small-24b.yaml"

# Probe-sized contexts: KV budget is not what's under test.
PROBE_N_CTX = 8192

PROMPTS = {
    "chatml": (
        "<|im_start|>user\nName the four Galilean moons of Jupiter."
        "<|im_end|>\n<|im_start|>assistant\n<think>",
        ["<|im_end|>"],
    ),
    "tekken": (
        "[INST]Name the four Galilean moons of Jupiter.[/INST]",
        ["</s>"],
    ),
}


def wired_gb() -> float:
    out = subprocess.check_output(["vm_stat"], text=True)
    for line in out.splitlines():
        if "Pages wired" in line:
            return int(line.split()[-1].rstrip(".")) * 16384 / 1e9
    raise RuntimeError("vm_stat: no wired line")


def load_probe_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())
    raw["model"]["n_ctx"] = PROBE_N_CTX
    raw["graphql"]["max_tokens"] = PROBE_N_CTX
    raw["generation"]["max_tokens_default"] = 1024
    raw["logging"]["enabled"] = False
    return Config(**raw)


async def one_cycle(label: str, cfg_path: Path) -> dict:
    r: dict = {"label": label}
    cfg = load_probe_config(cfg_path)
    set_config(cfg)  # init-time reads (static tokens etc.) see this model

    r["wired_before"] = wired_gb()
    t0 = time.perf_counter()
    backend = LlamaCppBackend(cfg)
    await backend.initialize()
    r["load_s"] = time.perf_counter() - t0
    r["wired_hot"] = wired_gb()

    family = cfg.model.family
    prompt, stops = PROMPTS[family]
    inst = await backend.acquire_instance()
    try:
        tokens = inst.tokenize(prompt.encode("utf-8"), add_bos=True, special=True)
        t0 = time.perf_counter()
        text = "".join(
            backend.generate_stream_sync(
                inst, list(tokens), max_tokens=160, temperature=0.0, stop_texts=stops
            )
        )
        dt = time.perf_counter() - t0
    finally:
        await backend.release_instance(inst)

    r["decode_ok"] = bool(text.strip())
    r["decode_tail"] = text.strip()[-120:].replace("\n", " ")
    r["decode_s"] = dt

    t0 = time.perf_counter()
    await backend.shutdown()
    del backend
    gc.collect()
    await asyncio.sleep(2)  # let Metal settle before sampling
    r["teardown_s"] = time.perf_counter() - t0
    r["wired_after"] = wired_gb()
    return r


async def main() -> None:
    print(f"baseline wired: {wired_gb():.1f} GB")
    results = []
    for label, cfg in [
        ("olmo-cold", OLMO),
        ("devstral-cold", DEVSTRAL),
        ("olmo-warm", OLMO),
    ]:
        r = await one_cycle(label, cfg)
        results.append(r)
        print(
            f"[{r['label']:14s}] load {r['load_s']:6.1f}s | "
            f"wired {r['wired_before']:5.1f} -> {r['wired_hot']:5.1f} -> "
            f"{r['wired_after']:5.1f} GB | decode "
            f"{'OK' if r['decode_ok'] else 'EMPTY'} ({r['decode_s']:.1f}s)"
        )
        print(f"    tail: {r['decode_tail']}")

    print("\n--- verdict ---")
    leaks = [r["label"] for r in results if r["wired_after"] - r["wired_before"] > 2.0]
    print(f"teardown releases wired: {'YES' if not leaks else f'NO — {leaks}'}")
    print(f"all decodes ok:          {all(r['decode_ok'] for r in results)}")
    print(
        f"warm reload speedup:     {results[0]['load_s']:.1f}s cold -> "
        f"{results[2]['load_s']:.1f}s warm"
    )


if __name__ == "__main__":
    asyncio.run(main())
