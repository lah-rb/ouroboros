#!/usr/bin/env python3
"""Retry oa_unresolved PDFs via ALTERNATE OA locations (repositories).

Publisher WAFs (Cloudflare-class TLS fingerprinting) 403 non-browser
clients even on fully-OA content — a browser User-Agent alone recovered
2/73 in the July rebuild, and curl with the same UA is also blocked, so
this is not a headers problem. The principled path: Unpaywall lists
EVERY legal OA copy per DOI; repository mirrors (PMC, arXiv,
institutional) serve plain HTTP and don't fingerprint. Prefer
repositories, fall back through remaining locations, keep failures
cataloged with reasons.

    uv run python dev/redownload_unresolved.py [working_dir]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _candidate_urls(up_json: dict) -> list[str]:
    """All OA pdf URLs for a DOI, repositories first, deduped in order."""
    locs = [
        loc
        for loc in (up_json.get("oa_locations") or [])
        if isinstance(loc, dict) and loc.get("url_for_pdf")
    ]
    locs.sort(key=lambda loc: 0 if loc.get("host_type") == "repository" else 1)
    seen, urls = set(), []
    for loc in locs:
        url = str(loc["url_for_pdf"])
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def main() -> None:
    working_dir = (
        sys.argv[1] if len(sys.argv) > 1 else "/Users/lah-rb/corpora/ouroboros-hea"
    )
    from agent.actions.scholarly_actions import (
        PDF_DIR,
        _contact_email,
        append_records,
        polite_request,
        read_databank,
    )
    from agent.effects.local import LocalEffects

    effects = LocalEffects(working_directory=working_dir)
    bank = await read_databank(effects)
    todo = [
        r
        for r in bank.values()
        if r.get("access_status") == "oa_unresolved" and r.get("doi")
    ]
    print(f"{len(bank)} records, retrying {len(todo)} via alternate OA locations")

    updates, ok = [], 0
    for i, rec in enumerate(todo):
        up = await polite_request(
            effects,
            "GET",
            f"https://api.unpaywall.org/v2/{rec['doi']}",
            params={"email": _contact_email()},
        )
        urls = (
            _candidate_urls(up.json_data)
            if up.status == 200 and isinstance(up.json_data, dict)
            else []
        )
        # The publisher URL we already have goes last (it 403'd once).
        known = str(rec.get("oa_pdf_url") or "")
        if known and known not in urls:
            urls.append(known)

        path = f"{PDF_DIR}/{rec['paper_key']}.pdf"
        last_err = "no OA locations"
        for url in urls[:4]:  # bounded politeness: at most 4 attempts/paper
            dl = await effects.http_download(url, path)
            await asyncio.sleep(2.0)
            if dl.success:
                rec["access_status"] = "oa_pdf"
                rec["oa_pdf_url"] = url
                rec["pdf_path"] = path
                rec["failure_reason"] = ""
                ok += 1
                break
            last_err = str(dl.error or "")[:80]
        else:
            rec["failure_reason"] = f"all OA locations failed: {last_err}"
        updates.append(rec)
        if (i + 1) % 15 == 0:
            print(f"  …{i + 1}/{len(todo)} ({ok} recovered)")

    if updates:
        await append_records(effects, updates)
    bank = await read_databank(effects)
    total = sum(1 for r in bank.values() if r.get("access_status") == "oa_pdf")
    print(f"recovered {ok}/{len(todo)}; corpus now has {total} oa_pdf")


if __name__ == "__main__":
    asyncio.run(main())
