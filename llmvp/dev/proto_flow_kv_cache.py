#!/usr/bin/env python3
"""Prototype + measurement: per-flow static-prefix KV caching.

THE IDEA (flow ownership of cached static content): a flow declares its static
head (persona + fixed instructions — the invariant ~60-80% of a plan_charter /
plan_provision / judge prompt). The framework pins that head's KV once and, on
every subsequent visit, prefills ONLY the dynamic tail (the per-cycle feedback)
instead of re-prefilling the whole head.

THE MECHANISM ALREADY EXISTS for ONE prefix: the global static buffer (SOUL.md /
knowledge) is evaluated once at warm-up and snapshotted with save_state(); every
request restores it with load_state() and prefills only the dynamic remainder
(llama_cpp_backend.py: _warm_static_state / acquire_instance / generate_stream_sync,
where `n_static = self._static_state.n_tokens` is skipped). "hello world" returns
in ~175 ms despite a ~1,809-token static buffer — proof the buffer is pinned, not
re-prefilled. The per-flow idea is the SAME save_state/load_state primitive keyed
per-flow instead of one global key.

THE PRIZE (measured here, gpt-oss-120b, via /v1/completions, max_tokens=1):
    [flow_static(~1170 tok) + dynamic(~60 tok)]  re-prefilled every cycle : ~1593 ms
    [dynamic only]                               flow_static cached       : ~ 355 ms
    => ~1238 ms reclaimed PER CYCLE for one static-heavy step. Across an ops loop
       (~2 such fresh inferences × ~6 cycles) ~15 s/task (~3% of a 540 s budget);
       larger on code_core (many more inferences/task) and on slower models.

IMPLEMENTATION CONSTRAINT (found the hard way): gpt-oss uses sliding-window
attention (SWA). Driving the per-flow snapshot via RAW llama_cpp Llama.eval()
after a save_state/load_state cycle trips "Invalid input batch" — the SWA cache
state isn't cleanly round-tripped by raw eval (this is the documented save_state
fragility locus; cf. the qwen3.x degeneration that full-replay fixed). The
production backend AVOIDS this by going through load_state()+generate(reset=False),
never reset()+eval() loops. So the real per-flow cache must extend that existing
path (a dict of per-flow saved states, skip [global + flow_static] instead of just
[global]) — NOT a new raw-eval path.

This script measures the prize against the running server (no save_state needed —
it compares prefill cost by prompt length). Run with the server up on gpt-oss.
"""
import json, statistics, time, urllib.request

URL = "http://localhost:8008/v1/completions"
CYCLES = 6

FLOW_STATIC = (
    "You are a terminal-operations module in an automated agent pipeline. Your "
    "job each cycle is to plan the next batch of shell commands that move the "
    "task toward its definition of done, then stop and observe. Operate directly "
    "in the container's working directory. Prefer robust idempotent commands. "
    "Never destroy a correct artifact. "
) * 18  # ~realistic static-head size (~1170 tok)

DYN = (
    "Cycle feedback: the previous attempt left output.txt empty; the transform "
    "step exited 2 and the log shows a parse error near row 14. Current terminal "
    "tail shows the file is present but zero bytes. Plan the next commands."
)


def ms(prompt, n=3):
    xs = []
    for _ in range(n):
        data = json.dumps(
            {"prompt": prompt, "max_tokens": 1, "temperature": 0}
        ).encode()
        req = urllib.request.Request(
            URL, data=data, headers={"Content-Type": "application/json"}
        )
        t = time.monotonic()
        try:
            urllib.request.urlopen(req, timeout=120).read()
        except Exception as e:  # noqa: BLE001
            print("request failed:", e)
            return -1
        xs.append((time.monotonic() - t) * 1000)
    return statistics.median(xs)


def main():
    ms("warm")
    full = ms(FLOW_STATIC + "\n\n" + DYN)
    dyn = ms(DYN)
    floor = ms("ok")
    saving = full - dyn
    print(f"[flow_static + dynamic] (today, re-prefilled): {full:.0f} ms")
    print(f"[dynamic only]          (flow_static cached):  {dyn:.0f} ms")
    print(f"floor (~1 tok):                                {floor:.0f} ms")
    print(f"\nper-cycle saving caching flow_static (~1170 tok): ~{saving:.0f} ms")
    print(f"over {CYCLES} cycles, ONE static-heavy step:       ~{CYCLES*saving:.0f} ms")


if __name__ == "__main__":
    main()
