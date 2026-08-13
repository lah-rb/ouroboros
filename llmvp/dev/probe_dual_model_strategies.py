#!/usr/bin/env python3
"""P0.b, RE-MEASURED — in-process dual-model decode on the current build.

WHY AGAIN. P0.b ran 2026-07-16 and returned DIRTY: one greedy divergence,
and concurrent wall == solo SUM on 5/6 questions (Metal serialized the two
models' kernels). That verdict parked Phase 2b co-residency and pushed
every "two models at once" answer toward cross-process.

It was measured on a llama.cpp build from BEFORE the b10131 -> b10243
rebuild. That rebuild cost DeepSeek-V4 a 4.2x decode factor — a stale
binary produced 44 graph splits on 43 layers and a throughput verdict that
was simply wrong. A concurrency verdict is no more durable than a
throughput one: both are properties of the binary underneath.

So this re-runs P0.b's protocol unchanged in shape, adds the numbers the
original did not report, and states the ratio in the same units as
`dev/gpu_contention_probe.py` so every concurrency measurement in the repo
is finally commensurable:

    serialization = (t_concurrent - max(tA,tB)) / ((tA+tB) - max(tA,tB))
        0.0 = free overlap (both models really ran at once)
        1.0 = fully serialized (Metal alternated them)

REFERENCE POINTS ALREADY ON RECORD
    in-process, 2 models   (P0.b, 2026-07-16)          ~1.0
    cross-process, 2 models (2026-07-17)               ~0.0
    cross-process, paddle x muse (2026-08-13)           0.339
    batched, 1 model 4 streams (muse)     19.4 -> 37.8 tok/s aggregate

WHAT IS MEASURED, per question, per model pair:
    solo A twice, solo B twice     — baselines AND self-consistency
    concurrent A+B (two threads)   — the strategy under test
    alternated A+B (decode lock)   — what Phase 2b would ship instead

Correctness gate is P0.b's and is not negotiable: a concurrent output that
differs from a SELF-CONSISTENT solo output is corruption, not sampling.
Aggregate throughput is reported too, because a strategy that costs each
stream latency but wins on total is still the right strategy when the
workload is a batch and not a chat.

Run from llmvp/ under its venv, with the production server IDLE:

    .venv/bin/python dev/probe_dual_model_strategies.py --max-tokens 128
    .venv/bin/python dev/probe_dual_model_strategies.py --max-tokens 512
"""

import argparse
import asyncio
import json
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
    # The production pair. Weights are 18 GB against 0.5 GB — a 36:1 ratio
    # where olmo/devstral is 2:1, which is exactly the variable in question:
    # a tiny model's kernels may interleave into a large model's decode gaps
    # in a way two comparable models cannot.
    "muse": {
        "config": BASE / "configs" / "muse-glimmer-30b.yaml",
        "wrap": lambda q: f"<|start|>user<|message|>{q}<|eot|><|start|>assistant",
        "stops": ["<|eot|>"],
    },
    "paddle": {
        "config": BASE / "configs" / "experiments" / "paddle-ocr-vl-probe.yaml",
        "wrap": lambda q: f"<|im_start|>user\n{q}<|im_end|>\n<|im_start|>assistant\n",
        "stops": ["<|im_end|>", "<|end▁of▁sentence|>"],
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
    # decode_mode is irrelevant here — this probe drives instances directly,
    # one sequence each. Pinned so a config edit cannot silently change the
    # topology under the measurement.
    raw.setdefault("resources", {})["decode_mode"] = "pool"
    raw["resources"]["max_concurrent_requests"] = 1
    return Config(**raw)


class Rig:
    """One model: backend + pre-acquired instance + prompt shaping."""

    def __init__(self, name: str, spec: dict, max_tokens: int):
        self.name = name
        self.spec = spec
        self.max_tokens = max_tokens
        self.backend = None
        self.inst = None

    async def up(self) -> None:
        cfg = load_probe_config(self.spec["config"])
        set_config(cfg)  # sequential init: global reads see this model
        self.backend = LlamaCppBackend(cfg)
        await self.backend.initialize()
        self.inst = await self.backend.acquire_instance()

    def reset(self) -> None:
        """Return seq 0 to its pristine static prefix BEFORE a timed decode.

        THE DEFECT THIS FIXES, and it is P0.b's too. `generate_stream_sync`
        runs `Llama.generate(reset=False)` — by design, so the static prefix
        established by `acquire_instance()` survives. But the probe reuses ONE
        instance for every arm of every question and never restored it, so the
        dynamic context ACCUMULATED across ~10 decodes x 6 questions. At
        max_tokens=128 that lands around 7k tokens and squeaks under the 8192
        ceiling. At 512 it is ~32k against 8192 — four times over, and the run
        went to 4x its predicted wall at 98% GPU while llama.cpp thrashed.

        It also explains the "sequence drift" the controls caught: the same
        prompt decoded at a different context depth sees a different KV state,
        so greedy picks differently. Divergence appeared only at q4/q5 — late,
        when the context was nearly full — and never at q0-q3. That is a
        measurement artifact end to end, not a property of concurrency, and
        P0.b's lone divergence is the same shape.

        `_resident_restore_static` is the backend's own per-request restore.
        It is private; a probe reaching for it is a deliberate trade, and it
        is correct for both modes — with no static prefix it degrades to
        clearing the working seq and zeroing n_tokens, i.e. a plain reset.
        """
        restore = getattr(self.backend, "_resident_restore_static", None)
        if restore is not None:
            restore(self.inst)
        else:  # pragma: no cover — older backends
            self.inst.reset()

    def decode(self, question: str) -> dict:
        """Greedy-decode one prompt; returns text/tokens/seconds or error."""
        prompt = self.spec["wrap"](question)
        tokens = self.inst.tokenize(prompt.encode("utf-8"), add_bos=True, special=True)
        # Outside the timer: state restoration is probe hygiene, not the
        # workload under measurement.
        self.reset()
        t0 = time.perf_counter()
        try:
            pieces = list(
                self.backend.generate_stream_sync(
                    self.inst,
                    list(tokens),
                    max_tokens=self.max_tokens,
                    temperature=0.0,
                    stop_texts=self.spec["stops"],
                )
            )
        except Exception as exc:  # noqa: BLE001 — errors ARE the measurement
            return {"error": f"{type(exc).__name__}: {exc}", "s": 0.0, "pieces": 0}
        text = "".join(pieces)
        return {
            "text": text,
            "n": len(text),
            "pieces": len(pieces),  # token-ish; the throughput numerator
            "s": time.perf_counter() - t0,
        }

    async def down(self) -> None:
        if self.inst is not None:
            await self.backend.release_instance(self.inst)
        if self.backend is not None:
            await self.backend.shutdown()


def serialization(t_conc: float, ta: float, tb: float) -> float:
    ideal, serial = max(ta, tb), ta + tb
    return (t_conc - ideal) / (serial - ideal) if serial > ideal else float("nan")


async def run_inproc(names: list, max_tokens: int):
    """Both models as two backend objects in THIS process, two threads."""
    rigs = {}
    for name in names:
        t0 = time.perf_counter()
        rig = Rig(name, MODELS[name], max_tokens)
        await rig.up()
        rigs[name] = rig
        print(
            f"loaded {name}: {time.perf_counter() - t0:.1f}s, wired {wired_gb():.1f} GB"
        )

    a, b = rigs[names[0]], rigs[names[1]]
    pool = ThreadPoolExecutor(max_workers=2)
    loop = asyncio.get_running_loop()

    async def dec_a(q):
        return await loop.run_in_executor(pool, a.decode, q)

    async def dec_b(q):
        return await loop.run_in_executor(pool, b.decode, q)

    rows, fails = await run_arms(dec_a, dec_b, names[0], names[1])
    return rows, fails, rigs


async def run_arms(dec_a, dec_b, name_a: str, name_b: str):
    """The arm protocol, topology-agnostic.

    ``dec_a``/``dec_b`` are async callables taking a question and returning
    the decode dict. In-process they wrap ``Rig.decode`` on a thread; across
    processes they round-trip a worker over a pipe. Sharing this function is
    what makes the two topologies COMPARABLE — an arm structure that drifted
    between them would make the whole comparison meaningless, which is the
    entire point of running it.
    """
    fails, rows = [], []
    decs = {name_a: dec_a, name_b: dec_b}
    for i, q in enumerate(QUESTIONS):
        solo = {}
        for nm, dec in decs.items():
            r1 = await dec(q)
            r2 = await dec(q)
            consistent = (
                "error" not in r1 and "error" not in r2 and r1["text"] == r2["text"]
            )
            solo[nm] = {"r": r1, "consistent": consistent}
            for r in (r1, r2):
                if "error" in r:
                    fails.append(f"q{i} solo {nm}: {r['error']}")

        t0 = time.perf_counter()
        ca, cb = await asyncio.gather(dec_a(q), dec_b(q))
        conc_wall = time.perf_counter() - t0

        t0 = time.perf_counter()
        aa = await dec_a(q)
        ab = await dec_b(q)
        alt_wall = time.perf_counter() - t0

        # THE CONTROL THE ORIGINAL P0.b LACKED. Its divergence check compared
        # the CONCURRENT decode (3rd on this instance) against the SOLO decode
        # (1st/2nd) and attributed any difference to concurrency. But the
        # instance is reused across every arm and every question, so a
        # difference at that position is equally consistent with accumulated
        # per-instance state — cache reuse, position drift, anything
        # sequence-dependent. Two more comparisons separate the hypotheses:
        #
        #   ALTERNATED diverges too  -> NOT concurrency (strictly serial arm)
        #   TRAILING SOLO diverges   -> NOT concurrency (nothing else running)
        #
        # Only a divergence unique to the concurrent arm is a concurrency bug.
        tail = {}
        for nm, dec in decs.items():
            tail[nm] = await dec(q)

        for nm, c, alt in ((name_a, ca, aa), (name_b, cb, ab)):
            base = solo[nm]
            if "error" in c:
                fails.append(f"q{i} concurrent {nm}: {c['error']}")
            if "error" in alt:
                fails.append(f"q{i} alternated {nm}: {alt['error']}")
            if not base["consistent"]:
                continue
            ref = base["r"]["text"]
            drift = {
                "concurrent": c.get("text") != ref,
                "alternated": alt.get("text") != ref,
                "trailing_solo": tail[nm].get("text") != ref,
            }
            if not any(drift.values()):
                continue
            if drift["concurrent"] and not (
                drift["alternated"] or drift["trailing_solo"]
            ):
                fails.append(
                    f"q{i} {nm}: CONCURRENCY CORRUPTION — concurrent "
                    f"diverged while alternated and trailing solo did not"
                )
            else:
                fails.append(
                    f"q{i} {nm}: sequence drift, NOT concurrency — "
                    f"diverged in {sorted(k for k, v in drift.items() if v)} "
                    f"(a serial arm diverged too, so concurrency is exonerated)"
                )

        ta, tb = solo[name_a]["r"]["s"], solo[name_b]["r"]["s"]
        toks = ca.get("pieces", 0) + cb.get("pieces", 0)
        rows.append(
            {
                "q": i,
                "solo_a_s": round(ta, 2),
                "solo_b_s": round(tb, 2),
                "solo_sum_s": round(ta + tb, 2),
                "alternated_s": round(alt_wall, 2),
                "concurrent_s": round(conc_wall, 2),
                "serialization": round(serialization(conc_wall, ta, tb), 3),
                # Aggregate throughput is the reason to want this at all.
                "solo_tps": (
                    round(
                        (
                            solo[name_a]["r"].get("pieces", 0)
                            + solo[name_b]["r"].get("pieces", 0)
                        )
                        / (ta + tb),
                        2,
                    )
                    if (ta + tb)
                    else None
                ),
                "concurrent_tps": round(toks / conc_wall, 2) if conc_wall else None,
            }
        )
        r = rows[-1]
        print(
            f"q{i}: solo-sum {r['solo_sum_s']:6.1f}s | alt {r['alternated_s']:6.1f}s | "
            f"conc {r['concurrent_s']:6.1f}s | serialization {r['serialization']:5.2f} | "
            f"tps {r['solo_tps']} -> {r['concurrent_tps']} | wired {wired_gb():.1f} GB"
        )
    return rows, fails


# ── Cross-process topology ────────────────────────────────────────────
#
# THE CONTROL THIS EXISTS FOR. In-process olmo+devstral measured 0.619 and
# muse+devstral 0.603, while the cross-process reference on record is 0.342
# — but that number came from a DIFFERENT pair doing a DIFFERENT workload
# (paddle OCR against muse text). Two variables moved at once, so it cannot
# be read as "separate processes overlap better". Running the SAME pair,
# same questions, same arm protocol, in two processes isolates topology.
#
# The worker is this same file under --worker, so Rig, the reset discipline
# and MODELS cannot drift between the two topologies.


async def worker_main(name: str, max_tokens: int) -> None:
    """One model in its own process, answering decode requests on stdin."""
    rig = Rig(name, MODELS[name], max_tokens)
    await rig.up()
    print(json.dumps({"ready": name}), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if req.get("stop"):
            break
        print(json.dumps(rig.decode(req["q"])), flush=True)
    await rig.down()


async def spawn_worker(name: str, max_tokens: int):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        name,
        "--max-tokens",
        str(max_tokens),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    while True:  # skip loader chatter until the ready line
        raw = await proc.stdout.readline()
        if not raw:
            raise RuntimeError(f"worker {name} died before ready")
        try:
            if json.loads(raw.decode()).get("ready"):
                return proc
        except json.JSONDecodeError:
            continue


def worker_decoder(proc):
    """Async decode callable over the pipe.

    ta/tb come from the WORKER's own timer (IPC excluded), matching the
    in-process probe; only the concurrent arm's wall is measured by the
    parent, so pipe overhead inflates it slightly. That biases serialization
    UPWARD — against the cross-process case — which is the safe direction.
    """

    async def dec(q):
        proc.stdin.write((json.dumps({"q": q}) + "\n").encode())
        await proc.stdin.drain()
        raw = await proc.stdout.readline()
        if not raw:
            return {"error": "worker died", "s": 0.0, "pieces": 0, "text": ""}
        return json.loads(raw.decode())

    return dec


async def run_xproc(names: list, max_tokens: int):
    procs = []
    for nm in names:
        t0 = time.perf_counter()
        procs.append(await spawn_worker(nm, max_tokens))
        print(
            f"spawned {nm}: {time.perf_counter() - t0:.1f}s, wired {wired_gb():.1f} GB"
        )
    da, db = worker_decoder(procs[0]), worker_decoder(procs[1])
    rows, fails = await run_arms(da, db, names[0], names[1])
    for p in procs:
        try:
            p.stdin.write(b'{"stop":true}\n')
            await p.stdin.drain()
            await asyncio.wait_for(p.wait(), timeout=60)
        except Exception:  # noqa: BLE001 — teardown must not fail a result
            p.kill()
    return rows, fails


def report(names, max_tokens, topology, rows, fails, out) -> dict:
    ok = [r for r in rows if r["serialization"] == r["serialization"]]  # drop NaN
    med = sorted(x["serialization"] for x in ok)[len(ok) // 2] if ok else float("nan")
    agg_solo = sum(r["solo_sum_s"] for r in rows)
    agg_conc = sum(r["concurrent_s"] for r in rows)
    verdict = {
        "pair": names,
        "topology": topology,
        "max_tokens": max_tokens,
        "questions": len(rows),
        "median_serialization": round(med, 3),
        "total_solo_sum_s": round(agg_solo, 1),
        "total_concurrent_s": round(agg_conc, 1),
        "wall_saved_pct": round((agg_solo - agg_conc) / agg_solo * 100, 1),
        "failures": fails,
        "clean": not fails,
        "rows": rows,
    }

    print(f"\n--- verdict ({topology}) ---")
    print(f"median serialization  {med:.3f}   (0 = free overlap, 1 = serialized)")
    print(
        f"total solo-sum {agg_solo:.1f}s -> concurrent {agg_conc:.1f}s "
        f"({verdict['wall_saved_pct']}% saved)"
    )
    if fails:
        print(f"DIRTY — {len(fails)} failure(s):")
        for f in fails:
            print(f"  {f}")
    else:
        print("CLEAN — no decode errors, no divergence from self-consistent solo")
    print(f"wired after teardown: {wired_gb():.1f} GB")

    if out:
        Path(out).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
        print(f"-> {out}")
    return verdict


async def entry() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-tokens", type=int, default=128)
    ap.add_argument(
        "--pair",
        default="olmo,devstral",
        help="two names from MODELS (default: July's pair, for comparability)",
    )
    ap.add_argument(
        "--xproc",
        action="store_true",
        help="run the pair in TWO PROCESSES instead of one (topology control)",
    )
    ap.add_argument("--worker", default="", help="internal: serve one model on stdin")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if args.worker:
        await worker_main(args.worker, args.max_tokens)
        return

    names = [n.strip() for n in args.pair.split(",") if n.strip()]
    if len(names) != 2 or any(n not in MODELS for n in names):
        sys.exit(f"--pair needs two of {sorted(MODELS)}; got {args.pair!r}")

    topology = "cross-process" if args.xproc else "in-process"
    print(
        f"pair={names[0]}+{names[1]}  topology={topology}  "
        f"max_tokens={args.max_tokens}  baseline wired: {wired_gb():.1f} GB"
    )

    if args.xproc:
        rows, fails = await run_xproc(names, args.max_tokens)
    else:
        rows, fails, rigs = await run_inproc(names, args.max_tokens)
        for rig in rigs.values():
            await rig.down()
    report(names, args.max_tokens, topology, rows, fails, args.out)


if __name__ == "__main__":
    asyncio.run(entry())
