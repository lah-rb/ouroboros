#!/usr/bin/env python3
"""REFINE the arithmetic KV estimate against what a model actually decodes.

    api/main.py --probe-context step37-flash-196b-a11,gemma-4-31b
    api/main.py --probe-context active
    api/main.py --probe-context a,b,c --probe-no-write

Produces a MEASURED n_ctx ceiling per model, to 2k resolution, so a config stops
being a guess — `qwen3.5-122b-a10` declares max_tokens_default 262144 against
n_ctx 131072 precisely because nobody measured it.

── WHY THIS IS REFINEMENT AND NOT A SEARCH (v2, 2026-07-29) ─────────
v1 bisected blindly from the trained range to find out whether the always-on
error-code guard made the pre-allocation arithmetic redundant. It answered on
its second rung, by HARD-REBOOTING THE MACHINE: step-3.7 at n_ctx 262144
computed 143.7GB against 137.4GB physical — 6.3GB over, 4.6% — and died 2m51s
into the load having emitted no llama.cpp error code at all. The process died
allocating, so the decode-code guard never had a call to return from.

That settled it. Coding alone cannot defend the allocation band, so the
arithmetic guard stays and this probe's job changed: it no longer searches for
the ceiling, it refines the arithmetic's answer.

The opening rung is now `calc_start()` — the largest n_ctx the arithmetic says
fits, times a 3% margin. That is deliberately UNDER the boundary, which is the
whole safety property: a failure there surfaces as a decode code the guard can
catch, not as an allocation that kills the OS. The margin also means the first
rung usually PASSES, so the ladder climbs; a passing rung costs a load, a
failing one costs a load plus a guard teardown.

From there the ladder is 10240 → 5120 → 2048, reversing direction each time the
verdict flips. Up if the calculated point passed, down if it failed; each flip
brackets the true boundary tighter. The measured ceiling is then written back as
`probe_verified_n_ctx`, which the KV preflight honours over its own arithmetic —
a measurement outranks an estimate.

WHAT THIRTEEN MODELS SHOWED (2026-07-29). Two findings, and the formula is only
half-right.

1. THE FORMULA IS EXACTLY RIGHT WHEN swa_full IS ON, and over-predicts by the
   SWA interleave ratio when it is off. The ratios are integers, not noise:
   1.00x on six models with swa_full:true; 4.00x on five with swa_full:false;
   2.00x on glm-4.7-flash (MLA stores one latent, the formula counts K and V);
   22.59x on gemma-4-26b-a4b. laguna-S and laguna-XS are the proof — same
   architecture, 1.00x and 3.95x, differing only in that flag.
2. THE MACHINE'S USABLE FRACTION IS A CONSTANT, 0.871-0.879 of physical across
   every architecture and quant measured. That is what the formula cannot see
   and what the guard's ceiling now encodes.

Seven of thirteen were limited by their TRAINED RANGE, not by this machine.

── THE PREFLIGHT IS NOT DISABLED ANY MORE ───────────────────────────
`OURO_KV_PREFLIGHT_GB=9999` is still set, but it now means "ignore the config's
declared budget" rather than "ignore physics": the guard clamps every budget to
physical * OURO_PHYSICAL_SAFETY. So the probe can explore past step-3.7's
declared 112GB while remaining unable to repeat the crash that produced this
design. The ceiling is read FROM the guard, so the probe and the thing it
refines cannot drift apart.

── SAFETY, GIVEN A CRASH REMAINS POSSIBLE ───────────────────────────
* Every rung result is written and fsync'd BEFORE the next rung starts, so a
  hard reboot loses at most the rung in flight. (v1 lost exactly that much.)
* The original config text is copied to the run directory before the first edit
  and restored in a finally-block; the backup path is printed up front, because
  a crash-time restore is impossible by definition and v1 needed the backup.
* Wired memory is sampled at 10Hz throughout.

── NOT VALID FOR POOLED/BATCHED CONFIGS ─────────────────────────────
This probe assumes `n_ctx` is ONE context's window. Under
`decode_mode: batched` it is the SUM budgeted across pool streams —
`gpt-oss-120b-a5-swarm-524k` declares n_ctx 524288 against a
model_max_context of 131072 — so a rung there means something different and a
recorded `probe_verified_n_ctx` would be actively misleading. Probe the
single-stream config for a model's per-context ceiling; a pool's total is a
different measurement and needs a different instrument.

READ IT AS: `ceiling` is the largest rung that both LOADED and DECODED. Loading
is not enough — the Hy3 ladder found 49152 loaded fine and could not decode a
single token, which is why every rung generates.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

LLMVP = Path(__file__).resolve().parents[1]
ROOT = LLMVP.parent
RUNS = Path.home() / "ouroboros-runs"
ENDPOINT = "http://localhost:8008/graphql"
PRODUCTION = "gpt-oss-120b-a5-swarm-524k"

# How far under the guard ceiling to open. The ceiling is calibrated to the
# LOWEST usable fraction observed (0.871 of physical), so a rung computed at
# it is a coin flip; 3% under makes the first rung a pass and lets the ladder
# climb. Costs at most one extra 10k step.
_PROBE_START_MARGIN = 0.97

# The probe is allowed slightly MORE physical memory than production, so it can
# reach the hardware boundary instead of re-measuring the guard.
#
# Production sits at 0.87 (the lowest usable fraction observed). The real
# boundary is a shade above it — laguna-poolside passed at 0.877 and failed at
# 0.881 — so a probe held to 0.87 stops just short and every failing rung comes
# back `preflight_refused`. That is what happened to laguna-APEX and
# laguna-unsloth on 2026-07-29: their ceilings are OUR CONSTANT, not the
# machine, and are lower bounds.
#
# 0.88 worked for the lagunas — both re-measures ended on decode errors instead
# of refusals — but step-3.7 and mistral-medium still hit the arithmetic first,
# so 0.89 gives the heavy-weights models the last point they need. Every
# observed hardware boundary sits at 0.871-0.879 and the reboot was at 1.046, so
# 0.89 clears the boundary while staying far below anything dangerous. The
# margin between measuring and rebooting is thin, so this moves one point at a
# time, not five.
#
# 0.89 -> 0.90 on 2026-07-30, one point, for a measured reason: PRODUCTION runs
# hy3 at n_ctx 32768, whose real footprint (108.3GB weights + 10.9 KV + 3.2
# measured overhead = 122.4GB) is 0.891 of physical. A probe that refuses a
# configuration this machine serves every day is measuring its own constant.
# 0.891 is therefore an OBSERVED-SAFE datapoint, and the only observed-fatal
# one remains step-3.7 at 1.046 — the band between them is what the probe is
# for. Both hard reboots ran the machine out of PHYSICAL memory; neither had
# anything to do with iogpu.wired_limit_mb (see the note below it).
_PROBE_PHYSICAL_SAFETY = float(os.environ.get("OURO_PROBE_PHYSICAL_SAFETY", "0.90"))


# THE WIRED LIMIT IS NOT THE CEILING — do not reintroduce a clamp to it.
# `sysctl iogpu.wired_limit_mb` (116000 MiB = 121.6 GB) reads like a hard
# Metal boundary and is not one. Two independent measurements cross it and
# serve fine: the 2026-07-28 hy3 ladder at 116.4 GB (see the header of
# dev/wired_mem_recorder.py, which exists BECAUSE of that observation) and
# this probe's own 2026-07-30 rung 30720, which passed 3/3 generations at
# peak_wired 121.7 GB. PHYSICAL memory is what dictates death: the only
# hard reboots on record (step-3.7 at 143.7 GB, hy3 at ~125 GB + rebuild
# churn) both ran the machine out of RAM outright. A clamp to the wired
# limit was added here after the hy3 reboot and reverted the same day —
# it refused production-proven configurations while doing nothing about
# the actual failure mechanism (see rung(): a latched context must never
# be generated into).

PROBE_PROMPT = (
    "Write a Python function that merges two sorted lists. Return only the code."
)


@dataclass
class Rung:
    n_ctx: int
    verdict: str  # pass | boot_fail | preflight_refused | decode_code
    detail: str = ""  # | guard_sigterm | no_output | server_gone
    kv_mib: float = 0.0
    peak_wired_gb: float = 0.0
    gens_ok: int = 0
    gens_fail: int = 0
    seconds: int = 0
    # THE RESEARCH OUTPUT. Did the always-on error-code guard notice, and did it
    # act? A rung that fails with guard_saw=False is a rung the guard missed.
    guard_saw: bool = False
    guard_acted: bool = False


@dataclass
class ModelResult:
    config: str
    trained_ctx: int
    original_n_ctx: int
    ceiling: Optional[int] = None
    calc_start: Optional[int] = None
    weights_bytes: int = 0
    kv_bytes_per_token_actual: int = 0
    bound_by: str = ""
    rungs: list = field(default_factory=list)
    note: str = ""


def q(query: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def healthy() -> bool:
    try:
        return "ok" in json.dumps(q("{ health { status } }", 5))
    except Exception:  # noqa: BLE001
        return False


def health_flags() -> dict:
    try:
        d = q("{ health { decodeFailures unhealedDecodeFailures unservable } }", 5)
        return (d.get("data") or {}).get("health") or {}
    except Exception:  # noqa: BLE001
        return {}


def server_pids() -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", "api/main.py"], capture_output=True, text=True
    ).stdout.split()
    return [int(p) for p in out if p.isdigit()]


def stop_server(log) -> None:
    """SIGTERM, then escalate. A server that has just been declared unservable
    is already on its way out; one wedged behind an abandoned generation is not,
    and waiting 900s for it cost an arm on 2026-07-29."""
    if not server_pids():
        return
    for p in server_pids():
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(180):
        if not server_pids():
            return
        time.sleep(1)
    log("    server ignored SIGTERM 180s — SIGKILL")
    for p in server_pids():
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(3)


def kv_bytes_per_token(model_path: str, measured: Optional[int]) -> int:
    """Measured if the config declares it, else the header formula."""
    if measured:
        return int(measured)
    sys.path.insert(0, str(ROOT / "dev"))
    from gguf_geometry import read

    kv, _ = read(model_path)
    arch = kv.get("general.architecture", "")
    layers = int(kv.get(f"{arch}.block_count") or 0)
    heads = kv.get(f"{arch}.attention.head_count")
    kvh = kv.get(f"{arch}.attention.head_count_kv", heads)
    if hasattr(kvh, "__len__"):
        kvh = max(kvh)
    hd = kv.get(f"{arch}.attention.key_length")
    if not hd and heads:
        hd = int(kv.get(f"{arch}.embedding_length")) // int(heads)
    return 2 * layers * int(kvh) * int(hd) * 2


_KV_LINE = re.compile(r"KV buffer size = *([0-9.]+)")
_FREED = "ggml_metal_free: deallocating"


def live_kv_mib(server_log: str) -> float:
    """KV actually held, not KV ever allocated.

    Summing every `KV buffer size` line in the log counts allocations that were
    subsequently FREED, and one config reliably does that: a model requesting
    `resident_seq_cache` on an architecture with `memory_can_shift=False` gets
    the seq-band context built, released, and rebuilt single-seq —

        llama_context: constructing llama_context (n_seq_max=14)
        KV buffer size = 6480.00 / 17820.00
        🧩 un-fragmenting: rebuilding single-seq
        ggml_metal_free: deallocating          <-- the first is GONE
        llama_context: constructing llama_context (n_seq_max=1)
        KV buffer size = 6480.00 / 17820.00

    Naively summed that reads as 2x the real footprint, and it did: step-3.7
    was recorded at 360 KiB/token against a 180 KiB formula and spent a day
    looking like an architecture whose geometry we could not predict. It was
    24,300 MiB all along — exactly what the formula said.

    So: split on the deallocation marker and take the last group that actually
    allocated anything. Last NON-EMPTY rather than simply last, because a log
    read after shutdown ends with a free and no allocations after it.
    """
    groups = [
        sum(float(x) for x in _KV_LINE.findall(seg)) for seg in server_log.split(_FREED)
    ]
    return next((g for g in reversed(groups) if g), 0.0)


def weights_gb(model_path: str) -> float:
    """Total weight bytes for THIS model, in GB.

    Delegates to the backend's shard-aware ``weights_bytes_total``, which
    follows the ``-00001-of-000NN`` shard set (or the GGUF's split.count)
    rather than globbing the directory.

    IT USED TO GLOB (2026-08-03 fix). The glob summed every ``*.gguf``
    beside the model and excluded only ``mmproj``, so a sibling MODEL was
    counted as part of ours — the exact failure the backend's own
    docstring warns about ("anything that switches this to a directory
    glob must re-exclude it by name"). Found on the first subject that
    had a neighbour: DeepSeek-V4-Flash ships four shards totalling
    104.2GB and sits beside an unrelated 10.9GB Q8_0 build, so the probe
    computed 115.1GB of weights, found NEGATIVE headroom under the
    ceiling, and clamped its opening rung to n_ctx 2048 — unusable.
    The over-count direction is fail-safe (it refuses rather than
    over-allocates), which is why this hid until a model with a
    room-mate turned up.
    """
    from inference.backends.llama_cpp_backend import LlamaCppBackend

    return LlamaCppBackend.weights_bytes_total(model_path) / 1e9


def calc_start(
    model_path: str,
    measured: Optional[int],
    trained: int,
    ceiling_gb: float,
    resolution: int,
) -> tuple[int, float]:
    """The ARITHMETIC's own answer: the largest n_ctx whose weights + KV fits
    under the physical ceiling.

    This is the whole change of shape. The first probe bisected blindly from
    the trained range and its second rung — 6.3GB past physical — rebooted the
    machine before llama.cpp returned anything. Starting from the calculated
    fit means the first rung is just UNDER the boundary, where a failure
    surfaces as a decode code the always-on guard can catch rather than as an
    allocation that kills the OS. The search then refines the arithmetic
    instead of searching for it.
    """
    per = kv_bytes_per_token(model_path, measured)
    w = weights_gb(model_path)
    # Open just BELOW the guard's ceiling, not exactly at it. The ceiling is now
    # calibrated to the lowest fraction actually observed (0.871), so a rung
    # computed at it lands on the boundary and is as likely to fail as pass.
    # Opening under it means the first rung PASSES and the ladder walks up,
    # which is the cheaper direction: a passing rung costs a load, a failing one
    # costs a load plus a guard teardown, and on the very first rung of an
    # unmeasured model it is the difference between refining and gambling.
    n = int(((ceiling_gb * _PROBE_START_MARGIN - w) * 1e9) // per)
    n -= n % resolution
    return max(resolution, min(n, trained)), per / 1024.0


def trained_context(model_path: str, cfg_max: int) -> int:
    """Prefer the GGUF header; fall back to the config's declared range."""
    try:
        sys.path.insert(0, str(ROOT / "dev"))
        from gguf_geometry import read

        kv, _ = read(model_path)
        arch = kv.get("general.architecture", "")
        n = int(kv.get(f"{arch}.context_length") or 0)
        return n or cfg_max
    except Exception:  # noqa: BLE001
        return cfg_max


_N_CTX_RE = re.compile(r"^(\s*n_ctx:\s*)\d+", re.M)


def set_n_ctx(path: Path, n: int) -> None:
    """Line-level edit. A YAML round-trip would strip every comment in these
    files, and the comments are the reasoning.

    VERIFY THE MATCH, NOT THE DIFF. The first version raised "could not rewrite
    n_ctx" whenever the substitution produced identical text — which is exactly
    what happens when the rung equals the value already in the config. It killed
    the probe on its first rung, because gpt-oss's control rung IS its configured
    n_ctx. "No change" and "no match" are different facts and only one of them is
    an error."""
    text = path.read_text()
    if not _N_CTX_RE.search(text):
        raise SystemExit(f"no 'n_ctx:' line found in {path}")
    path.write_text(_N_CTX_RE.sub(lambda m: f"{m.group(1)}{n}", text, count=1))


class Probe:
    def __init__(self, args):
        self.args = args
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.base = RUNS / f"ctx_probe_{stamp}"
        self.base.mkdir(parents=True, exist_ok=True)
        self.results: list[ModelResult] = []
        self.rec: Optional[subprocess.Popen] = None
        # Same ceiling the guard enforces, read from the guard itself so the
        # probe and the thing it is refining can never drift apart.
        from inference.backends.llama_cpp_backend import _physical_memory_gb

        self.physical_gb = _physical_memory_gb()
        # Probe fraction, not the production one — see _PROBE_PHYSICAL_SAFETY.
        self.ceiling_gb = self.physical_gb * _PROBE_PHYSICAL_SAFETY

    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(self.base / "probe.log", "a") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def save(self) -> None:
        """Written and FSYNC'd after every rung — a hard reboot is an expected
        outcome here, and losing the bisect state to it would waste the run."""
        tmp = self.base / "RESULTS.json.tmp"
        with open(tmp, "w") as fh:
            json.dump([asdict(r) for r in self.results], fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(self.base / "RESULTS.json")

    def _wired_rows(self) -> list:
        """Sample rows from the recorder's CSV.

        NOTE the recorder writes to ~/ouroboros-runs/ regardless of cwd (it says
        so, deliberately: /tmp does not survive the reboot this probe might
        cause). Globbing the probe's own directory finds nothing and reports a
        silent 0.0 on every rung — which looks like a measurement rather than a
        missing one."""
        csvs = sorted(RUNS.glob("wired_ctxprobe_*.csv"))
        if not csvs:
            return []
        return [ln for ln in csvs[-1].read_text().splitlines() if ln.startswith("S,")]

    def mark_wired(self) -> None:
        """Watermark the sampler before a rung starts, so peak_wired() reports
        THIS rung's peak and not the run's.

        One recorder covers the whole probe session, so an unwatermarked max is
        monotone across every model and rung. That is not a subtle error: in
        run ctx_probe_20260730-124104, gemma-26b (14GB of weights) inherited
        hy3's 120.4GB and its measured overhead came out as 100.4GB. It was
        harmless there only because gemma hit the trained range on its first
        rung — anywhere else it would have poisoned the arithmetic gate, which
        is exactly how a probe artifact becomes a recorded 'ceiling'."""
        self._wired_mark = len(self._wired_rows())

    def peak_wired(self) -> float:
        """Peak wired GB since the last mark_wired()."""
        try:
            rows = self._wired_rows()[getattr(self, "_wired_mark", 0) :]
            return max((float(ln.split(",")[3]) for ln in rows), default=0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    # ── one rung ──────────────────────────────────────────────────
    def rung(self, cfg: str, cfg_path: Path, n: int) -> Rung:
        t0 = time.time()
        self.log(f"  ── rung n_ctx={n}")
        stop_server(self.log)
        self.mark_wired()  # this rung's peak, not the run's
        set_n_ctx(cfg_path, n)
        (LLMVP / "active_config.txt").write_text(cfg)
        slog = self.base / f"{cfg}_{n}_server.log"

        env = {
            **os.environ,
            # Neutralise the ARITHMETIC guard: this probe measures the
            # machine, not the header formula. The error-code guard stays on
            # — it has no off switch and that is the point.
            "OURO_KV_PREFLIGHT_GB": "9999",
            # Raise the guard's own clamp for the child server, so a rung
            # between the production ceiling and the hardware boundary is
            # actually attempted rather than refused on arithmetic.
            "OURO_PHYSICAL_SAFETY": str(_PROBE_PHYSICAL_SAFETY),
        }
        with open(slog, "w") as fh:
            subprocess.Popen(
                [sys.executable, "api/main.py"],
                cwd=LLMVP,
                stdout=fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )

        deadline = time.time() + self.args.boot_timeout
        up = False
        while time.time() < deadline:
            if healthy():
                up = True
                break
            if not server_pids():
                break  # died during load
            txt = slog.read_text(errors="ignore") if slog.exists() else ""
            if "Startup failed" in txt or "KV preflight REFUSED" in txt:
                break
            time.sleep(5)

        txt = slog.read_text(errors="ignore") if slog.exists() else ""
        saw_code = bool(re.search(r"code -[123]\b", txt))
        acted = "UNSERVABLE" in txt
        kv = live_kv_mib(txt)

        if not up:
            why = (
                "preflight_refused"
                if "KV preflight REFUSED" in txt
                else "decode_code" if saw_code else "boot_fail"
            )
            m = re.search(r"(KV preflight REFUSED.*|Startup failed.*)", txt)
            r = Rung(
                n,
                why,
                (m.group(1)[:160] if m else "no healthy server"),
                kv,
                self.peak_wired(),
                0,
                0,
                int(time.time() - t0),
                saw_code,
                acted,
            )
            self.log(
                f"     FAIL/{why}  guard_saw={saw_code} acted={acted}  {r.detail[:80]}"
            )
            stop_server(self.log)
            return r

        # HEALTHY IS NOT THE SAME AS SOUND — and the difference is what
        # rebooted the machine (hy3 @ 40960, 2026-07-30). That rung OOMed at
        # its FIRST compute (Metal status 5), which latched the context; the
        # backend logged "Warm-up failed for pool slot #0" and then went on to
        # log "Server initialized successfully", so healthy() said yes. The
        # probe generated into it anyway. Every generation hit the latch and
        # triggered a heal — and a context rebuild allocates a fresh KV
        # alongside the one it is replacing, so the transient footprint jumped
        # by a whole KV against a machine already near physical. That is what
        # ran the box out of RAM, not the rung's steady-state size.
        #
        # So: a boot that already produced a decode code or a failed warm-up
        # is a FAILING rung, full stop. Report it and stop the server without
        # ever asking it to generate.
        if saw_code or "Warm-up failed" in txt:
            r = Rung(
                n,
                "decode_code",
                "latched during warm-up — not generated into "
                "(heal churn is what reboots the machine)",
                kv,
                self.peak_wired(),
                0,
                0,
                int(time.time() - t0),
                saw_code,
                acted,
            )
            self.log(
                f"     FAIL/decode_code  latched at warm-up; skipped "
                f"generations  guard_saw={saw_code} acted={acted}"
            )
            stop_server(self.log)
            return r

        ok = bad = 0
        for i in range(self.args.gens):
            if not server_pids():
                bad += 1
                self.log(f"     gen{i+1}: SERVER GONE — the guard SIGTERMed it")
                break
            try:
                d = q(
                    '{completion(request:{prompt:"%s",maxTokens:200}){text}}'
                    % PROBE_PROMPT,
                    timeout=600,
                )
                t = ((d.get("data") or {}).get("completion") or {}).get("text") or ""
                if d.get("errors") or not t.strip():
                    bad += 1
                else:
                    ok += 1
            except Exception as exc:  # noqa: BLE001
                bad += 1
                self.log(f"     gen{i+1}: {type(exc).__name__} {str(exc)[:70]}")

        txt = slog.read_text(errors="ignore") if slog.exists() else ""
        saw_code = bool(re.search(r"code -[123]\b", txt))
        acted = "UNSERVABLE" in txt
        flags = health_flags()
        gone = not server_pids()
        verdict = (
            "pass"
            if ok > 0 and not saw_code and not gone
            else "server_gone" if gone else "decode_code" if saw_code else "no_output"
        )
        r = Rung(
            n,
            verdict,
            f"health={flags}",
            kv,
            self.peak_wired(),
            ok,
            bad,
            int(time.time() - t0),
            saw_code,
            acted,
        )
        self.log(
            f"     {verdict.upper()}  ok={ok} fail={bad} kv={kv:.0f}MiB "
            f"peak_wired={r.peak_wired_gb:.1f}GB guard_saw={saw_code} acted={acted}"
        )
        stop_server(self.log)
        return r

    def write_back(self, res: ModelResult) -> None:
        """Record what was MEASURED into the config that was measured.

        This is the point of the whole exercise: the arithmetic gets refined by
        the run instead of staying a standing estimate. Two values land —

          probe_verified_n_ctx        the largest rung that loaded AND decoded
          probe_verified_weights_bytes what the weights summed to at the time,
                                       so a requant invalidates the claim
                                       rather than silently outliving it

        Line-edited, not YAML round-tripped: these configs carry their
        reasoning in comments and a round-trip would delete all of it.
        """
        if not res.ceiling or self.args.no_write:
            return
        path = LLMVP / "configs" / f"{res.config}.yaml"
        text = path.read_text()
        pairs = (
            ("probe_verified_n_ctx", res.ceiling),
            ("probe_verified_weights_bytes", res.weights_bytes),
        )
        for key, val in pairs:
            pat = re.compile(rf"^(\s*{key}:\s*).*$", re.M)
            if pat.search(text):
                text = pat.sub(lambda m: f"{m.group(1)}{val}", text, count=1)
            else:
                # Anchor to n_ctx — the same block, and a line every model
                # config has. Matching indentation keeps it inside `model:`.
                anchor = _N_CTX_RE.search(text)
                indent = anchor.group(1)[: len(anchor.group(1)) - len("n_ctx: ")]
                insert = f"{anchor.group(0)}\n{indent}{key}: {val}"
                text = text.replace(anchor.group(0), insert, 1)
        path.write_text(text)
        self.log(
            f"    config updated: probe_verified_n_ctx={res.ceiling} "
            f"weights={res.weights_bytes / 1e9:.1f}GB"
        )

    # ── one model ─────────────────────────────────────────────────
    def model(self, cfg: str) -> ModelResult:
        cfg_path = LLMVP / "configs" / f"{cfg}.yaml"
        if not cfg_path.exists():
            self.log(f"!! no config {cfg}")
            return ModelResult(cfg, 0, 0, note="config not found")

        import yaml

        raw = yaml.safe_load(cfg_path.read_text())
        m = raw["model"]
        orig = int(m["n_ctx"])
        trained = trained_context(
            str(m["path"]), int(m.get("model_max_context") or orig)
        )
        # POOL BUDGETS ARE NOT CAPPED BY THE TRAINED RANGE. Under batched decode
        # `n_ctx` is the SUM across streams while `model_max_context` bounds each
        # ONE — gpt-oss-swarm declares n_ctx 524288 against a 131072 trained
        # range, i.e. four streams' worth. Capping such a config at the GGUF's
        # context_length would refuse every rung above a quarter of its actual
        # allocation and measure nothing.
        #
        # A config declaring n_ctx ABOVE its trained range is making exactly
        # that claim, so the only real bound left is memory: the arithmetic max
        # under the probe ceiling.
        if orig > trained:
            pool_max, _ = calc_start(
                str(m["path"]),
                m.get("kv_bytes_per_token_measured"),
                10**9,
                self.ceiling_gb / _PROBE_START_MARGIN,
                self.args.resolution,
            )
            self.log(
                f"    POOL config (n_ctx {orig} > trained {trained}): "
                f"per-stream cap does not apply, memory bound {pool_max}"
            )
            trained = pool_max
        backup = self.base / f"{cfg}.yaml.orig"
        shutil.copy2(cfg_path, backup)

        res = ModelResult(cfg, trained, orig)
        self.results.append(res)
        self.log(f"\n═══ {cfg} — trained {trained}, config {orig} ═══")
        self.log(f"    config backup: {backup}")

        start, kib = calc_start(
            str(m["path"]),
            m.get("kv_bytes_per_token_measured"),
            trained,
            self.ceiling_gb,
            self.args.resolution,
        )
        res.calc_start = start
        res.weights_bytes = int(weights_gb(str(m["path"])) * 1e9)
        self.log(
            f"    KV {kib:.0f} KiB/token "
            f"({'measured' if m.get('kv_bytes_per_token_measured') else 'formula'})"
            f" · arithmetic says {start} fits under {self.ceiling_gb:.1f}GB"
        )

        try:
            # ── REFINE THE ARITHMETIC, DO NOT SEARCH FOR IT ──────────
            # Step 10k in whichever direction the calculated point indicates,
            # until the verdict flips; then reverse at 5k until it flips again;
            # then 2k. Each flip brackets the boundary tighter, and every rung
            # sits near a value the arithmetic already believes is close — so
            # failures arrive as decode codes rather than as reboots.
            ladder = [10240, 5120, self.args.resolution]
            rung0 = self.rung(cfg, cfg_path, start)
            res.rungs.append(rung0)
            self.save()

            passed = rung0.verdict == "pass"
            best = start if passed else None
            direction = 1 if passed else -1
            current = start

            w_gb = res.weights_bytes / 1e9
            # LEARNED overhead: the formula predicts weights+KV only; the
            # real footprint adds compute/layer buffers (hy3 rung 1: predicted
            # 118.5, wired 121.7 — a +3.2GB offset the gate must carry, or an
            # "admitted" rung lands past the wired limit anyway).
            overhead_gb = 0.0
            _pred0 = w_gb + kib * 1024 * start / 1e9
            if rung0.peak_wired_gb > _pred0:
                overhead_gb = rung0.peak_wired_gb - _pred0
                self.log(
                    f"    measured overhead {overhead_gb:.1f}GB over the "
                    f"formula — carried into every later rung's gate"
                )
            # peak_wired is the run-wide max (monotone across rungs), so an
            # overhead re-measure is only attributable when the rung is a new
            # n_ctx high — a smaller rung inherits an earlier rung's peak.
            max_booted = start
            for step in ladder:
                while True:
                    nxt = current + direction * step
                    nxt = max(self.args.resolution, min(nxt, trained))
                    if nxt == current:
                        break  # pinned at trained or floor
                    # Arithmetic gate on EVERY rung, not just the opener. The
                    # opener respected the ceiling and the ladder then stepped
                    # blind — on hy3 (324 KiB/token) one 10k step was +3.3GB,
                    # from wired-saturation straight into the hard-reboot band.
                    predicted = w_gb + kib * 1024 * nxt / 1e9 + overhead_gb
                    if predicted > self.ceiling_gb:
                        self.log(
                            f"  ── rung n_ctx={nxt} ARITH-REFUSED "
                            f"(predicted {predicted:.1f}GB incl. "
                            f"{overhead_gb:.1f}GB measured overhead > "
                            f"ceiling {self.ceiling_gb:.1f}GB — never booted)"
                        )
                        r = Rung(
                            nxt,
                            "arith_refused",
                            f"predicted {predicted:.1f}GB > "
                            f"ceiling {self.ceiling_gb:.1f}GB",
                        )
                    else:
                        r = self.rung(cfg, cfg_path, nxt)
                        if nxt > max_booted:
                            max_booted = nxt
                            if r.peak_wired_gb > predicted:
                                overhead_gb += r.peak_wired_gb - predicted
                                self.log(
                                    f"    overhead re-measured: "
                                    f"+{r.peak_wired_gb - predicted:.1f}GB "
                                    f"(now {overhead_gb:.1f}GB)"
                                )
                    res.rungs.append(r)
                    self.save()
                    now = r.verdict == "pass"
                    if now:
                        best = max(best or 0, nxt)
                    current = nxt
                    if now != passed:  # state swapped
                        passed = now
                        direction = -direction
                        break
                    if nxt in (trained, self.args.resolution):
                        break  # ran out of room, not flipped

            # WHICH LIMIT DID WE HIT? A ceiling whose failures were all
            # `preflight_refused` measured OUR OWN GUARD, not the machine —
            # the value is safe and verified, but it is a lower bound and must
            # not be read as "the hardware stops here". The distinction is not
            # cosmetic: it decides whether raising OURO_PHYSICAL_SAFETY would
            # buy anything, and whether the 0.87 constant is being confirmed or
            # merely re-measured.
            fails = [g.verdict for g in res.rungs if g.verdict != "pass"]
            res.bound_by = (
                "guard"
                if fails
                and all(f in ("preflight_refused", "arith_refused") for f in fails)
                else "hardware" if fails else "trained-range"
            )
            res.ceiling = best
            hit = [g for g in res.rungs if g.verdict == "pass" and g.n_ctx == best]
            if hit and hit[0].kv_mib:
                res.kv_bytes_per_token_actual = int(
                    hit[0].kv_mib * 1048576 / hit[0].n_ctx
                )
            if best == trained:
                res.note = "ceiling is the trained range — nothing here limits it"
            elif best:
                delta = best - start
                where = (
                    "GUARD-BOUND (all failures were preflight refusals — "
                    "this is a lower bound, the hardware was never reached)"
                    if res.bound_by == "guard"
                    else "hardware-bound (failures were decode errors)"
                )
                res.note = (
                    f"start {start} -> measured {best} "
                    f"({delta:+d}, {abs(delta)/start*100:.1f}%) · {where}"
                )
            else:
                res.note = f"nothing decoded at or below {start}"
            return res
        finally:
            shutil.copy2(backup, cfg_path)
            self.log(f"    config restored from {backup}")

    def run(self) -> int:
        self.log(
            f"=== context ceiling probe · {len(self.args.configs)} models "
            f"· resolution {self.args.resolution} ==="
        )
        self.log(
            f"    config KV budget bypassed; ceiling {self.ceiling_gb:.1f}GB "
            f"({_PROBE_PHYSICAL_SAFETY} x physical), enforced on EVERY rung"
        )
        self.log("    ERROR-CODE guard ON — it is the instrument under test")
        self.log(f"    base: {self.base}")

        rec = ROOT / "dev/wired_mem_recorder.py"
        if rec.exists():
            self.rec = subprocess.Popen(
                [sys.executable, str(rec), "--label", "ctxprobe", "--hz", "10"],
                cwd=self.base,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.log(f"    wired recorder pid {self.rec.pid}")

        try:
            for cfg in self.args.configs:
                res = self.model(cfg)
                # AFTER model()'s finally-block restore, so the measurement is
                # written onto the pristine config rather than onto a rung.
                self.write_back(res)
                self.save()
        finally:
            if self.rec:
                self.rec.terminate()
            stop_server(self.log)
            (LLMVP / "active_config.txt").write_text(PRODUCTION)
            self.log(f"\nserver DOWN; active_config restored to {PRODUCTION}")

        self.log("\n=== SUMMARY ===")
        for r in self.results:
            kvt = (
                f"  KV {r.kv_bytes_per_token_actual / 1024:.0f} KiB/tok measured"
                if r.kv_bytes_per_token_actual
                else ""
            )
            self.log(
                f"  {r.config:<28} trained {r.trained_ctx:>7}  "
                f"config {r.original_n_ctx:>7}  CEILING "
                f"{str(r.ceiling):>7}   {r.note}{kvt}"
            )
        self.log("\n=== GUARD OBSERVATIONS (NOT a retirement verdict) ===")
        missed = [
            (r.config, g)
            for r in self.results
            for g in r.rungs
            if g.verdict != "pass" and not g.guard_saw
        ]
        caught = sum(
            1
            for r in self.results
            for g in r.rungs
            if g.verdict != "pass" and g.guard_saw
        )
        self.log(f"  failing rungs caught by an llama.cpp error code: {caught}")
        if missed:
            self.log("  Rungs that FAILED WITHOUT the guard seeing anything:")
            for c, g in missed:
                self.log(f"    {c} @ {g.n_ctx}: {g.verdict} — {g.detail[:70]}")

        # THIS BLOCK ONCE PRINTED "the arithmetic preflight is retirable" WHEN
        # `missed` WAS EMPTY. That inference is circular and was removed on
        # 2026-07-29 within hours of being written.
        #
        # Every rung this probe tests is chosen to sit UNDER the physical
        # ceiling — that is `calc_start`'s entire purpose, adopted precisely
        # because the previous version's second rung (6.3GB over physical) hard
        # rebooted the machine having emitted NO error code at all. So a clean
        # sweep here means "the codes cover the band the arithmetic confined us
        # to", which is not evidence about the band it kept us out of. A guard
        # cannot be exonerated by the absence of the failures it prevented.
        #
        # The counter-evidence is permanent and lives outside this run:
        # ctx_probe_20260729-095541 died at step-3.7 n_ctx 262144 with an empty
        # error log. Nothing measured under the ceiling can overturn it.
        self.log(
            "  NOTE: every rung here was chosen to sit under the physical "
            "ceiling, so this says nothing about the band above it."
        )
        self.log(
            "  The arithmetic preflight is NOT retirable — see "
            "ctx_probe_20260729-095541 (reboot, zero error codes)."
        )
        self.log(f"\nresults: {self.base / 'RESULTS.json'}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("configs", nargs="+")
    ap.add_argument("--resolution", type=int, default=2048)
    ap.add_argument("--gens", type=int, default=3)
    ap.add_argument("--boot-timeout", type=int, default=1200)
    ap.add_argument(
        "--start-fraction",
        type=float,
        default=1.0,
        help="Open below the trained range for a cautious first pass",
    )
    ap.add_argument(
        "--no-write",
        action="store_true",
        help="Measure without recording probe_verified_* into configs",
    )
    return Probe(ap.parse_args()).run()


if __name__ == "__main__":
    raise SystemExit(main())
