#!/usr/bin/env python3
"""Forced-drain repro for the 2026-08-03 swarm context-refresh kill.

    # boot: llmvp/active_config.txt -> swarm-refresh-probe, start api/main.py
    python3 dev/refresh_drain_probe.py <server.log>

Holds ONE long generation live while the accelerated refresh cap fires, so
the drain must time out and force-clear. Short requests run alongside to keep
request boundaries ticking. Then watches the server log for the outcome.

Reads the log rather than the API on purpose: the failure mode is the process
dying, and a dead process answers no queries.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request

URL = "http://localhost:8008/graphql"

LONG_PROMPT = (
    "Write an exhaustive design document for a text-adventure engine: module "
    "layout, every data structure, the save schema, the combat resolution "
    "rules, and a worked example of each. Be extremely thorough and do not "
    "stop early."
)
SHORT_PROMPT = "Name one Python standard-library module. One word."

_stop = threading.Event()
_events: list[str] = []


def _gql(payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        URL, data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _completion(prompt: str, max_tokens: int, timeout: float) -> str:
    r = _gql(
        {
            "query": "query($r: CompletionRequest!){ completion(request:$r){ text } }",
            "variables": {
                "r": {
                    "prompt": prompt,
                    "maxTokens": max_tokens,
                    "temperature": 0.7,
                }
            },
        },
        timeout,
    )
    return (r.get("data") or {}).get("completion", {}).get("text", "") or ""


def _pinned_session() -> None:
    """THE ACTUAL STRAGGLER: a session that pins its seat and then goes IDLE.

    Rev 1 and rev 2 both drove stateless completions, which release their
    seat every request — so the drain always cleared naturally and the force
    path was never reached (`forced drain: False`). That was the wrong client
    shape. In the production kill, the thing that held the drain for its full
    300s window was a SESSION: sessions pin their seat across turns by design
    (the resident-seq cache), so `_checked_out` never returns to zero while
    one is alive, no matter how long the window is. The absence of an
    "evicted N stream(s)" line at the production deadline proves the
    generations had already finished — only the pinned seat remained.

    So: open a session, take ONE turn to pin the seat, then hold it idle
    across the refresh cap and let the drain time out against it.
    """
    t0 = time.monotonic()
    try:
        r = _gql(
            {
                "query": "mutation($c: SessionConfig){ startSession(config:$c)"
                "{ sessionId } }",
                "variables": {"c": {"ttlSeconds": 1800}},
            },
            timeout=300,
        )
        sid = (r.get("data") or {}).get("startSession", {}).get("sessionId")
        if not sid:
            _events.append(f"session start returned no id: {str(r)[:200]}")
            return
        _events.append(f"[{time.monotonic()-t0:6.1f}s] session {sid[:12]} pinned")
        # One turn to make the pin real (KV resident on the seat), then idle.
        _completion(SHORT_PROMPT, 24, timeout=300)
        _events.append(
            f"[{time.monotonic()-t0:6.1f}s] session idle, holding seat across the cap"
        )
        while not _stop.is_set():
            time.sleep(1.0)
    except Exception as exc:  # noqa: BLE001 — the death is the datum
        _events.append(f"[{time.monotonic()-t0:6.1f}s] session path failed: {exc}")


def _short_traffic() -> None:
    """Keep request boundaries ticking so the refresh check runs."""
    t0 = time.monotonic()
    n = 0
    while not _stop.is_set():
        try:
            _completion(SHORT_PROMPT, 24, timeout=120)
            n += 1
        except Exception as exc:  # noqa: BLE001
            _events.append(f"[{time.monotonic()-t0:6.1f}s] short #{n} failed: {exc}")
            return
        time.sleep(2.0)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    log_path = sys.argv[1]
    t0 = time.monotonic()

    print("── pinning a session (the straggler) + short traffic")
    long_t = threading.Thread(target=_pinned_session, daemon=True)
    short_t = threading.Thread(target=_short_traffic, daemon=True)
    long_t.start()
    time.sleep(1.0)
    short_t.start()

    # cap 120s + drain 20s + rebuild ~10s, plus slack.
    deadline = time.monotonic() + 300
    seen: set[str] = set()
    markers = (
        "proactive refresh",
        "refresh drain",
        "force-expired",
        "evicted",
        "Context refresh aborted",
        "drain failed to clear",
        "context rebuilt",
        "MTL0 KV buffer size",
        "rebuild memory",
    )
    while time.monotonic() < deadline:
        try:
            with open(log_path, errors="ignore") as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
        for line in lines[-400:]:
            for m in markers:
                if m in line and line.strip() not in seen:
                    seen.add(line.strip())
                    print(f"  [{time.monotonic()-t0:6.1f}s] {line.strip()[:150]}")
        time.sleep(1.0)

    _stop.set()
    time.sleep(1.0)
    print("\n── client-side events")
    for e in _events:
        print(f"  {e}")

    kv_allocs = sum(1 for s in seen if "MTL0 KV buffer size" in s)
    aborted = any("Context refresh aborted" in s for s in seen)
    rebuilt = any("context rebuilt" in s for s in seen)
    forced = any("force-expired" in s or "evicted" in s for s in seen)
    print("\n── verdict")
    print(f"  forced drain (force-expire/evict) : {forced}")
    print(f"  refresh aborted (the guard fired) : {aborted}")
    print(f"  context rebuilt cleanly           : {rebuilt}")
    print(f"  KV buffer allocations logged      : {kv_allocs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
