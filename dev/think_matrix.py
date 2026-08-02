#!/usr/bin/env python3
"""Fleet thinking-level evidence matrix (operator, 2026-08-03).

Boots each REPRESENTATIVE config, drives one completion per canonical level
(None / low / medium / high), and records the token evidence per arm:
tokens_generated, raw-vs-stripped char lengths (from the server log), and
marker presence. "If we are asserting that a family/model exhibits a
behavior, I want evidence here before it can effect further tests."

Representatives (overlapping siblings skipped by design — the fastest of a
suite represents it): qwen3.6-35b-a3 for the qwen trio; gemma-4-31b for the
gemma quants; laguna-xs for the laguna dial (laguna-s covers mode:off).

Every expectation below is PRE-REGISTERED and never gates execution; the
report records what happened.

    cd llmvp && ../.venv-or-.venv/bin/python ../dev/think_matrix.py

Writes dev/think_matrix_results.json + prints a row per arm.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import signal
import subprocess
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
LLMVP = HERE.parent / "llmvp"
URL = "http://localhost:8008/graphql"
RESULTS = HERE / "think_matrix_results.json"

TASK = (
    "A train travels 240 km in 3 hours, then 180 km in 2 hours. What is its "
    "average speed for the whole journey? Answer with the number."
)

# (config, mode, per-level expectation summary — PRE-REGISTERED)
MATRIX = [
    ("laguna-xs-2.1",          "per_request", "None/low: no CoT (close-only). med/high: CoT via <think> opener"),
    ("laguna-s-2.1-apex",      "off",         "ALL levels: no CoT (policy off, close-only always)"),
    ("devstral-2-small-24b",   "unavailable", "ALL levels: bare answer; no MODEL_SETTINGS, no [THINK]"),
    ("olmo-3.1-32b-instruct",  "unavailable", "ALL levels: bare answer, no think markers"),
    ("olmo-3.1-32b-think",     "on",          "ALL levels: CoT (dial-less think model, opener prefilled)"),
    ("gemma-4-31b",            "per_request", "None/low: no CoT (padding + pre-closed). med/high: CoT, model opens channel"),
    ("glm-4.7-flash",          "on",          "ALL levels: CoT (always-think policy)"),
    ("qwen3.6-35b-a3",         "on",          "ALL levels: CoT incl. a LOW request (on overrides — always think, never adapt)"),
    ("qwen3-next-coder-80b-a3","unavailable", "ALL levels: bare answer (dial-less coder)"),
    ("gpt-oss-120b-a5",        "per_request", "CoT at every level (harmony always reasons); LENGTH scales low<med<high via head-swap"),
    ("mistral-medium-3.5-128b","per_request", "None/low: direct answer. high: [THINK] CoT via head-swap (med collapses to none)"),
    ("step37-flash-196b-a11",  "per_request", "None/low: NO CoT (gate closed, opener omitted). med/high: CoT, depth scales"),
    ("hy3-reap-200b-a21",      "per_request", "None/low: no_think, direct. med: low-effort CoT. high: deep CoT (RE-TEST: None routing changed since the 08-03 validation)"),
]

LEVELS = [None, "low", "medium", "high"]


def gql(query: str, variables: dict, timeout: float) -> dict:
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wait_ready(log_path: str, proc: subprocess.Popen, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if re.search(
                r"Application startup complete|Uvicorn running",
                open(log_path, errors="ignore").read(),
            ):
                return True
        except OSError:
            pass
        time.sleep(5)
    return False


def last_raw_strip(log_path: str) -> tuple:
    """(raw_len, strip_len) from the newest run_completion pair, else (0,0)."""
    raw = strip = 0
    try:
        for line in open(log_path, errors="ignore").readlines()[-200:]:
            m = re.search(r"raw answer len=(\d+)", line)
            if m:
                raw = int(m.group(1))
                strip = raw  # families without a strip log keep raw==strip
            m = re.search(r"after strip len=(\d+)", line)
            if m:
                strip = int(m.group(1))
    except OSError:
        pass
    return raw, strip


def main() -> int:
    results = []
    if RESULTS.exists():
        try:
            results = json.loads(RESULTS.read_text())
        except Exception:
            results = []
    done = {r["config"] for r in results if r.get("complete")}

    for config, mode, expect in MATRIX:
        if config in done:
            print(f"◦ {config}: already recorded, skipping")
            continue
        print(f"══ {config} (mode={mode}) ══")
        (LLMVP / "active_config.txt").write_text(config + "\n")
        log_path = f"/tmp/think_matrix_{config}.log"
        with open(log_path, "w") as lf:
            proc = subprocess.Popen(
                [str(LLMVP / ".venv/bin/python"), "api/main.py"],
                cwd=str(LLMVP),
                stdout=lf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        rec = {"config": config, "mode": mode, "expect": expect, "arms": {},
               "complete": False}
        try:
            if not wait_ready(log_path, proc, 1200):
                rec["error"] = "boot failed/timeout"
                print(f"  !! boot failed")
                continue
            for lvl in LEVELS:
                req: dict = {"prompt": TASK, "maxTokens": 4096,
                             "temperature": 0.7}
                if lvl:
                    req["reasoning"] = lvl
                t0 = time.monotonic()
                try:
                    r = gql(
                        "query($r: CompletionRequest!){ completion(request:$r)"
                        "{ text tokensGenerated truncated } }",
                        {"r": req},
                        timeout=1800,
                    )
                    data = (r.get("data") or {}).get("completion") or {}
                    text = data.get("text", "") or ""
                    raw, strip = last_raw_strip(log_path)
                    arm = {
                        "tokens_generated": data.get("tokensGenerated", 0),
                        "content_chars": len(text),
                        "raw_chars": raw,
                        "strip_chars": strip,
                        "cot_chars": max(0, raw - strip),
                        "truncated": bool(data.get("truncated")),
                        "wall_s": round(time.monotonic() - t0, 1),
                        "first60": text[:60],
                        "errors": (str(r.get("errors"))[:160]
                                   if r.get("errors") else None),
                    }
                except Exception as exc:  # noqa: BLE001 — record and continue
                    arm = {"errors": str(exc)[:200],
                           "wall_s": round(time.monotonic() - t0, 1)}
                rec["arms"][str(lvl)] = arm
                print(f"  {str(lvl):7} tok={arm.get('tokens_generated','?'):>5} "
                      f"cot_chars={arm.get('cot_chars','?'):>6} "
                      f"content={arm.get('content_chars','?'):>5} "
                      f"err={arm.get('errors')}")
            rec["complete"] = True
        finally:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                proc.terminate()
            deadline = time.monotonic() + 240
            while proc.poll() is None and time.monotonic() < deadline:
                time.sleep(2)
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            results = [r for r in results if r["config"] != config] + [rec]
            RESULTS.write_text(json.dumps(results, indent=2))
    print(f"\nmatrix complete -> {RESULTS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
