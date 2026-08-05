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

Runs in the tool venv (no agent imports):
    tools/pdf_extract/.venv/bin/python tools/pdf_extract/reverify.py \
        /tmp/ouroboros-hea-scrape
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fitz  # noqa: E402
from extract_batch import (
    _NUM_RE,
    _norm,
    _prose_text,
    _SPAN_WORDS,
    _SPANS_PER_PAGE,
)  # noqa: E402

# Mirrors agent/actions/extraction_actions.py (tool venv can't import
# agent code); keep in sync.
MIN_NUMERIC_RATE = 0.85
MIN_SPAN_RATE = 0.75


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


def main() -> None:
    working_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    bank_path = os.path.join(working_dir, "databank", "papers.jsonl")
    records: dict[str, dict] = {}
    for line in open(bank_path):
        line = line.strip()
        if line:
            rec = json.loads(line)
            records[rec["paper_key"]] = rec

    updates = []
    for key, rec in records.items():
        if rec.get("extraction_status") not in ("extracted", "extract_failed"):
            continue
        md_path = os.path.join(working_dir, "databank", "markdown", f"{key}.md")
        pdf_path = os.path.join(working_dir, rec.get("pdf_path") or "")
        if not (os.path.isfile(md_path) and os.path.isfile(pdf_path)):
            continue
        md = open(md_path).read()
        num, span, verified, unverified = _rates(pdf_path, md)
        old_status = rec["extraction_status"]
        passed = num >= MIN_NUMERIC_RATE and span >= MIN_SPAN_RATE
        rec["extraction_status"] = "extracted" if passed else "extract_failed"
        rec["extraction_quality"] = {
            "numeric_match_rate": round(num, 4),
            "span_pass_rate": round(span, 4),
            "verified_pages": verified,
            "unverified_pages": unverified,
            "oracle": "prose-v2-wholedoc",
        }
        if passed:
            rec["md_path"] = f"databank/markdown/{key}.md"
            fig_dir = os.path.join(working_dir, "databank", "figures", key)
            rec["figure_count"] = (
                len(os.listdir(fig_dir)) if os.path.isdir(fig_dir) else 0
            )
            rec["failure_reason"] = ""
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

    with open(bank_path, "a") as f:
        for rec in updates:
            f.write(json.dumps(rec) + "\n")
    n_ok = sum(1 for r in updates if r["extraction_status"] == "extracted")
    print(
        f"\nreverified {len(updates)}: {n_ok} extracted, {len(updates) - n_ok} failed"
    )


if __name__ == "__main__":
    main()
