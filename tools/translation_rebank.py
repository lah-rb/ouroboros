#!/usr/bin/env python3
"""Re-bank translations onto changed sources: translate only what changed.

The library is agent/translation_rebank.py (its docstring is the design). This
driver finds the papers, picks the source each translation was bound to, and
writes the result where the translate drain reads it:

  FINISHED translations (translated, .en.md on disk) whose source an OCR repair
  changed: the .en.md is aligned onto the OLD source's pages, trusted units on
  unchanged pages are banked, and the record is flagged ``retranslate`` — the
  paper stays translated with its current English until the drain's patch
  books (translation_actions._retranslation_pending).

  PENDING translations whose bank went stale (its (n, src_len) binding no
  longer matches the source): the bank's own chunks are carried onto the new
  source wherever their pages did not change.

The OLD source is a repair backup: databank/_injection_repair/backup/<key>.md
(the 2026-10-04 injection repair) or databank/_loop_repair/backup/<key>.md
(2026-09-19) — for a stale bank, the one whose length and chunk count match
the bank's binding.

Writes, per paper: the parts file (atomic replace; the old one kept under
databank/translations/_rebank_backup/) and one full extraction record. A parts
file touched in the last 10 minutes is skipped: the drain may be on it.

    .venv/bin/python tools/translation_rebank.py --finished [--dry-run]
    .venv/bin/python tools/translation_rebank.py --stale-banks [--dry-run]
    .venv/bin/python tools/translation_rebank.py --keys K1 K2 [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    read_databank,
)
from agent.actions.translation_actions import chunk_markdown  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402
from agent.translation_rebank import (  # noqa: E402
    chunks_from_plan,
    plan_rebank,
    units_from_bank,
    units_from_translation,
)

DEFAULT_WORKSPACE = os.path.expanduser("~/corpora/ouroboros-spectra")
BUSY_SECONDS = 600
BACKUP_DIRS = ("_injection_repair/backup", "_loop_repair/backup")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _bank_lines(parts_path: Path) -> list[dict]:
    out = []
    for line in _read(parts_path).splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _epoch(rec: dict) -> int:
    raw = rec.get("translate_epoch")
    return int(raw if raw is not None else (rec.get("translate_attempts") or 0))


def _write_bank(
    ws: Path, key: str, src: str, plan: list[int], parts: dict, epoch: int
) -> None:
    tdir = ws / "databank" / "translations"
    path = tdir / f"{key}.parts.jsonl"
    if path.is_file() and path.stat().st_size:
        bdir = tdir / "_rebank_backup"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, bdir / f"{key}.{int(time.time())}.parts.jsonl")
    lines = [json.dumps({"plan": plan, "src_len": len(src)})]
    for i in sorted(parts):
        lines.append(
            json.dumps(
                {
                    "idx": i,
                    "n": len(plan),
                    "src_len": len(src),
                    "attempt": epoch,
                    "text": parts[i],
                    "rebanked": True,
                },
                ensure_ascii=False,
            )
        )
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def rebank_finished(ws: Path, key: str, rec: dict) -> dict:
    md = ws / "databank" / "markdown" / f"{key}.md"
    en = ws / str(rec.get("md_en_path") or f"databank/markdown/{key}.en.md")
    src, en_md = _read(md), _read(en)
    old_path = ws / "databank" / BACKUP_DIRS[0] / f"{key}.md"
    old = _read(old_path)
    if not (src and en_md):
        return {"key": key, "skip": "no source or no English"}
    if not old or old == src:
        return {"key": key, "skip": "source unchanged since translation"}
    units, stats = units_from_translation(old, en_md)
    if not units:
        return {"key": key, "skip": stats.get("error", "no units")}
    res = plan_rebank(old, src, units)
    if "error" in res:
        return {"key": key, "skip": res["error"], "align": stats}
    return {
        "key": key,
        "mode": "finished",
        "old": str(old_path.relative_to(ws)),
        "src": src,
        "align": stats,
        **res,
    }


def rebank_stale(ws: Path, key: str, rec: dict) -> dict:
    md = ws / "databank" / "markdown" / f"{key}.md"
    src = _read(md)
    epoch = _epoch(rec)
    lines = [
        d
        for d in _bank_lines(ws / "databank" / "translations" / f"{key}.parts.jsonl")
        if not d.get("failed") and str(d.get("text") or "").strip() and "idx" in d
    ]
    lines = [d for d in lines if int(d.get("attempt", -1)) == epoch]
    if not src or not lines:
        return {"key": key, "skip": "no source or no banked parts this epoch"}
    n, src_len = int(lines[-1]["n"]), int(lines[-1]["src_len"])
    if src_len == len(src) and n == len(chunk_markdown(src)):
        return {"key": key, "skip": "bank already valid"}
    old, old_rel = "", ""
    for d in BACKUP_DIRS:
        cand = ws / "databank" / d / f"{key}.md"
        text = _read(cand)
        if text and len(text) == src_len and len(chunk_markdown(text)) == n:
            old, old_rel = text, str(cand.relative_to(ws))
            break
    if not old:
        return {
            "key": key,
            "skip": f"no backup matches the bank (n={n}, len={src_len})",
        }
    parts = {
        int(d["idx"]): str(d["text"])
        for d in lines
        if int(d["n"]) == n and int(d["src_len"]) == src_len
    }
    res = plan_rebank(old, src, units_from_bank(old, parts))
    if "error" in res:
        return {"key": key, "skip": res["error"]}
    return {"key": key, "mode": "stale_bank", "old": old_rel, "src": src, **res}


async def main_async(a) -> int:
    ws = Path(a.workspace)
    fx = LocalEffects(str(ws))
    db = await read_databank(fx)
    now = datetime.now(timezone.utc).isoformat()
    todo = []
    for key, rec in db.items():
        if a.keys and key not in a.keys:
            continue
        if (
            rec.get("review_status") != "accepted"
            or rec.get("record_kind") == "supplement"
        ):
            continue
        en = ws / str(rec.get("md_en_path") or f"databank/markdown/{key}.en.md")
        if rec.get("translated") and en.is_file() and (a.finished or a.keys):
            todo.append(("finished", key, rec))
        elif (
            rec.get("extraction_status") == "extract_lingual"
            and not rec.get("translated")
            and (a.stale_banks or a.keys)
        ):
            todo.append(("stale", key, rec))
    report, totals = [], {"rebanked": 0, "carried": 0, "dirty_chunks": 0, "skipped": 0}
    for mode, key, rec in sorted(todo, key=lambda t: t[1]):
        parts_path = ws / "databank" / "translations" / f"{key}.parts.jsonl"
        if (
            parts_path.is_file()
            and time.time() - parts_path.stat().st_mtime < BUSY_SECONDS
        ):
            report.append({"key": key, "skip": "bank touched in the last 10 min"})
            totals["skipped"] += 1
            continue
        res = (
            rebank_finished(ws, key, rec)
            if mode == "finished"
            else rebank_stale(ws, key, rec)
        )
        if "skip" in res:
            if not res["skip"].startswith(("source unchanged", "bank already valid")):
                report.append(res)
                totals["skipped"] += 1
            continue
        src, plan, parts = res.pop("src"), res["plan"], res["parts"]
        # The plan must reproduce the source exactly, and every carried unit's
        # source span must be text the old translation was made from.
        assert "\n\n".join(chunks_from_plan(src, plan)) == src
        dirty_chars = sum(
            len(c) for i, c in enumerate(chunks_from_plan(src, plan)) if i not in parts
        )
        row = {
            "key": key,
            "mode": res["mode"],
            "old": res["old"],
            "chunks": len(plan),
            "carried": res["carried"],
            "dirty_chunks": res["dirty_chunks"],
            "dirty_chars": dirty_chars,
            "src_chars": len(src),
            "changed_pages": len(res["changed_pages"]),
            "align": res.get("align"),
        }
        report.append(row)
        totals["rebanked"] += 1
        totals["carried"] += res["carried"]
        totals["dirty_chunks"] += res["dirty_chunks"]
        totals["dirty_chars"] = totals.get("dirty_chars", 0) + dirty_chars
        totals["src_chars"] = totals.get("src_chars", 0) + len(src)
        if a.dry_run:
            continue
        if mode == "finished":
            epoch = _epoch(rec) + 1
            _write_bank(ws, key, src, plan, parts, epoch)
            ext = dict(rec)
            ext.update(
                retranslate=True,
                retranslate_failed="",
                translate_epoch=epoch,
                translate_attempts=0,
                retranslate_info={k: row[k] for k in row if k != "key"} | {"at": now},
                updated_at=now,
            )
        else:
            _write_bank(ws, key, src, plan, parts, _epoch(rec))
            ext = dict(rec)
            ext.update(
                rebank_info={k: row[k] for k in row if k != "key"} | {"at": now},
                updated_at=now,
            )
        await append_extraction_records(fx, [ext])
    for r in report:
        print(json.dumps(r, ensure_ascii=False)[:400])
    if totals.get("src_chars"):
        totals["dirty_share"] = round(totals["dirty_chars"] / totals["src_chars"], 4)
    print(json.dumps(totals))
    if a.dry_run:
        print("--dry-run: nothing written")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--finished", action="store_true", help="finished translations")
    ap.add_argument("--stale-banks", action="store_true", help="pending papers")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if not (a.keys or a.finished or a.stale_banks):
        ap.error("say which: --finished, --stale-banks or --keys")
    return asyncio.run(main_async(a))


if __name__ == "__main__":
    raise SystemExit(main())
