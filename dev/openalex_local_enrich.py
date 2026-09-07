#!/usr/bin/env python3
"""Enrich corpus records from the local OpenAlex meta table instead of the API.

WHY (2026-09-04). `action_enrich_paper_metadata` spends the metered OpenAlex
credit on batched DOI lookups for page extent and license -- 114 of its 416
calls in one run came back 429. The mirror holds the same fields for every
work as of the snapshot date, so the lookup is a JOIN, not a request.

WHAT IT FILLS, and only where the record is EMPTY: first_page/last_page,
license, language, openalex_id, and -- as a NEW OA location, appended to
oa_pdf_urls, never replacing oa_pdf_url -- the snapshot's best OA pdf when
the record has never tried it. Every value is stamped in `*_source` fields
so a consumer can tell snapshot-derived metadata from publisher-stated.

Dry run by default; --apply appends FULL merged records to papers.jsonl
(last-row-replaces semantics: never a partial row).

    python dev/openalex_local_enrich.py                # report only
    python dev/openalex_local_enrich.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import (  # noqa: E402
    DATABANK_PATH,
    _read_jsonl_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
META_DB = os.path.expanduser("~/corpora/openalex_meta.duckdb")
SNAPSHOT = "2026-06-26"  # latest updated_date partition in the mirror


def _norm_doi(d) -> str:
    return str(d or "").lower().strip().rstrip(".,;")


def lookup(dois: list[str], db: str) -> dict[str, dict]:
    con = duckdb.connect(db, read_only=True)
    con.execute("PRAGMA threads=6")
    con.execute("PRAGMA memory_limit='16GB'")
    con.execute("CREATE TEMP TABLE _d (doi VARCHAR)")
    con.executemany("INSERT INTO _d VALUES (?)", [[d] for d in dois])
    rows = con.execute(
        """SELECT d.doi, w.id, w.fp, w.lp, w.lic, w.lang, w.oa_pdf, w.oa_status
           FROM _d d JOIN works_meta w ON lower(w.doi) = d.doi"""
    ).fetchall()
    return {
        r[0]: dict(
            id=r[1], fp=r[2], lp=r[3], lic=r[4], lang=r[5], oa_pdf=r[6], oa_status=r[7]
        )
        for r in rows
    }


async def main_async(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    side = await _read_jsonl_records(fx, DATABANK_PATH)
    scope = {
        k: r
        for k, r in bank.items()
        if r.get("doi") and (a.all or r.get("review_status") or r.get("pdf_path"))
    }
    found = lookup(sorted({_norm_doi(r["doi"]) for r in scope.values()}), a.db)
    print(f"records in scope {len(scope):,}; DOIs found in the snapshot {len(found):,}")
    gains = collections.Counter()
    out = []
    src = f"openalex snapshot {SNAPSHOT}"
    for k, r in scope.items():
        w = found.get(_norm_doi(r["doi"]))
        if not w:
            continue
        rec = dict(side.get(k) or r)  # the papers-side row, never the merged view
        rec["paper_key"] = k
        changed = False
        if not (rec.get("first_page") or rec.get("last_page")) and (w["fp"] or w["lp"]):
            rec["first_page"], rec["last_page"] = str(w["fp"] or ""), str(w["lp"] or "")
            rec["page_extent_source"] = src
            gains["pages"] += 1
            changed = True
        if not rec.get("license") and w["lic"]:
            rec["license"], rec["license_source"] = str(w["lic"]), src
            gains["license"] += 1
            changed = True
        if not rec.get("language") and w["lang"]:
            rec["language"], rec["language_source"] = str(w["lang"]), src
            gains["language"] += 1
            changed = True
        if not rec.get("openalex_id") and w["id"]:
            rec["openalex_id"] = f"https://openalex.org/{w['id']}"
            gains["openalex_id"] += 1
            changed = True
        tried = {str(rec.get("oa_pdf_url") or "")} | set(rec.get("oa_pdf_urls") or [])
        if w["oa_pdf"] and w["oa_pdf"] not in tried and not rec.get("pdf_path"):
            rec["oa_pdf_urls"] = list(rec.get("oa_pdf_urls") or []) + [w["oa_pdf"]]
            if not rec.get("oa_pdf_url"):
                rec["oa_pdf_url"] = w["oa_pdf"]
            rec["oa_lead_source"] = src
            gains["new_oa_location"] += 1
            changed = True
        if changed:
            out.append(rec)
    print("fields filled:", dict(gains), f"| records touched {len(out):,}")
    if not a.apply:
        print("(dry run: nothing written)")
        return 0
    await append_records(fx, out)
    print(f"applied {len(out):,} records")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--db", default=META_DB)
    ap.add_argument(
        "--all", action="store_true", help="include discovered-but-unacquired records"
    )
    ap.add_argument("--apply", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
