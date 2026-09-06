#!/usr/bin/env python3
"""List packed papers that must be EXCLUDED from a training export.

Operator ruling 2026-09-06: packs must be English (the continued-pre-training
targets are too small for multilingual packs). A paper packed from its
original-language text -- accepted, flagged extract_lingual, no translation
yet -- stays `packed` in the databank until the translate lane produces an
English text and the pack is re-cut, so the export must skip it meanwhile.

Run this immediately before every export; the set changes as translation
and repacks land.

    python dev/export_exclusions.py                       # print + write ~/tmp/exclude_non_english_packs.txt
    python dev/export_exclusions.py --out path/to/file.tsv
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")


def is_non_english_pack(rec: dict) -> bool:
    return (
        rec.get("pack_status") == "packed"
        and rec.get("extraction_status") == "extract_lingual"
        and not rec.get("translated")
    )


async def main_async(a) -> int:
    bank = await read_databank(LocalEffects(a.root))
    rows = sorted((k, r) for k, r in bank.items() if is_non_english_pack(r))
    leaves = sum(
        int((r.get("pack_quality") or {}).get("numeric_leaves") or 0) for _, r in rows
    )
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(
            "# Packed from non-English text; awaiting translation + repack. EXCLUDE from the training export.\n"
            "# Regenerate before every export:  .venv/bin/python dev/export_exclusions.py\n"
            "# paper_key\tlanguage\tleaves\tdataset_path\n"
        )
        for k, r in rows:
            f.write(
                f"{k}\t{r.get('language') or '?'}\t"
                f"{(r.get('pack_quality') or {}).get('numeric_leaves')}\t{r.get('dataset_path') or ''}\n"
            )
    print(f"{len(rows)} non-English packs ({leaves:,} leaves) -> {a.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument(
        "--out", default=os.path.expanduser("~/tmp/exclude_non_english_packs.txt")
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
