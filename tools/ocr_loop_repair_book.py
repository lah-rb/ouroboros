"""Book the OCR loop repair into the databank (root venv).

Reads the repair report (tools/pdf_extract/ocr_loop_repair.py) and, for every
document whose markdown was rewritten:

* extraction side (append_extraction_records): ``extraction_quality.loop_repair``
  with what was re-OCR'd, what was collapsed and what was removed; a lingual
  paper that had already been TRANSLATED is re-armed (``translated`` False,
  ``extract_lingual``, attempt/epoch reset, parts bank wiped) so the translate
  drain re-does it from the repaired source — the stale ``.en.md`` stays until
  the new one overwrites it;
* papers side (append_records): a PACKED English paper goes to ``needs_repack``
  so a curate lane rebuilds its pack from the repaired text. Lingual papers are
  left to the translate drain, which books ``needs_repack`` on completion —
  booking it now would let a curate lane repack from the stale English.

Writes the SAFE side first (extraction), the activating side last (papers),
and only one row per document per side. Never partial rows: the full merged
record is appended.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402


def load_report(path: Path) -> list[dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if r.get("status") == "ok" and r.get("written"):
            rows[r["key"]] = r  # last row per key wins (a --resume re-run)
    return list(rows.values())


def plan(rec: dict, row: dict, now: str) -> tuple[dict | None, dict | None]:
    """(extraction_row, papers_row) for one repaired document."""
    if (rec.get("extraction_quality") or {}).get("loop_repair", {}).get(
        "report_seconds"
    ) == row.get("seconds") and (rec.get("extraction_quality") or {}).get(
        "loop_repair", {}
    ).get(
        "at"
    ):
        return None, None  # already booked from this very report row
    methods = row.get("methods") or {}
    q = dict(rec.get("extraction_quality") or {})
    q["loop_repair"] = {
        "at": now,
        "pages_reocr": sum(v for k, v in methods.items() if k.startswith("reocr")),
        "pages_collapsed": sum(v for k, v in methods.items() if "collapsed" in k),
        "removed": row.get("before"),
        "residual_after": row.get("after"),
        "backup": (
            os.path.relpath(row["backup"], start=str(REPO)) if row.get("backup") else ""
        ),
        "report_seconds": row.get("seconds"),
    }
    ext = dict(rec)
    ext["extraction_quality"] = q
    ext["updated_at"] = now
    # The translation domain is a matter of STATUS, not of the language vote
    # (a Japanese scan carried language "en"): translated, pending, or retired.
    status = rec.get("extraction_status")
    in_translation = bool(rec.get("translated")) or status in (
        "extract_lingual",
        "translate_failed",
    )
    # re-arm: a TRANSLATED paper (its English carries the loop, or a marker)
    # and a RETIRED one (loops were part of why); a pending one needs nothing.
    rearm = (bool(rec.get("translated")) and status == "extracted") or (
        status == "translate_failed"
    )
    if rearm:
        ext["translated"] = False
        ext["extraction_status"] = "extract_lingual"
        ext["translate_attempts"] = 0
        ext["translate_epoch"] = 0
        ext["failure_reason"] = "loop repair: source re-OCR'd, translation re-armed"
    papers = None
    if rec.get("pack_status") == "packed" and not in_translation:
        papers = dict(rec)
        papers["pack_status"] = "needs_repack"
        papers["updated_at"] = now
    return ext, papers


async def main_async(args) -> int:
    fx = LocalEffects(str(args.workspace))
    databank = await read_databank(fx)
    rows = load_report(Path(args.report))
    now = datetime.now(timezone.utc).isoformat()
    ext_rows, paper_rows, rearmed, repacks, skipped = [], [], [], [], 0
    for row in rows:
        rec = databank.get(row["key"])
        if not rec:
            skipped += 1
            continue
        ext, papers = plan(rec, row, now)
        if ext is None:
            skipped += 1
            continue
        ext_rows.append(ext)
        if ext.get("extraction_status") == "extract_lingual" and rec.get(
            "extraction_status"
        ) in ("extracted", "translate_failed"):
            rearmed.append(row["key"])
        if papers is not None:
            paper_rows.append(papers)
            repacks.append(row["key"])
    print(
        f"report rows {len(rows)} | to book {len(ext_rows)} | re-arm translation {len(rearmed)} | "
        f"needs_repack {len(repacks)} | skipped (missing/already booked) {skipped}"
    )
    for k in rearmed[:8]:
        print("  re-arm:", k[:70])
    if args.dry_run:
        print("dry run — nothing written")
        return 0
    if ext_rows:
        await append_extraction_records(fx, ext_rows)
        # wipe the stale parts bank of every re-armed paper (bound to the old text)
        for k in rearmed:
            parts = (
                Path(args.workspace) / "databank" / "translations" / f"{k}.parts.jsonl"
            )
            if parts.exists():
                parts.write_text("")
    if paper_rows:
        await append_records(fx, paper_rows)
    print(f"booked: {len(ext_rows)} extraction row(s), {len(paper_rows)} papers row(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--workspace", default=os.path.expanduser("~/corpora/ouroboros-spectra")
    )
    ap.add_argument("--report", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    args.report = args.report or os.path.join(
        args.workspace, "databank", "_loop_repair", "report.jsonl"
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
