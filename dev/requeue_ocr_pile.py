#!/usr/bin/env python3
"""Re-queue the OCR failed pile for a fresh pass (operator, 2026-10-04).

The pile: extract_failed papers whose reason is "below quality threshold" —
296 on 2026-10-04. Their markdown was read at T=0.8 (paddle's script injection)
and judged by an oracle that charged deliberate omissions (margin line
numbers behind a stamp, ignore-labelled furniture, flattened exponents). Both
were fixed in 3d0813c / c538332, so they get a new pass: greedy decoding, the
injection guard, and the corrected gate.

Left out: papers whose recorded rates are BOTH 0.00. Their text layer exists
but does not decode (e.g. Acta Petrologica Sinica's font encoding), so another
pass scores 0.00 again — that needs an unverified-text-layer fix, not OCR.

Per paper: the old markdown (+ segment parts), figures and region sidecar are
moved to databank/_pile_reocr/backup/<key>/, then ONE full extraction record
is appended (the merged record with the extraction fields reset — never a
partial row) with extraction_status "" — not terminal, so the OCR lane owes it.

Run with the mission STOPPED (tools/transition_3060.stop_mission) so no lane
writes extraction rows meanwhile.

    .venv/bin/python dev/requeue_ocr_pile.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

WS = Path(os.path.expanduser("~/corpora/ouroboros-spectra"))
RESET = {
    "extraction_status": "",
    "md_path": "",
    "md_en_path": "",
    "figure_count": 0,
    "extraction_method": "",
    "extraction_quality": {},
    "extract_progress": {},
    "translated": False,
    "translate_attempts": 0,
    "translate_epoch": 0,
}


def in_pile(rec: dict) -> str:
    """'' when the paper is re-queued, else why not."""
    if rec.get("extraction_status") != "extract_failed":
        return "not failed"
    if "below quality threshold" not in str(rec.get("failure_reason") or ""):
        return "other failure"
    q = rec.get("extraction_quality") or {}
    if (
        float(q.get("numeric_match_rate") or 0) == 0
        and float(q.get("span_pass_rate") or 0) == 0
    ):
        return "text layer does not decode (0.00/0.00)"
    if (
        rec.get("access_status") != "oa_pdf"
        or not (WS / str(rec.get("pdf_path") or "")).is_file()
    ):
        return "no PDF"
    return ""


def back_up(key: str) -> list[str]:
    db = WS / "databank"
    dest = db / "_pile_reocr" / "backup" / key
    moved = []
    for src in glob.glob(str(db / "markdown" / f"{key}.md")) + glob.glob(
        str(db / "markdown" / f"{key}.part_*.md")
    ):
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest / os.path.basename(src))
        moved.append(os.path.basename(src))
    for src in [db / "figures" / key] + [
        Path(p) for p in glob.glob(str(db / "ocr_regions" / f"{key}*.json"))
    ]:
        if src.exists():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), dest / src.name)
            moved.append(src.name)
    return moved


async def main_async(dry_run: bool) -> int:
    fx = LocalEffects(str(WS))
    db = await read_databank(fx)
    now = datetime.now(timezone.utc).isoformat()
    rows, skipped = [], {}
    for key, rec in sorted(db.items()):
        why = in_pile(rec)
        if why in ("not failed", "other failure"):
            continue
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        row = dict(rec)
        row.update(RESET)
        row["failure_reason"] = (
            "re-OCR queued 2026-10-04: greedy decoding + injection guard + corrected gate "
            f"(was: {str(rec.get('failure_reason'))[:120]})"
        )
        row["updated_at"] = now
        rows.append((key, row))
    print(f"re-queue {len(rows)}; left out {skipped}")
    if dry_run:
        print("--dry-run: nothing moved or written")
        return 0
    moved = 0
    for key, _ in rows:
        moved += bool(back_up(key))
    await append_extraction_records(fx, [r for _, r in rows])
    print(
        f"backed up {moved} paper(s) under databank/_pile_reocr/backup/; {len(rows)} reset row(s) appended"
    )
    (WS / "databank" / "_pile_reocr").mkdir(exist_ok=True)
    (WS / "databank" / "_pile_reocr" / "requeued.json").write_text(
        json.dumps({"at": now, "keys": [k for k, _ in rows]}, indent=1)
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(main_async(ap.parse_args().dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
