#!/usr/bin/env python3
"""Live contamination-rate monitor — surfaces (does NOT suppress) the
generate_rewrite stub-emission rate during a real run by tailing the trace.

This is the PRIMARY server-health signal: the agent's own generate_rewrite
outputs degrade into agentic action-stubs (`{"action":"read_file"}`) as the
server goes stale, at a rate that rises with uptime (verified 69-94% on the
preserved corpus). Unlike the canary probe it needs NO inference instance, so
the single-instance pool starvation that blinded the canary (1/171 probes) does
not blind this — it reads the rate straight from the trace jsonl.

Detection only: strictly read-only over the run's history store
(.agent/history, read incrementally by run/seq) or, for an archive that
predates it, the legacy .agent/traces JSONL. The agent still
writes the stub to disk, so the contamination "shape" stays visible (by design
— it's the indicator we want surfaced, not hidden).

Reuses the canonical classifier `is_stub` from agent/trace_health.py.

Usage:
  python dev/contam_monitor.py --working-dir /tmp/run_xyz --interval 30
  python dev/contam_monitor.py --replay --working-dir runs/capture_corpus/capture_card_game_3
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

# dev/ is on sys.path[0] when run as `python dev/contam_monitor.py`; import the
# single-source-of-truth classifier rather than re-implementing it.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.trace_health import is_stub  # noqa: E402
from agent.history.reader import has_history, load_events  # noqa: E402

# generate_rewrite with empty response_content (run lacked --trace-prompts) and
# fewer than this many generated tokens is treated as a stub: corpus stubs were
# 170-192 gen-tok vs 1300-1700 for real files, so the gap is wide.
STUB_TOKEN_PROXY = 250


def _isonow() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest_trace(working_dir: str) -> str | None:
    """The run's record: the ``.agent`` dir when it holds a history store,
    else the newest legacy JSONL trace path. Feed it to ``tail_events``."""
    agent_dir = os.path.join(working_dir, ".agent")
    if has_history(agent_dir):
        return agent_dir
    g = sorted(
        glob.glob(os.path.join(working_dir, ".agent/traces/*.jsonl")),
        key=os.path.getmtime,
    )
    return g[-1] if g else None


def tail_events(source: str, cursor):
    """Events past ``cursor`` from ``source`` (see latest_trace), plus the new
    cursor. A store's cursor is the (run_id, seq) of the last event seen; a
    legacy JSONL file's is a byte offset. ``None`` = from the start."""
    if os.path.isdir(source):
        events = load_events(source)
        if cursor is not None:
            idx = next(
                (
                    i
                    for i, e in enumerate(events)
                    if (e["_history"]["run_id"], e["_history"]["seq"]) == tuple(cursor)
                ),
                None,
            )
            if idx is not None:
                events = events[idx + 1 :]
        if events:
            h = events[-1]["_history"]
            cursor = (h["run_id"], h["seq"])
        return events, cursor
    offset = int(cursor or 0)
    lines: list[str] = []
    try:
        with open(source) as fh:
            fh.seek(offset)
            lines = fh.readlines()
            offset = fh.tell()
    except Exception:
        pass
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out, offset


def classify(ev: dict) -> str:
    """'stub' | 'code' for a generate_rewrite inference_call. Primary = is_stub on
    response_content; fallback = token-size when response_content is empty."""
    rc = ev.get("response_content") or ""
    if rc.strip():
        return "stub" if is_stub(rc) else "code"
    gen = int(ev.get("generated_tokens") or ev.get("tokens_out") or 0)
    return "stub" if (0 < gen < STUB_TOKEN_PROXY) else "code"


def iter_rewrites(items):
    """Yield (classification, event) for generate_rewrite inference_calls.
    ``items`` are event dicts (from tail_events) or raw JSONL lines."""
    for item in items:
        if isinstance(item, str):
            item = item.strip()
            if not item:
                continue
            try:
                e = json.loads(item)
            except Exception:
                continue
        else:
            e = item
        if (
            e.get("event_type") == "inference_call"
            and e.get("step") == "generate_rewrite"
        ):
            yield classify(e), e


def _alert(reason: str, win_rate: float, cum_rate: float, last_stub_head: str) -> None:
    print("\n" + "!" * 72)
    print(f"!! CONTAMINATION ALERT: {reason}")
    print(f"!!   window_rate={win_rate:.0%}  cumulative_rate={cum_rate:.0%}")
    if last_stub_head:
        # Surface the actual shape — that's the point, we are not hiding it.
        print(f"!!   last stub: {last_stub_head[:120]!r}")
    print("!" * 72 + "\n")


def replay(working_dir: str) -> tuple[int, int]:
    """One-shot: process the whole latest trace and report the stub-rate
    (verification mode — same generate_rewrite event selection as the trace forensics).
    """
    T = latest_trace(working_dir)
    if not T:
        print(f"{os.path.basename(working_dir)}: NO TRACE")
        return 0, 0
    n = stubs = 0
    heads = []
    events, _ = tail_events(T, None)
    for cls, e in iter_rewrites(events):
        n += 1
        if cls == "stub":
            stubs += 1
            if len(heads) < 3:
                heads.append((e.get("response_content") or "")[:80])
    rate = stubs / max(n, 1)
    tripped = " <ALERT ≥ 25%>" if (rate >= 0.25 and n >= 8) else ""
    print(
        f"{os.path.basename(working_dir)}: generate_rewrite {stubs}/{n} stub ({rate:.0%}){tripped}"
    )
    for h in heads:
        print(f"    stub: {h!r}")
    return stubs, n


def live(working_dir: str, interval: float, out: str, window: int) -> None:
    f = open(out, "a", buffering=1) if out else None
    seen_path = None
    offset = None  # tail_events cursor: None = from the start
    win: deque[int] = deque(maxlen=window)
    cum_n = cum_stub = 0
    last_stub_head = ""
    win_armed = True  # rising-edge state for the window alert (hysteresis)
    cum_alerted = False
    print(
        f"contam-monitor: tailing {working_dir} every {interval:.0f}s "
        f"(window={window}); log -> {out or '(stdout only)'}"
    )
    while True:
        T = latest_trace(working_dir)
        if T and T != seen_path:
            seen_path, offset = T, None  # new run/source -> tail from its start
        new_lines: list = []
        if T:
            new_lines, offset = tail_events(T, offset)
        fresh = 0
        for cls, e in iter_rewrites(new_lines):
            fresh += 1
            cum_n += 1
            win.append(1 if cls == "stub" else 0)
            if cls == "stub":
                cum_stub += 1
                last_stub_head = (e.get("response_content") or "")[:120]
        win_rate = sum(win) / len(win) if win else 0.0
        cum_rate = cum_stub / max(cum_n, 1)
        row = {
            "ts": time.time(),
            "iso": _isonow(),
            "fresh": fresh,
            "rewrites_total": cum_n,
            "stub_total": cum_stub,
            "window_rate": round(win_rate, 3),
            "cumulative_rate": round(cum_rate, 3),
            "last_stub": last_stub_head,
        }
        if f:
            f.write(json.dumps(row) + "\n")
        print(
            f"[{row['iso']}] rewrites={cum_n} stub={cum_stub} "
            f"window={win_rate:.0%} cum={cum_rate:.0%} (+{fresh})"
        )

        # Window alert: rising-edge at >=0.25 over >=8 events; re-arm below 0.20.
        if len(win) >= 8 and win_rate >= 0.25:
            if win_armed:
                _alert(
                    f"window stub-rate {win_rate:.0%} ≥ 25%",
                    win_rate,
                    cum_rate,
                    last_stub_head,
                )
                win_armed = False
        elif win_rate < 0.20:
            win_armed = True
        # Cumulative alert: fires once when the run is decisively contaminated.
        if not cum_alerted and cum_n >= 8 and cum_rate >= 0.50:
            cum_alerted = True
            _alert(
                "cumulative stub-rate crossed 50% (run contaminated, not a blip)",
                win_rate,
                cum_rate,
                last_stub_head,
            )
        time.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--working-dir", required=True, help="run dir containing .agent/traces/"
    )
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument(
        "--window", type=int, default=20, help="rolling window of generate_rewrites"
    )
    ap.add_argument("--out", default="/tmp/contam_monitor.jsonl")
    ap.add_argument(
        "--replay",
        action="store_true",
        help="process the trace once and report (verification), no loop",
    )
    args = ap.parse_args()
    if args.replay:
        replay(args.working_dir)
    else:
        live(args.working_dir, args.interval, args.out, args.window)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ncontam-monitor: stopped.")
