#!/usr/bin/env python3
"""Book binder-set extractions run OUTSIDE the mission, and their preapproval.

Operator ruling (2026-09-07): the binder papers (papers.jsonl `binder: true`)
are PREAPPROVED exceptions -- their path is OCR, then straight to pack. No
curate verdict. This script does the two bookings the pipeline would
otherwise do for them:

  1. --reports <log>...  : read the OCR tool's per-paper JSON report lines
     (stdout of tools/pdf_extract/extract_batch.py) and append extraction
     sidecar rows with the SAME verdict policy the ocr drain applies
     (agent/actions/extraction_actions.py, regular path) PLUS the preapproval
     rule: a below-gate binder paper is still `extracted` (flagged
     extraction_quality.below_gate) when repetition passes. Rows are built
     from the sidecar's LAST row for the key + changes: never a partial row
     (last-row-replaces semantics; see the 2026-08-22 incidents).
  2. preapproval          : every binder paper whose extraction_status is
     `extracted` or `extract_unverified` gets a papers.jsonl row with
     review_status=accepted, curation_method=binder_preapproved. Papers the
     curator already DENIED under the global data-in-text charter are
     flipped: the ruling is that the charter does not apply to this shelf.
     Rows are the papers.jsonl LAST row + changes; append_records strips
     the extraction-owned fields itself.

Both writers are the agent's own (O_APPEND + lock + field filter).

  .venv/bin/python dev/book_binder_local_ocr.py --reports a.log b.log --method llamacpp          # dry run
  .venv/bin/python dev/book_binder_local_ocr.py --reports a.log b.log --method llamacpp --apply
  .venv/bin/python dev/book_binder_local_ocr.py --apply                                          # preapproval only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import extraction_actions as ea  # noqa: E402
from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    append_records,
)
from agent.effects.local import LocalEffects  # noqa: E402

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")


def last_rows(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except Exception:  # noqa: BLE001
                continue
            k = r.get("paper_key")
            if k:
                out[k] = r
    return out


def read_reports(paths: list[str]) -> dict[str, dict]:
    reps: dict[str, dict] = {}
    for p in paths:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln.startswith("{"):
                    continue
                try:
                    r = json.loads(ln)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(r, dict) and r.get("paper_key"):
                    reps[r["paper_key"]] = r
    return reps


def verdict(rep: dict, method: str) -> dict:
    """Mirror of the ocr drain's regular-path verdict (extraction_actions)."""
    now = datetime.now(timezone.utc).isoformat()
    md_rel = rep.get("md_path") or ""
    md_abs = os.path.join(CORPUS, "databank", md_rel) if md_rel else ""
    md_size = os.path.getsize(md_abs) if md_abs and os.path.exists(md_abs) else 0
    changes: dict = {"updated_at": now}
    if rep.get("error"):
        changes["extraction_status"] = "extract_failed"
        changes["failure_reason"] = f"extraction (binder local run): {rep['error']}"[
            :400
        ]
        return changes
    verified = rep.get("verified_pages", 0) > 0
    ok = (
        (
            verified
            and rep.get("numeric_match_rate", 0) >= ea.MIN_NUMERIC_RATE
            and rep.get("span_pass_rate", 0) >= ea.MIN_SPAN_RATE
        )
        or (
            not verified
            and (rep.get("pages", 0) > 0 or rep.get("unverified_pages", 0) > 0)
            and md_size >= ea.UNVERIFIED_MIN_MD_BYTES
        )
    ) and rep.get("max_repeat_words", 0) <= ea.MAX_REPEAT_WORDS
    changes["md_path"] = os.path.join("databank", md_rel)
    changes["figure_count"] = rep.get("figures_kept", 0)
    changes["extraction_method"] = ea._EXTRACTION_METHODS.get(method, method)
    changes["extraction_quality"] = {
        k: rep.get(k, 0)
        for k in (
            "numeric_match_rate",
            "span_pass_rate",
            "max_repeat_words",
            "verified_pages",
            "unverified_pages",
            "pages",
            "seconds",
        )
    }
    if rep.get("script_profile"):
        changes["script_profile"] = rep["script_profile"]
    # PREAPPROVAL RULE (operator, 2026-09-07): a binder paper whose numeric /
    # span rates miss the drain's gates is still booked `extracted` when the
    # decode is not looping (repeat gate) and the markdown is substantial —
    # the rates are known to be uncorrelated with blind quality
    # (extraction-gate-measures-furniture) and the pack step judges the text
    # itself. The miss is RECORDED, not hidden: extraction_quality.below_gate.
    below_gate = (
        not ok
        and verified
        and rep.get("max_repeat_words", 0) <= ea.MAX_REPEAT_WORDS
        and md_size >= ea.UNVERIFIED_MIN_MD_BYTES
    )
    if ok or below_gate:
        changes["extraction_status"] = "extracted"
        changes["failure_reason"] = ""
        if not verified:
            changes["extraction_quality"]["unverified_text_layer"] = True
        if below_gate:
            changes["extraction_quality"]["below_gate"] = True
            changes["extraction_quality"]["below_gate_note"] = (
                "binder preapproval: booked despite "
                f"numeric={rep.get('numeric_match_rate', 0):.3f} "
                f"span={rep.get('span_pass_rate', 0):.3f} "
                f"(gates {ea.MIN_NUMERIC_RATE}/{ea.MIN_SPAN_RATE})"
            )
    elif not verified:
        changes["extraction_status"] = "extract_unverified"
        changes["failure_reason"] = (
            "extraction (binder local run): no verifiable text layer and "
            f"markdown only {md_size} bytes — rates vacuous"
        )
    else:
        changes["extraction_status"] = "extract_failed"
        changes["failure_reason"] = (
            "extraction (binder local run): below quality threshold "
            f"(numeric={rep.get('numeric_match_rate', 0):.2f}, "
            f"span={rep.get('span_pass_rate', 0):.2f}, "
            f"repeat={rep.get('max_repeat_words', 0)})"
        )
    return changes


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reports", nargs="*", default=[])
    ap.add_argument(
        "--method", default="llamacpp", choices=list(ea._EXTRACTION_METHODS)
    )
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    papers = last_rows(os.path.join(CORPUS, "databank", "papers.jsonl"))
    ext = last_rows(os.path.join(CORPUS, "databank", "extraction.jsonl"))
    binder = {k for k, r in papers.items() if r.get("binder")}
    effects = LocalEffects(CORPUS)

    # 1. extraction rows from tool reports
    ext_rows: list[dict] = []
    for key, rep in read_reports(args.reports).items():
        if key not in binder:
            print(f"  skip (not binder): {key}")
            continue
        base = dict(ext.get(key) or {"paper_key": key})
        base.update(verdict(rep, args.method))
        ext_rows.append(base)
        q = base.get("extraction_quality") or {}
        print(
            f"  EXT {base['extraction_status']:18s} pages={q.get('pages')} "
            f"num={q.get('numeric_match_rate')} span={q.get('span_pass_rate')} "
            f"rep={q.get('max_repeat_words')} figs={base.get('figure_count')} {key[:60]}"
        )
    # apply the new statuses to the in-memory view before deciding preapproval
    for row in ext_rows:
        ext[row["paper_key"]] = row

    # 2. preapproval rows
    now = datetime.now(timezone.utc).isoformat()
    pre_rows: list[dict] = []
    for key in sorted(binder):
        status = (ext.get(key) or {}).get("extraction_status") or ""
        if status not in ("extracted", "extract_unverified"):
            continue
        p = dict(papers[key])
        if p.get("review_status") == "accepted" and str(
            p.get("curation_method") or ""
        ).startswith("binder_preapproved"):
            # already stamped — possibly "binder_preapproved+<packing model>"
            # after dev/pack_binder_sonnet.py booked the pack; leave it.
            continue
        prev = p.get("review_status") or "unreviewed"
        p["review_status"] = "accepted"
        # Keep the packing model visible: "binder_preapproved+<model>" when a
        # pack already stamped one (pipeline convention is model+figtext_model;
        # the model part is what an audit needs).
        cm = str(p.get("curation_method") or "")
        p["curation_method"] = "binder_preapproved" + (
            f"+{cm.split('+')[0]}"
            if cm and not cm.startswith("binder_preapproved")
            else ""
        )
        p["deny_category"] = ""
        p["review_issues"] = []
        p["review_summary"] = (
            "Preapproved binder exception (operator ruling 2026-09-07): a foundational "
            "work of its technique, kept as reference material for the model; the "
            f"data-in-text curate charter does not apply to this shelf (was: {prev})."
        )
        p["updated_at"] = now
        pre_rows.append(p)
        print(f"  PRE {prev:11s} -> accepted/binder_preapproved  {key[:60]}")

    print(f"\n{len(ext_rows)} extraction row(s), {len(pre_rows)} preapproval row(s)")
    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0
    if ext_rows:
        await append_extraction_records(effects, ext_rows)
    if pre_rows:
        await append_records(effects, pre_rows)
    print("written.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
