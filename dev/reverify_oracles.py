#!/usr/bin/env python3
"""Offline oracle reverify — rescore preserved tb missions WITHOUT re-running.

Models tools/pdf_extract/reverify.py: re-apply an oracle to on-disk artifacts and
re-derive the verdict, so an oracle fix never costs an 80-task re-sweep. The
container's answer file is NOT preserved (only mission.json + traces, per
adapters/tb/agent.py:_preserve), so this is a TRACE rescorer: it re-applies the
deterministic non-degeneracy FLOOR (oracle_actions._degenerate_reason) to the
produced-artifact content AS PRINTED in the trace (mcp_tool_call result_previews
+ judge inference prompt/response), and cross-references whether the mission was
marked complete. Artifacts the session never printed are UNRECOVERABLE — reported,
not scored.

Headline metric: missions marked complete whose recovered answer the sanity floor
would FAIL = false-passes the rung would have caught (the count-dataset "0" class).

Usage:  python dev/reverify_oracles.py [runs/<run-id> ...]   (default: runs/)
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.actions.oracle_actions import (  # noqa: E402
    _degenerate_reason,
    _extract_artifact_path,
)

ROOT = Path(__file__).resolve().parent.parent


def _trace_text(mission_dir: Path) -> str:
    """Everything the session printed: mcp result previews + inference contents."""
    parts: list[str] = []
    for jl in sorted((mission_dir / "traces").glob("*.jsonl")):
        for line in jl.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            for k in ("result_preview", "prompt_content", "response_content"):
                v = e.get(k)
                if isinstance(v, str) and v:
                    parts.append(v)
    return "\n".join(parts)


def _recover_answer(path: str, blob: str) -> str | None:
    """The produced artifact's content as printed in the trace — CONSERVATIVE.
    Two reliable signals only; anything else is unrecoverable (reported, not
    scored — never guess from criteria text or source code):

      1. EXACT — the sanity rung's own published excerpt ``<path>:\\n<content>``
         (present on post-rung runs; makes reverify exact going forward).
      2. cat-ECHO — a ``cat <path|base>`` line in the captured session, whose very
         next non-empty line is the printed content (must look like a value, not
         code/prose: short, no trailing ``{`` / ``:`` / ``=``).
    """
    base = path.rsplit("/", 1)[-1]
    # 1. exact rung excerpt
    m = re.search(re.escape(path) + r":\n([^\n]{1,400})", blob)
    if m:
        return m.group(1).strip()
    # 2. strict cat-echo
    lines = blob.splitlines()
    for i, ln in enumerate(lines):
        if re.search(r"\bcat\s+\S*" + re.escape(base), ln):
            nxt = next((x.strip() for x in lines[i + 1 : i + 3] if x.strip()), "")
            if nxt and len(nxt) <= 80 and nxt[-1] not in "{:=,(" and "def " not in nxt:
                return nxt[:400]
    return None


def reverify(run_dirs: list[Path]) -> None:
    missions = []
    for rd in run_dirs:
        missions += list(rd.rglob("agent-logs/ouroboros-mission"))
    if not missions:
        print("no preserved missions found under:", *[str(r) for r in run_dirs])
        return

    caught, clean, unrecoverable, not_answer = [], [], [], []
    for md in missions:
        mj = md / "mission.json"
        if not mj.exists():
            continue
        m = json.loads(mj.read_text(errors="replace"))
        if (m.get("config") or {}).get("flow_set") != "ops":
            continue
        task = (
            md.parts[md.parts.index("runs") + 2]
            if "runs" in md.parts
            else m.get("id", "?")
        )
        td = m.get("task_definition") or {}
        criteria = td.get("completion_criteria") or []
        path = _extract_artifact_path(criteria)
        completed = m.get("status") == "completed" or any(
            g.get("type") == "task_exec" and g.get("status") == "complete"
            for g in (m.get("goals") or [])
        )
        if not path:
            not_answer.append(task)
            continue
        answer = _recover_answer(path, _trace_text(md))
        if answer is None:
            unrecoverable.append((task, path))
            continue
        reason = _degenerate_reason(answer, criteria)
        row = (task, path, answer[:40], completed, reason)
        (caught if reason else clean).append(row)

    print(f"\n=== oracle reverify — {len(missions)} preserved mission(s) ===")
    print(f"  answer-producing ops missions scored: {len(caught) + len(clean)}")
    print(f"  non-answer tasks (rung N/A):          {len(not_answer)}")
    print(f"  unrecoverable (answer never printed): {len(unrecoverable)}")
    if caught:
        print("\n  WOULD-FLAG (sanity floor fails the recovered answer):")
        for task, path, ans, done, reason in caught:
            tag = "FALSE-PASS" if done else "agreed-fail"
            print(f"    [{tag}] {task}: {path} = {ans!r}\n        → {reason}")
    if clean:
        print(f"\n  clean (floor passes): {', '.join(r[0] for r in clean)}")
    if unrecoverable:
        print("\n  unrecoverable (report, not scored):")
        for task, path in unrecoverable:
            print(f"    {task}: {path}")
    false_pass = sum(1 for r in caught if r[3])
    print(
        f"\n  HEADLINE: {false_pass} false-pass(es) the sanity rung would have caught."
    )


if __name__ == "__main__":
    args = [Path(a) for a in sys.argv[1:]] or [ROOT / "runs"]
    reverify(args)
