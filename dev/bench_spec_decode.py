#!/usr/bin/env python3
"""Speculative decoding for muse-glimmer-30b on the two-3090 rig (2026-10-02).

WHY. Muse decodes ~20 tok/s per stream and every lane waits on it. Muse ships
a DFlash drafter (models/dflash-kquant.gguf: 2.6B, 5 blocks, block size 16,
reading the target's hidden states at layers 2/14/26/38/50). LLMVP's Python
binding cannot drive llama.cpp's speculative path (common_speculative is not
in the C API; see the llmvp-binding memory), so this bench runs llama.cpp's
own llama-server and asks the one question that decides whether it is worth
wiring in: how fast does muse DECODE with and without the drafter, on one
card, layer split and tensor split (the operator's three placements)?

PROMPTS are production's, byte for byte: muse's own format (LLMVP's
renderer), the SOUL.md persona, reasoning "low", and three real workloads
from two papers each -- a pack window (curator/pack_data, the repack and
window-repair work), a curator review, a translation chunk -- at production
temperatures (0.4 curate turns, 0.3 translation; top_p 0.95, top_k 64).

MEASURES, per placement x {spec off, dflash}: single-stream decode tok/s
(llama-server's own timings) and draft acceptance; an aggregate tok/s with 4
concurrent streams (production is batched); and, at temperature 0, whether
the speculative output is identical to the plain one (lossless check).

    .venv/bin/python dev/bench_spec_decode.py prompts
    .venv/bin/python dev/bench_spec_decode.py run [--arms single-off,single-dflash,...] [--n-max 3]
    .venv/bin/python dev/bench_spec_decode.py report
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
OUT = Path(os.path.expanduser(os.environ.get("SPEC_BENCH_OUT", "~/tmp/spec_bench")))
LLAMA = Path(os.path.expanduser("~/Repos/llama.cpp/build/bin/llama-server"))
MUSE = os.path.expanduser("~/models/muse-glimmer-30B-kquant-dynamic.gguf")
DRAFT = os.path.expanduser("~/models/dflash-kquant.gguf")
CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
PORT = 8091
CTX = 16384
LIBS = (
    f"{os.path.expanduser('~')}/cuda-libs/nvidia/cublas/lib:"
    f"{os.path.expanduser('~')}/cuda-libs/nvidia/cuda_runtime/lib"
)
PLACEMENTS = {
    "single": ["-sm", "none", "-dev", "CUDA1"],
    "layer": ["-sm", "layer", "-ts", "1,1"],
    "tensor": ["-sm", "tensor", "-ts", "1,1"],
}
SAMPLING = {"pack": 0.4, "review": 0.4, "translate": 0.3}
MAX_TOKENS = 1024


# ── prompts ───────────────────────────────────────────────────────────


async def build_prompts() -> list[dict]:
    from agent.actions import curation_actions as ca
    from agent.actions import translation_actions as ta
    from agent.actions.pack_windows import window_sections
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.local import LocalEffects

    sys.path.insert(0, str(REPO / "llmvp"))
    from formats.registry import get_renderer

    r = get_renderer("muse-glimmer")
    persona = (REPO / "llmvp" / "knowledge" / "SOUL.md").read_text(encoding="utf-8")
    system = r.render_system(persona=persona, reasoning="low")
    gen = r.render_generation_prompt(reasoning="low")

    def frame(body: str) -> str:
        return system + r.render_user(body) + gen

    fx = LocalEffects(CORPUS)
    bank = await read_databank(fx)
    registry = await ca._load_registry(fx)
    packed = sorted(
        k
        for k, x in bank.items()
        if x.get("pack_status") == "packed" and x.get("review_status") == "accepted"
    )
    out: list[dict] = []
    target, cap = ca._pack_window_sizes()
    for key in packed[::97]:  # spread across the corpus, deterministic
        if sum(p["workload"] == "pack" for p in out) >= 2:
            break
        doc = await ca._raw_curator_doc(fx, key)
        ws = window_sections(doc, target, cap)
        w = next((w for w in ws if 6000 <= w.tokens <= 11000), None)
        if w is None:
            continue
        pack_prompt = await ca._render_prompt(
            "curator/pack_data",
            {
                "key_registry_block": ca.format_key_registry(registry),
                "gate_feedback": "",
            },
        )
        preface = ca._PACK_PREFACE.format(
            n=w.index + 1,
            total=len(ws),
            sections=w.section_count,
            heading=w.first_heading,
        )
        out.append({"id": f"pack-{key[:40]}", "workload": "pack",
                    "prompt": frame(preface + w.text + "\n\n---\n\n" + pack_prompt)})  # fmt: skip
    review_prompt = await ca._render_prompt(
        "curator/review_paper", {"corpus_subject": await ca._corpus_subject(fx)}
    )
    for key in packed[::131]:
        if sum(p["workload"] == "review" for p in out) >= 2:
            break
        doc = await ca._raw_curator_doc(fx, key)
        if not 15000 <= len(doc) <= 40000:
            continue
        out.append({"id": f"review-{key[:40]}", "workload": "review",
                    "prompt": frame(doc + "\n\n---\n\n" + review_prompt)})  # fmt: skip
    for key, x in sorted(bank.items()):
        if sum(p["workload"] == "translate" for p in out) >= 2:
            break
        if not x.get("translated"):
            continue
        md = Path(CORPUS) / "databank" / "markdown" / f"{key}.md"
        if not md.exists():
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        chunk = text[4000:9000]
        if len(chunk) < 4000:
            continue
        out.append({"id": f"translate-{key[:40]}", "workload": "translate",
                    "prompt": frame(ta._render_translate_prompt(chunk, ""))})  # fmt: skip
    return out


# ── server ────────────────────────────────────────────────────────────


def start_server(
    placement: str, spec: bool, n_max: int, slots: int
) -> subprocess.Popen:
    cmd = [str(LLAMA), "-m", MUSE, "-c", str(CTX * slots), "-ngl", "999", "-fa", "on",
           "-np", str(slots), "--port", str(PORT), "--no-webui", *PLACEMENTS[placement]]  # fmt: skip
    if spec:
        cmd += ["--spec-type", "draft-dflash", "-md", DRAFT, "-ngld", "all",
                "--spec-draft-n-max", str(n_max)]  # fmt: skip
        if placement == "single":
            cmd += ["-devd", "CUDA1"]
    log = open(
        OUT / f"server_{placement}_{'dflash' if spec else 'off'}_np{slots}.log", "w"
    )
    env = dict(os.environ, LD_LIBRARY_PATH=LIBS)
    p = subprocess.Popen(
        cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True
    )
    t0 = time.time()
    while time.time() - t0 < 600:
        if p.poll() is not None:
            raise RuntimeError(f"server exited ({p.returncode}); see {log.name}")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=5
            ) as r:
                if json.load(r).get("status") == "ok":
                    return p
        except Exception:  # noqa: BLE001 -- loading
            pass
        time.sleep(3)
    raise RuntimeError("server never became healthy")


def stop_server(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=120)
    except Exception:  # noqa: BLE001
        os.killpg(p.pid, signal.SIGKILL)


def complete(
    prompt: str, temperature: float, seed: int = 7, n_predict: int = MAX_TOKENS
) -> dict:
    body = {"prompt": prompt, "n_predict": n_predict, "temperature": temperature,
            "top_p": 0.95, "top_k": 64, "seed": seed, "cache_prompt": False}  # fmt: skip
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/completion",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=1800) as r:
        d = json.load(r)
    d["wall_s"] = time.time() - t0
    return d


def gpu_mem() -> list[int]:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout  # fmt: skip
    return [int(x) for x in out.split()]


def run_arm(arm: str, prompts: list[dict], n_max: int) -> list[dict]:
    placement, mode = arm.split("-")
    spec = mode == "dflash"
    rows: list[dict] = []
    # single stream: every prompt at its production temperature, plus temp-0 copies
    p = start_server(placement, spec, n_max, 1)
    try:
        mem = gpu_mem()
        complete(prompts[0]["prompt"][:2000], 0.0)  # warm-up, not recorded
        for pr in prompts:
            for temp, tag in ((SAMPLING[pr["workload"]], "prod"), (0.0, "greedy")):
                d = complete(pr["prompt"], temp)
                t = d.get("timings") or {}
                rows.append({"arm": arm, "n_max": n_max if spec else 0, "streams": 1, "id": pr["id"],
                             "workload": pr["workload"], "sampling": tag, "temperature": temp,
                             "prompt_n": t.get("prompt_n"), "predicted_n": t.get("predicted_n"),
                             "decode_tps": t.get("predicted_per_second"),
                             "prefill_tps": t.get("prompt_per_second"),
                             "draft_n": t.get("draft_n"), "draft_accepted": t.get("draft_n_accepted"),
                             "text": d.get("content", ""), "gpu_mib": mem})  # fmt: skip
                print(
                    json.dumps({k: v for k, v in rows[-1].items() if k != "text"}),
                    flush=True,
                )
    finally:
        stop_server(p)
    # four concurrent streams: aggregate decode throughput. Its own failure
    # (four 16k slots may not fit one card beside the drafter) is recorded,
    # never allowed to discard the single-stream rows above.
    try:
        p = start_server(placement, spec, n_max, 4)
    except Exception as e:  # noqa: BLE001
        rows.append({"arm": arm, "n_max": n_max if spec else 0, "streams": 4, "id": "batch4",
                     "error": str(e)[:200]})  # fmt: skip
        return rows
    try:
        complete(prompts[0]["prompt"][:2000], 0.0)
        batch = [pr for pr in prompts if pr["workload"] in ("pack", "translate")][:4]
        t0 = time.time()
        with ThreadPoolExecutor(4) as ex:
            res = list(
                ex.map(
                    lambda pr: complete(pr["prompt"], SAMPLING[pr["workload"]]), batch
                )
            )
        wall = time.time() - t0
        gen = sum((d.get("timings") or {}).get("predicted_n", 0) for d in res)
        dec = max((d.get("timings") or {}).get("predicted_ms", 0) for d in res) / 1000
        rows.append({"arm": arm, "n_max": n_max if spec else 0, "streams": 4, "id": "batch4",
                     "workload": "mixed", "sampling": "prod", "generated": gen, "wall_s": round(wall, 1),
                     "aggregate_decode_tps": round(gen / dec, 1) if dec else None,
                     "draft_n": sum((d.get("timings") or {}).get("draft_n", 0) or 0 for d in res),
                     "draft_accepted": sum((d.get("timings") or {}).get("draft_n_accepted", 0) or 0 for d in res)})  # fmt: skip
        print(json.dumps(rows[-1]), flush=True)
    finally:
        stop_server(p)
    return rows


# ── follow-ups: draft-length sweep at 1 and 8 streams; pack quality ──


def run_sweep(
    arm: str, prompts: list[dict], n_max: int, streams_list=(1, 8)
) -> list[dict]:
    placement, mode = arm.split("-")
    spec = mode == "dflash"
    rows: list[dict] = []
    for streams in streams_list:
        p = start_server(placement, spec, n_max, streams)
        try:
            complete(prompts[0]["prompt"][:2000], 0.0)
            if streams == 1:
                for pr in [
                    x for x in prompts if x["workload"] in ("pack", "translate")
                ]:
                    d = complete(pr["prompt"], SAMPLING[pr["workload"]])
                    t = d.get("timings") or {}
                    rows.append({"phase": "sweep", "arm": arm, "n_max": n_max if spec else 0, "streams": 1,
                                 "id": pr["id"], "workload": pr["workload"], "decode_tps": t.get("predicted_per_second"),
                                 "draft_n": t.get("draft_n"), "draft_accepted": t.get("draft_n_accepted")})  # fmt: skip
                    print(json.dumps(rows[-1]), flush=True)
            else:
                batch = (prompts * 4)[:streams]
                with ThreadPoolExecutor(streams) as ex:
                    res = list(
                        ex.map(
                            lambda pr: complete(pr["prompt"], SAMPLING[pr["workload"]]),
                            batch,
                        )
                    )
                gen = sum((d.get("timings") or {}).get("predicted_n", 0) for d in res)
                dec = (
                    max((d.get("timings") or {}).get("predicted_ms", 0) for d in res)
                    / 1000
                )
                rows.append({"phase": "sweep", "arm": arm, "n_max": n_max if spec else 0, "streams": streams,
                             "aggregate_decode_tps": round(gen / dec, 1) if dec else None,
                             "draft_n": sum((d.get("timings") or {}).get("draft_n", 0) or 0 for d in res),
                             "draft_accepted": sum((d.get("timings") or {}).get("draft_n_accepted", 0) or 0 for d in res)})  # fmt: skip
                print(json.dumps(rows[-1]), flush=True)
        finally:
            stop_server(p)
    return rows


async def build_pack_prompts(n: int) -> list[dict]:
    """n pack windows from n different papers (6-11k tokens), production-framed."""
    from agent.actions import curation_actions as ca
    from agent.actions.pack_windows import window_sections
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.local import LocalEffects

    sys.path.insert(0, str(REPO / "llmvp"))
    from formats.registry import get_renderer

    r = get_renderer("muse-glimmer")
    persona = (REPO / "llmvp" / "knowledge" / "SOUL.md").read_text(encoding="utf-8")
    system = r.render_system(persona=persona, reasoning="low")
    gen = r.render_generation_prompt(reasoning="low")
    fx = LocalEffects(CORPUS)
    bank = await read_databank(fx)
    registry = await ca._load_registry(fx)
    target, cap = ca._pack_window_sizes()
    pack_prompt = await ca._render_prompt(
        "curator/pack_data",
        {"key_registry_block": ca.format_key_registry(registry), "gate_feedback": ""},
    )
    keys = sorted(k for k, x in bank.items() if x.get("pack_status") == "packed")
    out = []
    for key in keys[13::61]:
        if len(out) >= n:
            break
        doc = await ca._raw_curator_doc(fx, key)
        ws = window_sections(doc, target, cap)
        w = next((w for w in ws if 4000 <= w.tokens <= 11000), None)
        if w is None:
            continue
        pre = ca._PACK_PREFACE.format(n=w.index + 1, total=len(ws), sections=w.section_count,
                                      heading=w.first_heading) if len(ws) > 1 else ""  # fmt: skip
        body = pre + w.text + "\n\n---\n\n" + pack_prompt
        out.append({"id": f"pack-{key[:40]}", "workload": "pack", "window": w.text,
                    "prompt": system + r.render_user(body) + gen})  # fmt: skip
    return out


def run_quality(arm: str, packs: list[dict], n_max: int, seeds=(1, 2)) -> list[dict]:
    placement, mode = arm.split("-")
    spec = mode == "dflash"
    rows = []
    p = start_server(placement, spec, n_max, 1)
    try:
        complete(packs[0]["prompt"][:2000], 0.0)
        for pr in packs:
            for seed in seeds:
                d = complete(pr["prompt"], 0.4, seed=seed, n_predict=8192)
                t = d.get("timings") or {}
                rows.append({"phase": "quality", "arm": arm, "n_max": n_max if spec else 0, "id": pr["id"],
                             "seed": seed, "predicted_n": t.get("predicted_n"),
                             "decode_tps": t.get("predicted_per_second"), "text": d.get("content", "")})  # fmt: skip
                print(
                    json.dumps({k: v for k, v in rows[-1].items() if k != "text"}),
                    flush=True,
                )
    finally:
        stop_server(p)
    return rows


def report() -> None:
    rows = [
        json.loads(x)
        for x in (OUT / "results.jsonl").read_text().splitlines()
        if x.strip()
    ]
    import statistics as st

    arms = sorted({(r["arm"], r.get("n_max", 0)) for r in rows})
    print(
        f"{'arm':<16}{'n_max':>6}{'decode prod':>13}{'decode greedy':>15}{'accept':>8}{'4-stream agg':>14}"
    )
    base = {}
    for arm, nm in arms:
        rs = [r for r in rows if r["arm"] == arm and r.get("n_max", 0) == nm]
        prod = [
            r["decode_tps"]
            for r in rs
            if r.get("streams") == 1 and r["sampling"] == "prod" and r.get("decode_tps")
        ]
        greedy = [
            r["decode_tps"]
            for r in rs
            if r.get("streams") == 1
            and r["sampling"] == "greedy"
            and r.get("decode_tps")
        ]
        dn = sum(r.get("draft_n") or 0 for r in rs if r.get("streams") == 1)
        da = sum(r.get("draft_accepted") or 0 for r in rs if r.get("streams") == 1)
        agg = next(
            (r.get("aggregate_decode_tps") for r in rs if r.get("streams") == 4), None
        )
        print(f"{arm:<16}{nm:>6}{st.median(prod) if prod else 0:>13.1f}{st.median(greedy) if greedy else 0:>15.1f}"
              f"{(da / dn if dn else 0):>8.0%}{agg or 0:>14.1f}")  # fmt: skip
        if arm.endswith("-off"):
            base[arm.split("-")[0]] = {
                r["id"]: r["text"] for r in rs if r.get("sampling") == "greedy"
            }
    print("\ngreedy outputs identical to the same placement's plain run:")
    for arm, nm in arms:
        if arm.endswith("-off"):
            continue
        ref = base.get(arm.split("-")[0], {})
        rs = [
            r
            for r in rows
            if r["arm"] == arm
            and r.get("n_max", 0) == nm
            and r.get("sampling") == "greedy"
        ]
        same = sum(1 for r in rs if ref.get(r["id"]) == r["text"])
        print(f"  {arm} n_max {nm}: {same}/{len(rs)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("prompts", "run", "report", "sweep", "quality"))
    ap.add_argument(
        "--arms",
        default="single-off,single-dflash,layer-off,layer-dflash,tensor-off,tensor-dflash",
    )
    ap.add_argument("--n-max", type=int, default=3)
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "prompts":
        ps = asyncio.run(build_prompts())
        (OUT / "prompts.json").write_text(json.dumps(ps, ensure_ascii=False))
        for p in ps:
            print(p["id"], p["workload"], len(p["prompt"]), "chars")
        return 0
    if a.cmd == "report":
        report()
        return 0
    if a.cmd == "sweep":
        prompts = json.loads((OUT / "prompts.json").read_text())
        for arm, nm in [
            ("tensor-off", 0),
            ("tensor-dflash", 3),
            ("tensor-dflash", 7),
            ("tensor-dflash", 15),
        ]:
            try:
                rows = run_sweep(arm, prompts, nm or 3)
            except Exception as e:  # noqa: BLE001
                print(
                    json.dumps(
                        {
                            "phase": "sweep",
                            "arm": arm,
                            "n_max": nm,
                            "error": str(e)[:300],
                        }
                    ),
                    flush=True,
                )
                continue
            with open(OUT / "sweep.jsonl", "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r) + "\n" for r in rows)
        return 0
    if a.cmd == "quality":
        packs = asyncio.run(build_pack_prompts(6))
        (OUT / "quality_prompts.json").write_text(json.dumps(packs, ensure_ascii=False))
        for arm in ("tensor-off", "tensor-dflash"):
            rows = run_quality(arm, packs, a.n_max)
            with open(OUT / "quality.jsonl", "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        return 0
    prompts = json.loads((OUT / "prompts.json").read_text())
    for arm in a.arms.split(","):
        try:
            rows = run_arm(arm, prompts, a.n_max)
        except (
            Exception
        ) as e:  # noqa: BLE001 -- one placement failing must not stop the rest
            print(json.dumps({"arm": arm, "error": str(e)[:300]}), flush=True)
            continue
        with open(OUT / "results.jsonl", "a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
