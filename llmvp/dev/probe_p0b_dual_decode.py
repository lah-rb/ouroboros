#!/usr/bin/env python3
"""P0.b — in-process dual-model concurrent decode probe (MULTI_MODEL_PLAN.md).

Question: can two DIFFERENT models, each a single-context backend in ONE
process, decode simultaneously on Metal without errors or corruption?
Evidence gap this fills: the known-bad case was multi-context on one model
(shared MTLCommandQueue + unwired-weights CB race); the known-good case is
cross-process (LMStudio alongside LLMVP). Two models in one process is the
untested middle, and it decides Phase 2's decode policy:
    clean -> per-config `concurrent_decode_ok` allowlist
    dirty -> global cross-backend decode lock (strict alternation)

Method: load Olmo-32B + Devstral-24B (both greedy, temp 0). For each of
N prompts:
    solo A, solo A again (self-consistency control),
    solo B, solo B again,
    then A+B decoding CONCURRENTLY from two threads,
    then A+B strictly ALTERNATED (lock) for the throughput comparison.
Concurrent output is byte-compared against solo output per model. Any
mismatch on a self-consistent prompt, or any decode error, is a FAIL.

Run from llmvp/ under its venv, with the production server IDLE:
    .venv/bin/python dev/probe_p0b_dual_decode.py
"""

import asyncio
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from core.config import Config, set_config  # noqa: E402
from inference.backends.llama_cpp_backend import LlamaCppBackend  # noqa: E402

BASE = Path(__file__).resolve().parents[1]
PROBE_N_CTX = 8192
MAX_TOKENS = 128

QUESTIONS = [
    "Name the four Galilean moons of Jupiter.",
    "What is the capital of Australia, and what is it NOT?",
    "Explain the difference between a mutex and a semaphore in two sentences.",
    "List the first six prime numbers.",
    "What does the acronym RAID stand for in storage?",
    "Summarise the plot of Hamlet in one sentence.",
]

MODELS = {
    "olmo": {
        "config": BASE / "configs" / "olmo-3.1-32b-think.yaml",
        "wrap": lambda q: (
            f"<|im_start|>user\n{q}<|im_end|>\n<|im_start|>assistant\n<think>"
        ),
        "stops": ["<|im_end|>"],
    },
    "devstral": {
        "config": BASE / "configs" / "devstral-2-small-24b.yaml",
        "wrap": lambda q: f"[INST]{q}[/INST]",
        "stops": ["</s>"],
    },
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


class Rig:
    """One model: backend + pre-acquired instance + prompt shaping."""

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.backend = None
        self.inst = None

    async def up(self) -> None:
        cfg = load_probe_config(self.spec["config"])
        set_config(cfg)  # sequential init: global reads see this model
        self.backend = LlamaCppBackend(cfg)
        await self.backend.initialize()
        self.inst = await self.backend.acquire_instance()

    def decode(self, question: str) -> dict:
        """Greedy-decode one prompt; returns text/tokens/seconds or error."""
        prompt = self.spec["wrap"](question)
        tokens = self.inst.tokenize(prompt.encode("utf-8"), add_bos=True, special=True)
        t0 = time.perf_counter()
        try:
            pieces = list(
                self.backend.generate_stream_sync(
                    self.inst,
                    list(tokens),
                    max_tokens=MAX_TOKENS,
                    temperature=0.0,
                    stop_texts=self.spec["stops"],
                )
            )
        except Exception as exc:  # noqa: BLE001 — errors ARE the measurement
            return {"error": f"{type(exc).__name__}: {exc}", "s": 0.0}
        text = "".join(pieces)
        return {
            "text": text,
            "n": len(text),
            "s": time.perf_counter() - t0,
        }

    async def down(self) -> None:
        if self.inst is not None:
            await self.backend.release_instance(self.inst)
        if self.backend is not None:
            await self.backend.shutdown()


async def main() -> None:
    print(f"baseline wired: {wired_gb():.1f} GB")
    rigs = {}
    for name, spec in MODELS.items():
        t0 = time.perf_counter()
        rig = Rig(name, spec)
        await rig.up()
        rigs[name] = rig
        print(
            f"loaded {name}: {time.perf_counter() - t0:.1f}s, wired {wired_gb():.1f} GB"
        )

    a, b = rigs["olmo"], rigs["devstral"]
    pool = ThreadPoolExecutor(max_workers=2)
    loop = asyncio.get_running_loop()

    fails, table = [], []
    for i, q in enumerate(QUESTIONS):
        solo = {}
        for rig in (a, b):
            r1 = await loop.run_in_executor(pool, rig.decode, q)
            r2 = await loop.run_in_executor(pool, rig.decode, q)
            consistent = (
                "error" not in r1 and "error" not in r2 and r1["text"] == r2["text"]
            )
            solo[rig.name] = {"r": r1, "consistent": consistent}
            for r in (r1, r2):
                if "error" in r:
                    fails.append(f"q{i} solo {rig.name}: {r['error']}")

        t0 = time.perf_counter()
        fa = loop.run_in_executor(pool, a.decode, q)
        fb = loop.run_in_executor(pool, b.decode, q)
        ca, cb = await asyncio.gather(fa, fb)
        conc_wall = time.perf_counter() - t0

        t0 = time.perf_counter()
        aa = await loop.run_in_executor(pool, a.decode, q)
        ab = await loop.run_in_executor(pool, b.decode, q)
        alt_wall = time.perf_counter() - t0

        for rig, c in ((a, ca), (b, cb)):
            if "error" in c:
                fails.append(f"q{i} concurrent {rig.name}: {c['error']}")
            elif (
                solo[rig.name]["consistent"]
                and c["text"] != solo[rig.name]["r"]["text"]
            ):
                fails.append(
                    f"q{i} concurrent {rig.name}: OUTPUT DIVERGED from solo "
                    f"(solo consistent — this is corruption, not sampling)"
                )
        for r in (aa, ab):
            if "error" in r:
                fails.append(f"q{i} alternated: {r['error']}")

        solo_wall = solo[a.name]["r"]["s"] + solo[b.name]["r"]["s"]
        table.append((i, solo_wall, alt_wall, conc_wall))
        print(
            f"q{i}: solo-sum {solo_wall:5.1f}s | alternated {alt_wall:5.1f}s | "
            f"concurrent {conc_wall:5.1f}s | wired {wired_gb():.1f} GB"
        )

    print("\n--- verdict ---")
    if fails:
        print(f"DIRTY — {len(fails)} failure(s):")
        for f in fails:
            print(f"  {f}")
        print("=> Phase 2 needs the global cross-backend decode lock.")
    else:
        speedup = sum(t[1] for t in table) / max(sum(t[3] for t in table), 1e-9)
        print("CLEAN — zero errors, zero divergences across all rounds.")
        print(f"concurrent speedup vs solo-sum: {speedup:.2f}x")
        print(
            "=> per-config concurrent_decode_ok allowlist is viable for small models."
        )

    for rig in rigs.values():
        await rig.down()
    pool.shutdown()
    print(f"final wired after both teardowns: {wired_gb():.1f} GB")


if __name__ == "__main__":
    asyncio.run(main())
