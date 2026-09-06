#!/usr/bin/env python3
"""Wait for figtext to drain, then re-apply the 3060 layer-split rung so the
local lanes can curate the large-document tail beside the remote lane.

WHY. Measured 2026-09-05: 237 of 264 papers awaiting review exceed the 65k
local seat even at deepest compression; only the single 256k remote lane can
take them, at ~1.6 papers/h. The local curate lanes idle hundreds of rounds
with free capacity because only 24 papers fit them. A 131,072-cell pool
makes 180 of the 264 locally servable. That pool needs the 3090's VRAM back
from the weights, which the [40,12] layer split provides -- the rung
llmvp/configs/muse-glimmer-30b-cuda.yaml records as "stable, 80.9
completions/h over 5.5 h, zero faults. Re-apply it whenever paddle next
drains." Paddle is drained. This script is that re-application, gated on
figtext finishing first because a big curate turn and four vision streams
would otherwise compete for the same unified cells.

WHAT IT DOES, once `_fig_pending` is empty and no sidecar is alive:
  1. stop the mission; terminate the sidecars it orphans; verify EMPTY
  2. stop LLMVP the supported way (api/main.py --stop, pid-reuse guarded)
  3. back up the config, then set n_ctx 131072 + split_mode layer +
     tensor_split [40, 12]  (temp+mv; nothing else changes -- seats stay 6)
  4. start LLMVP; wait for health ok, kvPoolTokens == 131072, the engine-
     ready line, KV buffers summing to n_ctx x 52 KiB, and no OOM marker
  5. on ANY failure in 4: restore the backup and bring LLMVP back at 65,536
  6. relaunch the mission with its previous OUROBOROS_* env plus
     OUROBOROS_DISABLE_LANES=ocr (paddle must not be asked for) and
     OUROBOROS_CURATE_SEAT_TOKENS=131072; on fallback, the previous env only

    python dev/expand_local_seat_on_figtext_drain.py --dry-run   # plan only
    python dev/expand_local_seat_on_figtext_drain.py             # wait, then switch
    python dev/expand_local_seat_on_figtext_drain.py --now       # switch now

Run it DETACHED (setsid nohup ... & disown): it fires hours later and must
outlive the shell that started it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

WORKDIR = os.path.expanduser("~/corpora/ouroboros-spectra")
LLMVP = os.path.join(REPO, "llmvp")
CONFIG = os.path.join(LLMVP, "configs", "muse-glimmer-30b-cuda.yaml")
ACTIVE = os.path.join(LLMVP, "active_config.txt")
LLMVP_PID_FILE = "/tmp/llmvp.pid"
GRAPHQL = "http://localhost:8008/graphql"
TMP = os.path.expanduser("~/tmp")
OWN_PID = os.path.join(TMP, "seat_expansion.pid")
RESULT = os.path.join(TMP, "seat_expansion_result.json")
# The cuBLAS 12.9 launch convention (memory: a stale 12.0 lib faulted 3x in 2
# days; 48 h clean on this path). Read live from the running server when it
# exists; this is the fallback.
CUBLAS_LD = "/home/lah-rb/cuda-libs/nvidia/cublas/lib:/home/lah-rb/cuda-libs/nvidia/cuda_runtime/lib"
KV_BYTES_PER_TOKEN = 52 * 1024  # 1 KiB/token/layer x 52 layers, from the load log
OOM_RE = re.compile(
    r"out of memory|failed to allocate|cudaMalloc|CUDA error|cuda_malloc"
    r"|ggml_backend_cuda.*alloc|failed to load model|llama_model_load: error",
    re.I,
)


def log(msg: str) -> None:
    print(f"{dt.datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def pids_matching(pattern: str) -> list[int]:
    """Pids whose cmdline matches AND whose executable is python -- never the
    bash wrappers around them, whose -c text also contains the word python.
    Killing a wrapper orphans the real process (memory: stop by the resolved
    python pid and verify the list is EMPTY)."""
    out = sh(["pgrep", "-af", pattern]).stdout
    pids: list[int] = []
    for line in out.splitlines():
        pid, _, _cmd = line.partition(" ")
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            exe = os.path.basename(os.path.realpath(f"/proc/{pid}/exe"))
        except OSError:
            continue
        if exe.startswith("python"):
            pids.append(int(pid))
    return sorted(set(pids))


def proc_env(pid: int) -> dict[str, str]:
    try:
        raw = open(f"/proc/{pid}/environ", "rb").read().split(b"\0")
    except OSError:
        return {}
    env = {}
    for item in raw:
        k, _, v = item.partition(b"=")
        if k:
            env[k.decode(errors="replace")] = v.decode(errors="replace")
    return env


def graphql(query: str, timeout: float = 8.0) -> dict | None:
    try:
        req = urllib.request.Request(
            GRAPHQL,
            data=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001 -- down/unready is a normal state here
        return None


def health() -> dict | None:
    d = graphql(
        "{ health { status poolSize availableInstances activeInstances kvPoolTokens } }"
    )
    return ((d or {}).get("data") or {}).get("health")


def nvidia_smi() -> str:
    r = sh(
        [
            "nvidia-smi",
            "--query-gpu=index,memory.used,memory.total",
            "--format=csv,noheader",
        ]
    )
    return r.stdout.strip().replace("\n", " | ")


def wait_gone(pids: list[int], timeout: float) -> list[int]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        alive = [p for p in pids if os.path.exists(f"/proc/{p}")]
        if not alive:
            return []
        time.sleep(1)
    return [p for p in pids if os.path.exists(f"/proc/{p}")]


# ── drain check ──────────────────────────────────────────────────────


async def figtext_pending() -> int:
    from agent.actions.curation_actions import _fig_pending
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.local import LocalEffects

    bank = await read_databank(LocalEffects(WORKDIR))
    return sum(1 for r in bank.values() if _fig_pending(r))


def sidecars() -> list[int]:
    return pids_matching(
        r"tools/fig_review/fig_review\.py|tools/pdf_extract/extract_batch\.py"
    )


def drained() -> tuple[bool, str]:
    n = asyncio.run(figtext_pending())
    sc = sidecars()
    return (n == 0 and not sc), f"figtext pending={n} sidecars alive={len(sc)}"


# ── the switch ───────────────────────────────────────────────────────


def mission_pids() -> list[int]:
    return pids_matching(r"ouroboros\.py start")


def stop_mission() -> dict[str, str]:
    """SIGTERM the mission, then the sidecars it orphans; return its env."""
    pids = mission_pids()
    env = proc_env(pids[0]) if pids else {}
    if pids:
        log(f"stopping mission {pids}")
        for p in pids:
            os.kill(p, signal.SIGTERM)
        left = wait_gone(pids, 90)
        if left:
            raise RuntimeError(f"mission did not exit on SIGTERM: {left}")
    orphans = sidecars()
    if orphans:
        log(f"terminating {len(orphans)} orphaned sidecars {orphans}")
        for p in orphans:
            os.kill(p, signal.SIGTERM)
        left = wait_gone(orphans, 60)
        if left:
            raise RuntimeError(f"sidecars survived SIGTERM: {left}")
    assert not mission_pids() and not sidecars(), "process list is not EMPTY"
    log("mission + sidecars: EMPTY")
    return env


def llmvp_pids() -> list[int]:
    return pids_matching(r"api/main\.py")


def stop_llmvp() -> dict[str, str]:
    pids = llmvp_pids()
    env = proc_env(pids[0]) if pids else {}
    log(f"stopping LLMVP {pids} via api/main.py --stop")
    r = sh(
        [os.path.join(LLMVP, ".venv/bin/python"), "api/main.py", "--stop"], cwd=LLMVP
    )
    log(r.stdout.strip()[-300:])
    left = wait_gone(pids, 45)
    if left:
        raise RuntimeError(f"LLMVP still alive after --stop: {left}")
    assert not llmvp_pids(), "an api/main.py process survived"
    time.sleep(3)
    log(f"LLMVP stopped; GPU now: {nvidia_smi()}")
    return env


def edit_config(n_ctx: int, split: list[int]) -> str:
    """Rewrite n_ctx and add the split under model:, temp+mv. Returns backup path."""
    text = open(CONFIG, encoding="utf-8").read()
    assert (
        open(ACTIVE).read().strip() == "muse-glimmer-30b-cuda"
    ), "active config is not the cuda one"
    assert (
        text.count("\n  n_ctx: 65536\n") == 1
    ), "expected exactly one active n_ctx: 65536"
    assert not re.search(
        r"^\s*(split_mode|tensor_split)\s*:", text, re.M
    ), "split keys already present"
    anchor = "\n  probe_verified_n_ctx: 91136\n"
    assert text.count(anchor) == 1, "probe_verified_n_ctx anchor not found once"
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(TMP, f"muse-glimmer-30b-cuda.yaml.{stamp}.bak")
    shutil.copy2(CONFIG, backup)
    new = text.replace("\n  n_ctx: 65536\n", f"\n  n_ctx: {n_ctx}\n", 1).replace(
        anchor,
        anchor
        + f"  # ── 3060 LAYER-SPLIT RE-APPLIED {stamp[:8]} by dev/expand_local_seat_on_figtext_drain.py:\n"
        f"  # paddle drained, figtext drained; the {split}@{n_ctx} rung measured stable\n"
        f"  # 5.5 h / zero faults on 2026-08-22 (LAYER_SPLIT_EXPERIMENT.md). ocr lane is\n"
        f"  # disabled in the mission (OUROBOROS_DISABLE_LANES=ocr) so paddle is never\n"
        f"  # loaded onto CUDA1 beside these layers. Revert = the .bak beside this run's log.\n"
        f"  split_mode: layer\n"
        f"  tensor_split: [{split[0]}, {split[1]}]\n",
        1,
    )
    tmp = CONFIG + ".tmp"
    open(tmp, "w", encoding="utf-8").write(new)
    os.replace(tmp, CONFIG)
    log(f"config written (backup {backup})")
    return backup


def restore_config(backup: str) -> None:
    tmp = CONFIG + ".tmp"
    shutil.copy2(backup, tmp)
    os.replace(tmp, CONFIG)
    log(f"config RESTORED from {backup}")


def start_llmvp(env_from_old: dict[str, str]) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    logf = os.path.join(TMP, f"llmvp_launch_{stamp}.log")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = env_from_old.get("LD_LIBRARY_PATH") or CUBLAS_LD
    if "PATH" in env_from_old:
        env["PATH"] = env_from_old["PATH"]
    with open(logf, "ab") as out:
        subprocess.Popen(
            ["setsid", "nohup", ".venv/bin/python", "api/main.py"],
            cwd=LLMVP,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    log(f"LLMVP launched -> {logf}")
    return logf


def wait_llmvp_ready(
    logf: str, want_ctx: int, timeout: float = 420.0
) -> tuple[bool, str]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        text = (
            open(logf, encoding="utf-8", errors="replace").read()
            if os.path.exists(logf)
            else ""
        )
        oom = [ln for ln in text.splitlines() if OOM_RE.search(ln)]
        if oom:
            return False, "boot log shows a fault: " + oom[0][:200]
        h = health()
        ready_line = "Batched decode engine ready" in text
        if (
            h
            and h.get("status") == "ok"
            and ready_line
            and int(h.get("availableInstances") or 0) >= 1
        ):
            if int(h.get("kvPoolTokens") or 0) != want_ctx:
                return False, f"kvPoolTokens={h.get('kvPoolTokens')} != {want_ctx}"
            kv = sum(
                float(m) for m in re.findall(r"KV buffer size =\s+([\d.]+) MiB", text)
            )
            want = want_ctx * KV_BYTES_PER_TOKEN / 2**20
            # the vision contexts add their own small KV buffers; the text
            # pool must account for the bulk of it
            if kv < want * 0.95:
                return (
                    False,
                    f"KV buffers total {kv:.0f} MiB, expected >= {want*0.95:.0f} for n_ctx {want_ctx}",
                )
            return True, f"ready: {h} | KV buffers {kv:.0f} MiB | {nvidia_smi()}"
        time.sleep(3)
    return False, f"not ready within {timeout:.0f}s (last health {health()})"


def start_mission(base_env: dict[str, str], extra: dict[str, str], tag: str) -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    logf = os.path.join(TMP, f"run_v25_{tag}_{stamp}.log")
    env = dict(os.environ)
    for k, v in base_env.items():
        if k.startswith("OUROBOROS_"):
            env[k] = v
    # the three the last launch carried, as a floor if the old env was unreadable
    env.setdefault("OUROBOROS_REMOTE_CURATE_LANES", "1")
    env.setdefault("OUROBOROS_SCRAPER_OVERLAP_PDFS", "0")
    env.setdefault("OUROBOROS_CONTACT_EMAIL", "luke.hayes@mines.sdsmt.edu")
    env.update(extra)
    with open(logf, "ab") as out:
        subprocess.Popen(
            [
                "setsid",
                "nohup",
                ".venv/bin/python",
                "ouroboros.py",
                "start",
                "--working-dir",
                WORKDIR,
            ],
            cwd=REPO,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    time.sleep(20)
    pids = mission_pids()
    if not pids:
        raise RuntimeError("mission did not come up")
    log(
        f"mission up {pids} -> {logf}  env: "
        + " ".join(
            f"{k}={v}" for k, v in sorted(env.items()) if k.startswith("OUROBOROS_")
        )
    )
    return logf


def do_switch(n_ctx: int, split: list[int]) -> dict:
    result = {
        "started": dt.datetime.now().isoformat(),
        "target_n_ctx": n_ctx,
        "split": split,
    }
    result["gpu_before"] = nvidia_smi()
    result["health_before"] = health()
    log(f"BEFORE: {result['gpu_before']} | {result['health_before']}")

    mission_env = stop_mission()
    llmvp_env = stop_llmvp()
    backup = edit_config(n_ctx, split)
    result["config_backup"] = backup
    logf = start_llmvp(llmvp_env)
    ok, why = wait_llmvp_ready(logf, n_ctx)
    log(("READY " if ok else "FAILED ") + why)
    result["llmvp_log"] = logf
    result["target_ok"] = ok
    result["target_detail"] = why

    if not ok:
        # exact revert: today's config, today's env, nothing new
        log("FALLBACK: reverting to the known-good config")
        try:
            stop_llmvp()
        except Exception as e:  # noqa: BLE001
            log(f"(stop during fallback: {e}); killing any survivor")
            for p in llmvp_pids():
                os.kill(p, signal.SIGKILL)
            wait_gone(llmvp_pids(), 30)
        restore_config(backup)
        logf2 = start_llmvp(llmvp_env)
        ok2, why2 = wait_llmvp_ready(logf2, 65536)
        log(("FALLBACK READY " if ok2 else "FALLBACK FAILED ") + why2)
        result.update(
            {"fallback_ok": ok2, "fallback_detail": why2, "llmvp_log_fallback": logf2}
        )
        result["mission_log"] = start_mission(mission_env, {}, "fallback65k")
    else:
        result["mission_log"] = start_mission(
            mission_env,
            {
                "OUROBOROS_DISABLE_LANES": "ocr",
                "OUROBOROS_CURATE_SEAT_TOKENS": str(n_ctx),
            },
            f"split{n_ctx//1024}k",
        )
    result["gpu_after"] = nvidia_smi()
    result["finished"] = dt.datetime.now().isoformat()
    json.dump(result, open(RESULT, "w"), indent=1)
    log(f"AFTER: {result['gpu_after']}")
    log(f"result -> {RESULT}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan and current state; change nothing",
    )
    ap.add_argument("--now", action="store_true", help="skip the drain wait")
    ap.add_argument("--poll", type=int, default=300)
    ap.add_argument("--max-wait-hours", type=float, default=24.0)
    ap.add_argument("--n-ctx", type=int, default=131072)
    ap.add_argument("--split", default="40,12")
    a = ap.parse_args()
    split = [int(x) for x in a.split.split(",")]
    assert (
        len(split) == 2 and sum(split) == 52
    ), "split must name 52 layers across 2 devices"

    os.makedirs(TMP, exist_ok=True)
    if os.path.exists(OWN_PID) and not a.dry_run:
        old = int(open(OWN_PID).read().strip() or 0)
        if old and os.path.exists(f"/proc/{old}"):
            log(f"another instance is running (pid {old}); refusing")
            return 2
    if not a.dry_run:
        open(OWN_PID, "w").write(str(os.getpid()))

    ok, state = drained()
    log(f"drain check: {state} -> {'DRAINED' if ok else 'waiting'}")
    log(f"mission {mission_pids()} | llmvp {llmvp_pids()} | sidecars {sidecars()}")
    log(
        f"config n_ctx line present: {open(CONFIG).read().count(chr(10)+'  n_ctx: 65536'+chr(10))==1} | active={open(ACTIVE).read().strip()}"
    )
    log(f"gpu: {nvidia_smi()} | health: {health()}")
    if a.dry_run:
        log(
            f"DRY RUN: would set n_ctx {a.n_ctx}, split_mode layer, tensor_split {split}; "
            "relaunch mission with OUROBOROS_DISABLE_LANES=ocr OUROBOROS_CURATE_SEAT_TOKENS="
            f"{a.n_ctx}; fallback = exact revert to 65536"
        )
        return 0

    try:
        if not a.now:
            deadline = time.time() + a.max_wait_hours * 3600
            while not ok:
                if time.time() > deadline:
                    log("max wait exceeded; NOT switching. exiting")
                    return 3
                time.sleep(a.poll)
                ok, state = drained()
                log(f"drain check: {state}")
            log("figtext DRAINED -- switching")
        res = do_switch(a.n_ctx, split)
        return 0 if res.get("target_ok") else 1
    finally:
        try:
            os.remove(OWN_PID)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
