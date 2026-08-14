#!/usr/bin/env python3
"""Backfill the article PAGE EXTENT onto databank records from OpenAlex.

WHY. `agent.actions.extraction_actions.acquisition_is_truncated` catches the
one defect no quality metric can: a PDF that is a *fragment* of the article it
claims to be. An extraction of page 1 of a 5-page paper is perfectly faithful —
to a fragment — so verification against that PDF's own text layer scores it
clean. Only the publisher's page extent disagrees.

`_normalize_openalex` now stores `first_page`/`last_page`, but records
collected before that carry neither, and the check is silent without them.
This fills them in.

TWO LOOKUPS, because a third of the corpus has no OpenAlex id: those records
came from S2 or CORE. They do have DOIs, and OpenAlex resolves DOIs in the same
batched filter, so the DOI pass recovers what the id pass cannot — measured on
the spectra corpus, 164 records by id and 111 more by DOI, and the ONE genuinely
truncated paper was in the DOI half. An id-only backfill would have found
nothing and reported success.

Appends whole records to papers.jsonl (last-wins on read, matching
append_records). Refuses while a mission is running.

    python tools/backfill_biblio.py --working-dir ~/corpora/ouroboros-spectra
    python tools/backfill_biblio.py --working-dir ... --dry-run

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_API = "https://api.openalex.org/works"
_BATCH = 40  # OpenAlex OR-filter length; 50 works for ids, 40 is safe for DOIs
_PAUSE = 0.3  # polite crawler — the same courtesy the scraper extends


def read_jsonl(path: Path) -> dict:
    out: dict = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("paper_key"):
            out[rec["paper_key"]] = rec
    return out


def _fetch(filt: str, mailto: str) -> list:
    url = f"{_API}?" + urllib.parse.urlencode(
        {
            "filter": filt,
            "select": "id,doi,biblio",
            "per-page": _BATCH,
            "mailto": mailto,
        }
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as fh:
            return (json.load(fh) or {}).get("results", []) or []
    except Exception as exc:  # noqa: BLE001 — a failed batch is not fatal
        print(f"  batch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return []


def _pages(work: dict) -> tuple:
    b = work.get("biblio") or {}
    return str(b.get("first_page") or ""), str(b.get("last_page") or "")


def collect(records: dict, keys: list, mailto: str) -> dict:
    """paper_key -> (first_page, last_page), by OpenAlex id then by DOI."""
    found: dict = {}

    by_id = [
        (k, (records[k].get("openalex_id") or "").rsplit("/", 1)[-1])
        for k in keys
        if (records.get(k) or {}).get("openalex_id")
    ]
    for i in range(0, len(by_id), _BATCH):
        chunk = by_id[i : i + _BATCH]
        results = _fetch("openalex_id:" + "|".join(w for _, w in chunk), mailto)
        lookup = {w["id"].rsplit("/", 1)[-1]: w for w in results if w.get("id")}
        for key, wid in chunk:
            if wid in lookup:
                found[key] = _pages(lookup[wid])
        time.sleep(_PAUSE)
    print(f"  by OpenAlex id : {len(found)}/{len(by_id)}")

    by_doi = [
        (k, str(records[k].get("doi") or "").lower())
        for k in keys
        if k not in found and (records.get(k) or {}).get("doi")
    ]
    before = len(found)
    for i in range(0, len(by_doi), _BATCH):
        chunk = by_doi[i : i + _BATCH]
        results = _fetch("doi:" + "|".join(d for _, d in chunk), mailto)
        lookup = {
            str(w.get("doi") or "").lower().split("doi.org/")[-1]: w for w in results
        }
        for key, doi in chunk:
            if doi in lookup:
                found[key] = _pages(lookup[doi])
        time.sleep(_PAUSE)
    print(f"  by DOI         : {len(found) - before}/{len(by_doi)}")
    return found


def mission_is_running(working_dir: Path) -> bool:
    """ps, NOT pgrep -fa: macOS accepts -a and silently ignores it. Fails
    CLOSED."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    target = str(working_dir)
    return any(
        "ouroboros.py start" in ln and (target in ln or working_dir.name in ln)
        for ln in out.splitlines()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--working-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mailto", default=os.environ.get("OUROBOROS_CONTACT_EMAIL", ""))
    args = ap.parse_args()

    W = Path(os.path.expanduser(args.working_dir)).resolve()
    bank_path = W / "databank" / "papers.jsonl"
    records = read_jsonl(bank_path)
    extraction = read_jsonl(W / "databank" / "extraction.jsonl")
    if not records:
        sys.exit(f"no papers.jsonl at {bank_path}")
    if not args.mailto:
        sys.exit(
            "OpenAlex asks for a contact address in the polite pool.\n"
            "  Pass --mailto or set OUROBOROS_CONTACT_EMAIL."
        )

    # Only records this check can ever apply to: something was extracted from
    # them. Re-querying 8k discovery rows to fill a field nothing reads would
    # spend the API quota the scraper needs.
    keys = [
        k
        for k, r in extraction.items()
        if r.get("extraction_status")
        in ("extracted", "extract_failed", "extract_unverified")
        and k in records
        and not (records[k].get("first_page") or records[k].get("last_page"))
    ]
    print(f"{len(keys)} extraction record(s) missing a page extent")
    if not keys:
        return 0

    found = collect(records, keys, args.mailto)
    usable = sum(
        1
        for first, last in found.values()
        if first.strip().isdigit() and last.strip().isdigit()
    )
    print(
        f"resolved {len(found)}; {usable} carry a clean numeric pair "
        f"(the rest are article numbers, roman numerals or supplements — "
        f"expected_page_extent declines those)"
    )

    updates = []
    for key, (first, last) in found.items():
        rec = dict(records[key])
        rec["first_page"], rec["last_page"] = first, last
        updates.append(rec)

    if args.dry_run:
        print(f"--dry-run: {len(updates)} record(s) NOT written")
        return 0
    if mission_is_running(W):
        print(
            "REFUSING: a mission is running on this workspace.\n"
            "  append_records rewrites papers.jsonl wholesale; writing now "
            "races it.\n  Pause the mission, or pass --dry-run."
        )
        return 2
    with bank_path.open("a", encoding="utf-8") as fh:
        for rec in updates:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"wrote {len(updates)} record(s) to {bank_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
