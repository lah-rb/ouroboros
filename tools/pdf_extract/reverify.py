#!/usr/bin/env python3
"""Re-verify existing extractions against the current oracle — no OCR.

Verification is recomputable from artifacts on disk (markdown +
source PDFs), so an oracle fix never costs an OCR re-run. Rescores
EVERY record that has markdown, then re-applies the quality policy:
records flip between extracted/extract_failed purely on the new
scores. One oracle across the whole corpus — the v3 stage must never
consume quality fields measured by two different rulers.

Nuance vs extract_batch.py: the saved markdown is page-joined, so
matching here is whole-document rather than per-page — marginally more
lenient (a numeric landing on a neighboring page still counts). Truth
extraction, thresholds, and corpus weighting are identical.

Reads the MERGED view (papers.jsonl with extraction.jsonl overlaid, exactly as
read_databank does) and appends its verdicts to extraction.jsonl — the
extractor's own file. Manual promotions are preserved: a human who read the
markdown against its PDF outranks this, and rescoring must refresh their
numbers without revoking their verdict.

Runs in the tool venv (no agent imports):
    tools/pdf_extract/.venv/bin/python tools/pdf_extract/reverify.py \
        /tmp/ouroboros-hea-scrape [--dry-run]
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz  # noqa: E402
from extract_batch import (
    _NUM_RE,
    _max_repeat_words,
    _norm,
    _prose_text,
    _SPAN_WORDS,
    _SPANS_PER_PAGE,
)  # noqa: E402

# Mirrors agent/actions/extraction_actions.py (tool venv can't import
# agent code); keep in sync.
MIN_NUMERIC_RATE = 0.75
MIN_SPAN_RATE = 0.70
MAX_REPEAT_WORDS = 200


def _rates(pdf_path: str, md: str) -> tuple[float, float, int, int]:
    md_n = _norm(md)
    md_compact = re.sub(r"\s", "", md_n)
    md_words = set(md_n.split())
    num_hit = num_total = span_hit = span_total = 0
    verified = unverified = 0
    doc = fitz.open(pdf_path)
    for page in doc:
        truth = _norm(_prose_text(page))
        if len(truth.strip()) < 200:
            unverified += 1
            continue
        verified += 1
        for n in _NUM_RE.findall(truth):
            num_total += 1
            if n in md_compact or n in md_n:
                num_hit += 1
        words = truth.split()
        if len(words) >= _SPAN_WORDS:
            step = max(1, (len(words) - _SPAN_WORDS) // _SPANS_PER_PAGE)
            spans = [
                words[i : i + _SPAN_WORDS]
                for i in range(0, len(words) - _SPAN_WORDS + 1, step)
            ][:_SPANS_PER_PAGE]
            for sw in spans:
                span_total += 1
                if sum(1 for w in sw if w in md_words) >= len(sw) - 1:
                    span_hit += 1
    num_rate = num_hit / num_total if num_total else 1.0
    span_rate = span_hit / span_total if span_total else 1.0
    return num_rate, span_rate, verified, unverified


def _read_jsonl(path: str) -> dict:
    records: dict[str, dict] = {}
    if not os.path.isfile(path):
        return records
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("paper_key"):
                records[rec["paper_key"]] = rec
    return records


def _mission_is_running(working_dir: str) -> bool:
    """ps, NOT pgrep -fa: macOS accepts -a and silently ignores it, printing
    bare PIDs, so a path match can never succeed. Fails CLOSED."""
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    name = os.path.basename(os.path.abspath(working_dir))
    return any(
        "ouroboros.py start" in ln and (working_dir in ln or name in ln)
        for ln in out.splitlines()
    )


def main() -> None:
    working_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    dry_run = "--dry-run" in sys.argv[2:]
    databank = os.path.join(working_dir, "databank")
    # THE SIDECAR IS THE EXTRACTOR'S FILE, and read_databank overlays it ON TOP
    # of papers.jsonl. This script used to read and append papers.jsonl, which
    # predates that split — every verdict it wrote was masked by the sidecar
    # and no downstream stage ever saw it. Read merged, write the sidecar.
    records = _read_jsonl(os.path.join(databank, "papers.jsonl"))
    for key, ext in _read_jsonl(os.path.join(databank, "extraction.jsonl")).items():
        base = records.get(key)
        records[key] = {**base, **ext} if base else ext

    if not dry_run and _mission_is_running(working_dir):
        sys.exit(
            "REFUSING: a mission is running on this workspace.\n"
            "  extraction.jsonl is appended by the extractor; writing now "
            "races it.\n  Pause the mission, or pass --dry-run."
        )

    updates = []
    promotions_kept = 0
    for key, rec in records.items():
        if rec.get("extraction_status") not in (
            "extracted",
            "extract_failed",
            "extract_unverified",
        ):
            continue
        md_path = os.path.join(working_dir, "databank", "markdown", f"{key}.md")
        pdf_path = os.path.join(working_dir, rec.get("pdf_path") or "")
        if not (os.path.isfile(md_path) and os.path.isfile(pdf_path)):
            continue
        md = open(md_path).read()
        num, span, verified, unverified = _rates(pdf_path, md)
        repeat = _max_repeat_words(md)
        old_status = rec["extraction_status"]
        # verified_pages > 0 is part of the shipping gate: with nothing
        # checkable the rates are vacuous 1.00s, not earned ones.
        passed = (
            verified > 0
            and num >= MIN_NUMERIC_RATE
            and span >= MIN_SPAN_RATE
            and repeat <= MAX_REPEAT_WORDS
        )
        # A HUMAN OVERRIDE OUTRANKS THE MACHINE. 34 records carry promoted_by
        # from a manual inspection that read the markdown against its PDF.
        # Rescoring must refresh their numbers, never revoke their verdict —
        # re-running this would otherwise silently undo every promotion.
        if rec.get("promoted_by"):
            promotions_kept += 1
            passed = True
        if passed:
            rec["extraction_status"] = "extracted"
        elif verified <= 0:
            # Its own terminal state, matching the shipping policy: another OCR
            # pass over a scan with no text layer yields the same unverifiable
            # result, so it must not go back on the re-extract queue.
            rec["extraction_status"] = "extract_unverified"
        else:
            rec["extraction_status"] = "extract_failed"
        rec["extraction_quality"] = {
            "numeric_match_rate": round(num, 4),
            "span_pass_rate": round(span, 4),
            "max_repeat_words": repeat,
            "verified_pages": verified,
            "unverified_pages": unverified,
            "oracle": "prose-v3-wholedoc",
        }
        if passed:
            rec["md_path"] = f"databank/markdown/{key}.md"
            fig_dir = os.path.join(working_dir, "databank", "figures", key)
            rec["figure_count"] = (
                len(os.listdir(fig_dir)) if os.path.isdir(fig_dir) else 0
            )
            rec["failure_reason"] = ""
        elif verified <= 0:
            rec["failure_reason"] = (
                f"extraction: no verifiable text layer "
                f"({verified + unverified} page(s), 0 verified) — "
                f"rates are vacuous, not earned"
            )
        elif repeat > MAX_REPEAT_WORDS:
            rec["failure_reason"] = (
                f"extraction: degenerate decode — {repeat} words of "
                f"back-to-back repetition (limit {MAX_REPEAT_WORDS})"
            )
        else:
            rec["failure_reason"] = (
                f"extraction: below quality threshold "
                f"(numeric={num:.2f}, span={span:.2f})"
            )
        rec["updated_at"] = datetime.now(timezone.utc).isoformat()
        updates.append(rec)
        flip = (
            ""
            if old_status == rec["extraction_status"]
            else (f"  [{old_status} -> {rec['extraction_status']}]")
        )
        print(f"{key[:50]:50s} num={num:.2f} span={span:.2f}{flip}")

    if dry_run:
        print("\n--dry-run: nothing written")
    else:
        with open(os.path.join(databank, "extraction.jsonl"), "a") as f:
            for rec in updates:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if promotions_kept:
        print(f"{promotions_kept} manual promotion(s) preserved, scores refreshed")
    n_ok = sum(1 for r in updates if r["extraction_status"] == "extracted")
    print(
        f"\nreverified {len(updates)}: {n_ok} extracted, {len(updates) - n_ok} failed"
    )


if __name__ == "__main__":
    main()
