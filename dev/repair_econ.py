#!/usr/bin/env python3
"""Join repair_econ notes against llmvp interaction logs.

The parallel structural mode tags every repair dispatch with a
machine-parseable note (see _note_repair_econ in mission_actions.py):

    repair_econ stage=diagnose file=engine.py class=syntax size_bytes=1234

This script brackets each note's episode by the next note's timestamp
(capped) and sums the LLM read/write tokens that fall inside the window
from interactions.jsonl — producing the per-failure-class economics
table that decides regenerate-vs-diagnose tiering thresholds.

Token estimates: write = generated_tokens where logged (session turns),
else len(raw_text)//4; read = len(prompt)//4. Same estimators used for
the pre-implementation baseline (diagnose ≈ 12.7k read / 3.3k write
median), so the numbers are comparable.

Usage:
    uv run python dev/repair_econ.py [mission.json] [interactions.jsonl]
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

_NOTE_RE = re.compile(
    r"repair_econ stage=(?P<stage>\S+) file=(?P<file>\S+) "
    r"class=(?P<cls>\S+) size_bytes=(?P<size>\d+)"
)
_EPISODE_CAP = timedelta(minutes=20)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_episodes(mission_path: Path) -> list[dict]:
    mission = json.loads(mission_path.read_text())
    episodes = []
    for note in mission.get("notes", []):
        m = _NOTE_RE.search(note.get("content", ""))
        if not m:
            continue
        episodes.append(
            {
                **m.groupdict(),
                "size": int(m.group("size")),
                "start": _ts(note["timestamp"]),
            }
        )
    episodes.sort(key=lambda e: e["start"])
    for cur, nxt in zip(episodes, episodes[1:]):
        cur["end"] = min(nxt["start"], cur["start"] + _EPISODE_CAP)
    if episodes:
        episodes[-1]["end"] = episodes[-1]["start"] + _EPISODE_CAP
    return episodes


def tally_tokens(log_path: Path, episodes: list[dict]) -> None:
    for e in episodes:
        e["read"] = e["write"] = e["calls"] = 0
    with open(log_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            when = _ts(rec["timestamp"])
            for e in episodes:
                if e["start"] <= when < e["end"]:
                    e["read"] += len(rec.get("prompt") or "") // 4
                    e["write"] += rec.get("generated_tokens") or (
                        (rec.get("raw_length") or len(rec.get("response") or "")) // 4
                    )
                    e["calls"] += 1
                    break


def main() -> None:
    mission_path = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "/tmp/ouroboros-challenge/.agent/mission.json"
    )
    log_path = Path(
        sys.argv[2] if len(sys.argv) > 2 else "llmvp/logs/interactions.jsonl"
    )

    episodes = load_episodes(mission_path)
    if not episodes:
        print(f"No repair_econ notes in {mission_path}")
        return
    tally_tokens(log_path, episodes)

    print(
        f"{'file':<18}{'class':<9}{'stage':<10}{'size_b':>8}{'calls':>7}{'read':>9}{'write':>8}"
    )
    for e in episodes:
        print(
            f"{e['file']:<18}{e['cls']:<9}{e['stage']:<10}{e['size']:>8}"
            f"{e['calls']:>7}{e['read']:>9}{e['write']:>8}"
        )

    print("\nBy failure class (episode totals: diagnose + patch stages):")
    by_cls: dict[str, dict] = {}
    for e in episodes:
        agg = by_cls.setdefault(e["cls"], {"n": 0, "read": 0, "write": 0, "size": 0})
        agg["n"] += 1
        agg["read"] += e["read"]
        agg["write"] += e["write"]
        agg["size"] += e["size"]
    for cls, agg in sorted(by_cls.items()):
        n = agg["n"]
        print(
            f"  {cls:<9} n={n:<3} avg_read={agg['read'] // n:<7} "
            f"avg_write={agg['write'] // n:<6} avg_file_bytes={agg['size'] // n}"
        )


if __name__ == "__main__":
    main()
