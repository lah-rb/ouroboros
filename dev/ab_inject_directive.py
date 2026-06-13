#!/usr/bin/env python3
"""Inject an identical new directive goal + reopen the mission (A/B helper).

Both arms start from the same archived replay-point and get the SAME
appended functional goal, so the only variable is the session mode.

Usage: ab_inject_directive.py <working_dir> "<directive text>"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.persistence.manager import PersistenceManager  # noqa: E402
from agent.persistence.models import GoalRecord  # noqa: E402


def main() -> None:
    working_dir, directive = sys.argv[1], sys.argv[2]
    pm = PersistenceManager(working_dir)
    m = pm.load_mission()
    m.goals.append(
        GoalRecord(description=directive, type="functional", status="incomplete")
    )
    m.status = "active"  # reopen the completed mission
    m.reopen_count = (getattr(m, "reopen_count", 0) or 0) + 1
    pm.save_mission(m)
    print(
        f"injected directive + reopened "
        f"(goals={len(m.goals)}, status={m.status}, reopen#{m.reopen_count})"
    )


if __name__ == "__main__":
    main()
