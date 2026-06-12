#!/usr/bin/env python3
"""Backfill language + license onto an existing scraper databank.

The fields landed in the catalog normalizers after the HEA corpus was
built; this one-shot queries OpenAlex (language, primary OA license)
and Unpaywall (license fallback) by DOI through the same polite_request
machinery the scraper uses, then appends updated records (last-wins).

Usage:
    OUROBOROS_CONTACT_EMAIL=you@example.com \
        uv run python dev/backfill_language_license.py [working_dir]
Default working_dir: /tmp/ouroboros-hea-scrape
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


async def main() -> None:
    working_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ouroboros-hea-scrape"

    from agent.actions.scholarly_actions import (
        _contact_email,
        append_records,
        polite_request,
        read_databank,
    )
    from agent.effects.local import LocalEffects

    effects = LocalEffects(working_directory=working_dir)

    # The request budget is mission-scoped politeness; the completed
    # scrape's counter persists in the working dir and would starve
    # this one-shot. Reset the count, keep the per-host pacing state.
    from agent.actions.scholarly_actions import _HTTP_STATE_KEY

    http_state = await effects.read_state(_HTTP_STATE_KEY) or {}
    http_state["total_requests"] = 0
    await effects.write_state(_HTTP_STATE_KEY, http_state)

    bank = await read_databank(effects)
    todo = [
        r
        for r in bank.values()
        if r.get("doi") and (not r.get("language") or not r.get("license"))
    ]
    print(f"{len(bank)} records, {len(todo)} need language/license")

    updates: list[dict] = []
    for i, rec in enumerate(todo):
        doi = rec["doi"]
        oa = await polite_request(
            effects,
            "GET",
            f"https://api.openalex.org/works/doi:{doi}",
            params={"select": "language,best_oa_location", "mailto": _contact_email()},
        )
        changed = False
        if oa.status == 200 and isinstance(oa.json_data, dict):
            lang = str(oa.json_data.get("language") or "")
            lic = str((oa.json_data.get("best_oa_location") or {}).get("license") or "")
            if lang and not rec.get("language"):
                rec["language"] = lang
                changed = True
            if lic and not rec.get("license"):
                rec["license"] = lic
                changed = True
        if not rec.get("license"):
            up = await polite_request(
                effects,
                "GET",
                f"https://api.unpaywall.org/v2/{doi}",
                params={"email": _contact_email()},
            )
            if up.status == 200 and isinstance(up.json_data, dict):
                lic = str(
                    (up.json_data.get("best_oa_location") or {}).get("license") or ""
                )
                if lic:
                    rec["license"] = lic
                    changed = True
        if changed:
            updates.append(rec)
        if (i + 1) % 25 == 0:
            print(f"  …{i + 1}/{len(todo)}")

    if updates:
        await append_records(effects, updates)
    print(f"backfilled {len(updates)} records")

    bank = await read_databank(effects)
    from collections import Counter

    langs = Counter(r.get("language") or "?" for r in bank.values())
    lics = Counter(r.get("license") or "?" for r in bank.values())
    print("languages:", dict(langs.most_common(6)))
    print("licenses:", dict(lics.most_common(8)))


if __name__ == "__main__":
    asyncio.run(main())
