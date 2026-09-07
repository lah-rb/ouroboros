#!/usr/bin/env python3
"""Re-arm the translation cohort for the post-acceptance pass (2026-08-29).

Two repairs, both to databank/extraction.jsonl, both full-record appends
(the file's last-row-REPLACES semantics make a partial append a field
WIPE — the exact bug repair #1 exists to undo):

1. RESTORE md_path on the 13 extract_lingual records that
   dev/route_legacy_cjk_to_translation.py appended with only four fields,
   wiping md_path and making them permanently invisible to
   _translation_pending. The markdown was never gone — only the pointer.
   Restored as markdown/<key>.md when that file exists on disk.

2. RE-ARM the 34 translate_failed papers: back to extract_lingual with
   translate_attempts=0, so they flow through the new post-acceptance
   design like the rest of the cohort (curate first; translate only if
   accepted). Precedent: commit 910f8d1 rebooked seven at attempt 0 under
   a fixed gate. 23 of the 34 carry a mislabelled language "en" from
   catalog metadata; the re-vote happens naturally if they are ever
   re-extracted, and the translate prompt's hint now trusts `language`
   only when it is a real non-en code.

Writes are filtered to EXTRACTION_OWNED_FIELDS — the same filter
append_extraction_records applies — so nothing scraper-owned gets frozen
into the sidecar where it would shadow later papers-side updates.

Usage:
  .venv/bin/python dev/rearm_translation_cohort.py            # dry run
  .venv/bin/python dev/rearm_translation_cohort.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

WORKSPACE = os.path.expanduser("~/corpora/ouroboros-spectra")


def mission_is_running() -> bool:
    """Fail CLOSED: papers/extraction jsonl writers must not race a live
    mission (tools/oa_triage.py precedent)."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001 — cannot verify => refuse
        return True
    return any(
        "ouroboros.py start" in ln or "api/main.py" in ln
        for ln in out.splitlines()
        if "grep" not in ln
    )


def load_merged(path: str) -> dict:
    d: dict[str, dict] = {}
    if not os.path.exists(path):
        return d
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            k = r.get("paper_key")
            if k:
                d[k] = r  # last row replaces
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workspace", default=WORKSPACE)
    args = ap.parse_args()

    if mission_is_running():
        print("REFUSING: a mission or LLMVP server is running on this machine.")
        return 2

    from agent.actions.scholarly_actions import EXTRACTION_OWNED_FIELDS

    wd = args.workspace
    papers = load_merged(os.path.join(wd, "databank", "papers.jsonl"))
    extr = load_merged(os.path.join(wd, "databank", "extraction.jsonl"))
    merged = dict(papers)
    for k, v in extr.items():
        merged.setdefault(k, {}).update(v)

    now = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    fixed_md = unfixable = rearmed = 0

    for key, rec in merged.items():
        status = rec.get("extraction_status")
        if status == "extract_lingual" and not rec.get("md_path"):
            md_rel = f"markdown/{key}.md"
            if os.path.isfile(os.path.join(wd, "databank", md_rel)):
                out = {k: v for k, v in rec.items() if k in EXTRACTION_OWNED_FIELDS}
                out["paper_key"] = key
                out["md_path"] = md_rel
                out["updated_at"] = now
                out["failure_reason"] = (
                    (rec.get("failure_reason") or "")
                    + " [md_path restored, dev/rearm_translation_cohort.py]"
                ).strip()
                rows.append(out)
                fixed_md += 1
            else:
                unfixable += 1
                print(f"  UNFIXABLE (no markdown on disk): {key}")
        elif status == "translate_failed":
            out = {k: v for k, v in rec.items() if k in EXTRACTION_OWNED_FIELDS}
            out["paper_key"] = key
            out["extraction_status"] = "extract_lingual"
            out["translate_attempts"] = 0
            out["updated_at"] = now
            out["failure_reason"] = (
                "re-armed for the post-acceptance translation pass "
                "(dev/rearm_translation_cohort.py; was: "
                + (rec.get("failure_reason") or "")[:160]
                + ")"
            )
            rows.append(out)
            rearmed += 1

    print(
        f"md_path restored: {fixed_md}   unfixable: {unfixable}   "
        f"translate_failed re-armed: {rearmed}   rows to append: {len(rows)}"
    )
    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0
    if not rows:
        return 0
    with open(
        os.path.join(wd, "databank", "extraction.jsonl"), "a", encoding="utf-8"
    ) as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"appended {len(rows)} full records to extraction.jsonl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
