#!/usr/bin/env python3
"""Reopen first-pass denials for a second-opinion curation pass.

The M6 bake-off's chosen combination: gpt-oss curates the full corpus
(fast, best grounding, but the strictest reviewer), then gemma-4-31b
(best gold agreement) re-reviews ONLY the denials. This script flips
each denied record back to pending, archiving the first verdict into a
``review_history`` list — nothing is overwritten, both opinions stay on
the record. Run it between the two curator missions, with the server
switched to the second-opinion model.

    uv run python dev/second_opinion_denials.py [working_dir]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    working_dir = (
        sys.argv[1] if len(sys.argv) > 1 else "/Users/lah-rb/corpora/ouroboros-hea"
    )
    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.effects.local import LocalEffects

    effects = LocalEffects(working_directory=working_dir)
    bank = await read_databank(effects)
    denied = [
        r
        for r in bank.values()
        if r.get("review_status") in ("denied", "review_failed")
    ]
    updates = []
    for rec in denied:
        history = list(rec.get("review_history") or [])
        history.append(
            {
                "status": rec.get("review_status"),
                "summary": rec.get("review_summary", ""),
                "issues": rec.get("review_issues", []),
                "method": rec.get("curation_method", ""),
            }
        )
        rec["review_history"] = history
        rec["review_status"] = ""
        rec["review_summary"] = ""
        rec["review_issues"] = []
        updates.append(rec)
    if updates:
        await append_records(effects, updates)
    print(f"reopened {len(updates)} denial(s) for a second opinion")


if __name__ == "__main__":
    asyncio.run(main())
