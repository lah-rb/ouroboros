#!/usr/bin/env python3
"""Canary harness — a pluggable probe loop for correlating server-health signals
with the "no-task confusion" / stub-emission degradation.

Each tick it snapshots the deep-health endpoint + macOS OS signals, then fires a
registry of PROBES and writes one merged JSONL row. Probes declare whether they
need an inference instance; under the single-instance pool (which starved the old
canary to 1/171 real fires) the instance-free probes still run every tick.

Registered probes:
  • ContaminationProbe (needs_instance=False) — PRIMARY. Tails the active run's
    trace and reports the generate_rewrite stub-emission rate (the real
    degradation signal). Reuses dev/contam_monitor.py. Pass --working-dir.
  • TaskBindingProbe (needs_instance=True) — the original "no-task confusion"
    canary (a derive-criteria prompt; checks thinking for confusion).
  • AuthoringProbe (needs_instance=True) — a MEMORYFUL session + small code-rewrite
    probe that exercises the resident-KV authoring path and classifies the
    RESPONSE as pass/stub/malformed (the path/task/signal the old canary missed).
    Best-effort: starved under single-instance, hence secondary to Contamination.

Usage:
  python3 dev/canary_probe.py --working-dir /tmp/run_xyz --interval 60 --out /tmp/canary.jsonl
  (Ctrl-C to stop; or --duration SECONDS.)
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

import httpx

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# dev/ on path for the shared classifier + trace tailer (single source of truth).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent.trace_health import is_stub  # noqa: E402
from contam_monitor import iter_rewrites, latest_trace  # noqa: E402

# ── TaskBindingProbe prompt (the original no-task-confusion canary) ────
CANARY_PROMPT = """You define the DONE criteria for the task in the ---YOUR TASK--- block
below — the shell checks that will later confirm it is complete. Your output is
parsed as JSON. Return ONLY a JSON object inside a fenced code block.

---YOUR TASK---
Merge user data from /data/source_a/users.json (primary) and
/data/source_b/users.csv into a unified dataset. Write the merged result to
/app/merged_users.parquet and a conflict report to /app/conflicts.json.
---END TASK---

Working directory: /app

Produce a "checks" array of robust shell checks that inspect the FINAL state
(files exist and are non-empty). Return ONLY the fenced JSON object, e.g.
```json
{"checks": [{"command": "test -s /app/x", "description": "x exists"}]}
```"""

CONFUSED = re.compile(
    r"haven.?t been given|no (specific|concrete|actual) (task|question|request)"
    r"|wait for (the )?next|don.?t know what the|hasn.?t given|unspecified task"
    r"|no further prompt|no action yet|we are stuck|need more info",
    re.I,
)

# ── AuthoringProbe prompt — exercises the resident-KV code-authoring path ──
AUTHOR_STATIC_PREFIX = (
    "---ACT AS---\nYou are a senior Python engineer. You rewrite whole files "
    "correctly and return the complete file — never a tool call or a diff.\n---END---"
)
AUTHOR_CANARY_PROMPT = """Here is the current file `counter.py`:

# === FILE: counter.py ===
```python
def increment(n: int) -> int:
    return n + 1
```

Task: add a function `decrement(n: int) -> int` that returns n - 1. Preserve
`increment`. Return the COMPLETE file in a single fenced python block, beginning
with the marker line `# === FILE: counter.py ===`."""

HEALTH_FIELDS = [
    "status", "availableInstances", "memProcessRssMb", "memSystemUsedPercent",
    "memSystemAvailableMb", "memSystemWiredMb", "flowCacheEntries", "residentActive",
    "flowBuilds", "flowHits", "flowEvicts", "flowFallbacks", "runawayCaptures",
    "trendSamples", "decodeTpsRecent", "decodeTpsBaseline", "throughputDrift",
    "ttftRecentS",
]


def _sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=6).stdout
    except Exception:
        return ""


def os_snapshot() -> dict:
    """macOS unified-memory / GPU signals the health endpoint does NOT report —
    the prime 'what degrades over uptime' suspects (compressor growth, swap,
    purgeable churn). Cheap; timeout-guarded. All sizes in GB."""
    out: dict = {}
    pg: dict = {}
    for line in _sh("vm_stat").splitlines():
        m = re.match(r'"?([A-Za-z][^:]+?)"?:\s+([0-9]+)\.?', line)
        if m:
            pg[m.group(1).strip()] = int(m.group(2))
    PS = 16384

    def gb(k):
        return round(pg.get(k, 0) * PS / 1e9, 2)

    out["free_gb"] = gb("Pages free")
    out["wired_gb"] = gb("Pages wired down")
    out["compressed_gb"] = gb("Pages occupied by compressor")
    out["anon_gb"] = gb("Anonymous pages")
    out["purgeable_gb"] = gb("Pages purgeable")
    out["swapins"] = pg.get("Swapins", 0)
    out["swapouts"] = pg.get("Swapouts", 0)
    mp = _sh("memory_pressure -Q 2>/dev/null")
    m = re.search(r"free percentage:\s*([0-9]+)%", mp)
    if m:
        out["mem_free_pct"] = int(m.group(1))
    ig = _sh("sysctl -n iogpu.wired_limit_mb 2>/dev/null").strip()
    if ig:
        out["iogpu_wired_limit_mb"] = ig
    bt = _sh("sysctl -n kern.boottime 2>/dev/null")
    m = re.search(r"sec = (\d+)", bt)
    if m:
        out["boot_age_h"] = round((time.time() - int(m.group(1))) / 3600, 2)
    return out


async def fetch_health(client: httpx.AsyncClient, endpoint: str) -> dict:
    q = "{ health { " + " ".join(HEALTH_FIELDS) + " } }"
    try:
        r = await client.post(endpoint, json={"query": q}, timeout=10)
        return r.json().get("data", {}).get("health", {}) or {}
    except Exception as e:
        return {"_health_error": str(e)}


def _alert(reason: str, row: dict) -> None:
    h = row.get("health", {})
    print("\n" + "!" * 70)
    print(f"!! CANARY ALERT @ {row['iso']} (+{row['elapsed_s']:.0f}s): {reason}")
    print(f"!!   mem avail={h.get('memSystemAvailableMb')}MB used%={h.get('memSystemUsedPercent')} "
          f"wired={h.get('memSystemWiredMb')}MB rss={h.get('memProcessRssMb')}MB")
    print(f"!!   flow fallbacks={h.get('flowFallbacks')} evicts={h.get('flowEvicts')} "
          f"runaways={h.get('runawayCaptures')}")
    print(f"!!   throughput_drift={h.get('throughputDrift')} decode_tps={h.get('decodeTpsRecent')}/"
          f"{h.get('decodeTpsBaseline')} ttft={h.get('ttftRecentS')}s")
    print("!" * 70 + "\n")


# ── Pluggable probe layer ─────────────────────────────────────────────


class Probe:
    """A health probe. Instance-free probes fire every tick; instance probes
    fire one-per-tick (rotated) only when the pool has a free instance."""

    name = "probe"
    needs_instance = False

    async def fire(self, ctx: dict) -> dict:
        """Return a dict merged into the JSONL row. ctx: endpoint, temp, working_dir, health."""
        return {}

    def alert(self, row: dict) -> str | None:
        """Return an alert reason (rising-edge; fire once), or None."""
        return None


class ContaminationProbe(Probe):
    """PRIMARY signal — tails the active run's trace for the generate_rewrite
    stub-emission rate. Needs NO instance, so the single-instance starvation that
    blinded the canary can't blind this. Reuses dev/contam_monitor.py."""

    name = "contam"
    needs_instance = False

    def __init__(self, window: int = 20):
        self._seen_path: str | None = None
        self._offset = 0
        self._win: deque[int] = deque(maxlen=window)
        self._cum_n = 0
        self._cum_stub = 0
        self._last_stub = ""
        self._win_armed = True
        self._cum_alerted = False

    async def fire(self, ctx: dict) -> dict:
        wd = ctx.get("working_dir")
        if not wd:
            return {"contam": "no-dir"}
        T = latest_trace(wd)
        if T and T != self._seen_path:
            self._seen_path, self._offset = T, 0
        new_lines: list[str] = []
        if T:
            try:
                with open(T) as fh:
                    fh.seek(self._offset)
                    new_lines = fh.readlines()
                    self._offset = fh.tell()
            except Exception:
                pass
        fresh = 0
        for cls, e in iter_rewrites(new_lines):
            fresh += 1
            self._cum_n += 1
            self._win.append(1 if cls == "stub" else 0)
            if cls == "stub":
                self._cum_stub += 1
                self._last_stub = (e.get("response_content") or "")[:120]
        win_rate = sum(self._win) / len(self._win) if self._win else 0.0
        cum_rate = self._cum_stub / max(self._cum_n, 1)
        return {
            "contam_window_rate": round(win_rate, 3),
            "contam_cum_rate": round(cum_rate, 3),
            "contam_fresh": fresh,
            "contam_total": self._cum_n,
            "contam_stub_total": self._cum_stub,
        }

    def alert(self, row: dict) -> str | None:
        wr = row.get("contam_window_rate", 0.0)
        cr = row.get("contam_cum_rate", 0.0)
        if len(self._win) >= 8 and wr >= 0.25:
            if self._win_armed:
                self._win_armed = False
                return (f"ContaminationProbe: window stub-rate {wr:.0%} ≥ 25% "
                        f"(last stub: {self._last_stub[:80]!r})")
        elif wr < 0.20:
            self._win_armed = True
        if not self._cum_alerted and self._cum_n >= 8 and cr >= 0.50:
            self._cum_alerted = True
            return f"ContaminationProbe: cumulative stub-rate {cr:.0%} ≥ 50% (run contaminated)"
        return None


class TaskBindingProbe(Probe):
    """The original no-task-confusion canary: a derive-criteria prompt; flags when
    the model's THINKING shows it lost task binding. Stateless completion."""

    name = "taskbind"
    needs_instance = True

    def __init__(self, temp_key: str = "temp"):
        self._flagged = False

    async def fire(self, ctx: dict) -> dict:
        from agent.effects.inference import InferenceEffect

        eff = InferenceEffect(ctx["endpoint"])
        try:
            res = await eff.run_inference(
                prompt=CANARY_PROMPT,
                config_overrides={"max_tokens": 600, "temperature": ctx["temp"]},
            )
        except Exception as e:
            return {"taskbind": "error", "taskbind_err": str(e)[:100]}
        try:
            think = await eff.fetch_thinking()
        except Exception:
            think = ""
        confused = bool(CONFUSED.search(think or ""))
        covered = False
        text = res.text or ""
        m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        raw = m.group(1) if m else text
        try:
            d = json.loads(raw)
            checks = d.get("checks", d if isinstance(d, list) else [])
            covered = "merged_users.parquet" in json.dumps(checks) and "conflicts.json" in json.dumps(checks)
        except Exception:
            pass
        return {
            "taskbind": "CONFUSED" if confused else ("ok" if covered else "wrong"),
            "taskbind_confused": confused,
            "taskbind_tokens": getattr(res, "generated_tokens", 0) or 0,
        }

    def alert(self, row: dict) -> str | None:
        if row.get("taskbind_confused") and not self._flagged:
            self._flagged = True
            return "TaskBindingProbe: CONFUSED — model lost task binding"
        return None


def _classify_author(text: str) -> str:
    """pass | stub | malformed for an AuthoringProbe response (inspect the TEXT)."""
    text = (text or "").strip()
    if is_stub(text):
        return "stub"
    m = re.search(r"```(?:python)?\s*(.+?)```", text, re.S)
    body = m.group(1) if m else text
    body = re.sub(r"^\s*#\s*===\s*FILE:.*?===\s*\n", "", body).strip()
    try:
        ast.parse(body)
    except SyntaxError:
        return "malformed"
    return "pass" if len(body) >= 60 else "malformed"


class AuthoringProbe(Probe):
    """Exercises the FAILING path: a memoryful session (resident-KV) + a small
    code-rewrite, classifying the RESPONSE as pass/stub/malformed. Best-effort —
    starved under single-instance, so secondary to ContaminationProbe."""

    name = "author"
    needs_instance = True

    def __init__(self):
        self._flagged = False

    async def fire(self, ctx: dict) -> dict:
        from agent.effects.inference import InferenceEffect

        eff = InferenceEffect(ctx["endpoint"])
        sid = None
        try:
            sid = await eff.start_session(
                {"ttl_seconds": 120},
                static_prefix=AUTHOR_STATIC_PREFIX,
                flow_key="canary:author:v1",
            )
            res = await eff.session_turn(
                sid, AUTHOR_CANARY_PROMPT,
                config_overrides={"max_tokens": 1200, "temperature": ctx["temp"]},
            )
        except Exception as e:
            return {"author": "error", "author_err": str(e)[:100]}
        finally:
            if sid:
                try:
                    await eff.end_session(sid)
                except Exception:
                    pass
        text = res.text or ""
        return {
            "author": _classify_author(text),
            "author_tokens": getattr(res, "generated_tokens", 0) or 0,
            "author_chars": len(text),
        }

    def alert(self, row: dict) -> str | None:
        cls = row.get("author")
        if cls in ("stub", "malformed") and not self._flagged:
            self._flagged = True
            return (f"AuthoringProbe: response was a {cls}, not code — the resident-KV "
                    f"authoring path is degrading")
        return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default="http://localhost:8008/graphql")
    ap.add_argument("--working-dir", default="",
                    help="active run dir (.agent/traces/) for the ContaminationProbe")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--duration", type=float, default=0.0, help="0 = until Ctrl-C")
    ap.add_argument("--temp", type=float, default=0.0,
                    help="probe temp (0.0 matches the run that broke)")
    # NOT /tmp: macOS wipes it on reboot/cleanup — a marathon canary's
    # time series is exactly the artifact that must survive (the July
    # corpus loss was this same trap).
    ap.add_argument("--out", default="llmvp/logs/canary_probe.jsonl")
    ap.add_argument("--drift-floor", type=float, default=0.7,
                    help="alert when throughput_drift falls below this")
    args = ap.parse_args()

    probes: list[Probe] = [ContaminationProbe(), TaskBindingProbe(), AuthoringProbe()]
    inst_probes = [p for p in probes if p.needs_instance]
    free_probes = [p for p in probes if not p.needs_instance]

    t0 = time.monotonic()
    f = open(args.out, "a", buffering=1)
    print(f"canary harness: {args.endpoint} every {args.interval}s; probes="
          f"{[p.name for p in probes]}; working_dir={args.working_dir or '(none)'}; log -> {args.out}")
    prev_fallbacks = None
    prev_swapouts = None
    prev_flow_builds = None  # server-restart detector (cumulative counter; resets on restart)
    tick = 0
    async with httpx.AsyncClient() as client:
        while True:
            elapsed = time.monotonic() - t0
            health = await fetch_health(client, args.endpoint)
            os_sig = os_snapshot()
            instance_free = bool(health.get("availableInstances", 0)) and "status" in health
            ctx = {"endpoint": args.endpoint, "temp": args.temp,
                   "working_dir": args.working_dir, "health": health}
            row = {"ts": time.time(), "iso": datetime.now(timezone.utc).isoformat(),
                   "elapsed_s": round(elapsed, 1), "health": health, "os": os_sig}

            # instance-free probes: every tick (these survive pool starvation)
            for p in free_probes:
                try:
                    row.update(await p.fire(ctx))
                except Exception as e:
                    row[p.name] = f"error:{str(e)[:40]}"
            # instance probes: at most ONE per tick (single pool), rotated; the
            # rest marked busy/skip so the row records why they didn't run.
            if instance_free and inst_probes:
                chosen = inst_probes[tick % len(inst_probes)]
                try:
                    row.update(await asyncio.wait_for(chosen.fire(ctx), timeout=90))
                except asyncio.TimeoutError:
                    row[chosen.name] = "timeout"
                for o in inst_probes:
                    row.setdefault(o.name, "skip")
            else:
                for p in inst_probes:
                    row[p.name] = "busy"

            f.write(json.dumps(row) + "\n")
            print(f"{elapsed:>7.0f}s  contam={str(row.get('contam_window_rate','-')):>5}/"
                  f"{str(row.get('contam_cum_rate','-')):<5} tb={str(row.get('taskbind','-')):<8} "
                  f"author={str(row.get('author','-')):<9} | availMB={str(health.get('memSystemAvailableMb')):>7} "
                  f"wired={str(health.get('memSystemWiredMb')):>7} drift={str(health.get('throughputDrift')):>5} "
                  f"| cmp={os_sig.get('compressed_gb')}G swpout={os_sig.get('swapouts')}")

            # probe alerts (rising-edge)
            for p in probes:
                reason = p.alert(row)
                if reason:
                    _alert(reason, row)
            # health/OS-level alerts (independent of probes)
            fbk = health.get("flowFallbacks")
            drift = health.get("throughputDrift")
            fbuilds = health.get("flowBuilds")
            # Server-restart boundary: the cumulative flowBuilds counter dropped, i.e.
            # the process restarted. Mark it loudly (so alerts ABOVE this line in the
            # log aren't mistaken for current state — e.g. a stale throughput_drift
            # from a now-replaced soured server) and reset the cross-tick delta-trackers
            # so the next deltas compare against the FRESH process, not the old one.
            if prev_flow_builds is not None and fbuilds is not None and fbuilds < prev_flow_builds:
                _alert(f"🔄 SERVER RESTART (flowBuilds {prev_flow_builds}->{fbuilds}) — "
                       f"alerts above this line are STALE; cross-tick trackers reset", row)
                prev_fallbacks = None
                prev_swapouts = None
            prev_flow_builds = fbuilds if fbuilds is not None else prev_flow_builds
            if prev_fallbacks is not None and fbk is not None and fbk > prev_fallbacks:
                _alert(f"flow_fallbacks rose {prev_fallbacks}->{fbk} (KV-cache instability)", row)
            if drift is not None and drift < args.drift_floor:
                _alert(f"throughput_drift {drift} < {args.drift_floor} (generation slowdown)", row)
            if prev_swapouts is not None and os_sig.get("swapouts", 0) > prev_swapouts:
                _alert(f"SWAPOUTS rose {prev_swapouts}->{os_sig.get('swapouts')} "
                       f"(compressed={os_sig.get('compressed_gb')}G — unified-memory pressure)", row)
            prev_fallbacks = fbk if fbk is not None else prev_fallbacks
            prev_swapouts = os_sig.get("swapouts", prev_swapouts)

            tick += 1
            if args.duration and elapsed >= args.duration:
                break
            await asyncio.sleep(args.interval)
    print(f"\ncanary: done ({round(time.monotonic()-t0)}s). log: {args.out}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ncanary: stopped.")
